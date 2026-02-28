"""WeChat channel implementation via MaaWxAuto bridge WebSocket.

Connects to a running MaaWxAuto bridge server (ws://127.0.0.1:9574/ws)
which handles the actual WeChat UI automation (OCR + DB monitoring).

Bridge WS protocol:
  Inbound  (bridge → here):  {"type":"message", "chat_id":"...", "sender":"...", "content":"...", ...}
  Outbound (here → bridge):  {"type":"send_text", "chat_id":"...", "content":"...", "at":[]}
  Response (bridge → here):  {"type":"result", "success":true, "command":"send_text", ...}
"""

import asyncio
import json
import re
import time
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import WeChatConfig

# Echo suppression window (seconds).  After the bot sends a message,
# any self-sent message with matching content arriving within this
# window is treated as the DB echo of our own send and suppressed.
_ECHO_WINDOW = 30.0

# Context buffer: hold non-mention rich media messages so the next
# @mention in the same chat can include them as context.
_CONTEXT_BUFFER_TTL = 300.0   # 5 minutes expiry
_CONTEXT_BUFFER_MAX = 10      # max buffered messages per chat


def _strip_markdown(text: str) -> str:
    """Convert Markdown text to plain text suitable for WeChat.

    Handles: headers, bold, italic, strikethrough, inline code,
    fenced code blocks, links, images, blockquotes, horizontal rules.
    """
    # Fenced code blocks: ```lang\ncode\n``` → code
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    # Inline code: `code` → code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Images: ![alt](url) → alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # Links: [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Bold+italic: ***text*** or ___text___
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text)
    # Bold: **text** or __text__
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text)
    # Italic: *text* or _text_ (single, word boundaries for _ to avoid false matches)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"\b_(.+?)_\b", r"\1", text)
    # Strikethrough: ~~text~~
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Headers: ## text → text
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Blockquotes: > text → text
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    # Horizontal rules: --- or *** or ___
    text = re.sub(r"^[-*_]{3,}\s*$", "", text, flags=re.MULTILINE)
    # Collapse 3+ blank lines to 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class WeChatChannel(BaseChannel):
    """WeChat channel that connects to a MaaWxAuto bridge via WebSocket."""

    name = "wechat"

    def __init__(self, config: WeChatConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WeChatConfig = config
        self._ws = None
        self._connected = False
        self._listen_registered: set[str] = set()
        # Track recent bot sends: (chat_id, content_prefix) -> timestamp
        self._recent_sends: dict[tuple[str, str], float] = {}
        # Context buffer: chat_id -> [(timestamp, sender, msg_type, content, metadata)]
        self._context_buffer: dict[str, list[tuple[float, str, str, str, dict]]] = {}

    async def start(self) -> None:
        """Connect to MaaWxAuto bridge and listen for WeChat messages."""
        import websockets

        bridge_url = self.config.bridge_url
        logger.info("Connecting to MaaWxAuto bridge at {}...", bridge_url)

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    self._connected = True
                    logger.info("Connected to MaaWxAuto bridge")

                    # Register listeners for configured chats
                    await self._register_listeners()

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("MaaWxAuto bridge connection error: {}", e)

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WeChat channel."""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WeChat via the bridge."""
        if not self._ws or not self._connected:
            logger.warning("MaaWxAuto bridge not connected")
            return

        try:
            # Check if content contains file path for send_file
            if msg.media:
                for file_path in msg.media:
                    payload = {
                        "type": "send_file",
                        "chat_id": msg.chat_id,
                        "filepath": file_path,
                    }
                    await self._ws.send(json.dumps(payload, ensure_ascii=False))

            if msg.content:
                content = msg.content
                # Strip Markdown formatting for WeChat
                if self.config.strip_markdown:
                    content = _strip_markdown(content)

                payload = {
                    "type": "send_text",
                    "chat_id": msg.chat_id,
                    "content": content,
                    "at": [],
                }
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                # Record send fingerprint for echo suppression
                key = (msg.chat_id, content[:20])
                self._recent_sends[key] = time.time()
        except Exception as e:
            logger.error("Error sending WeChat message: {}", e)

    async def _register_listeners(self) -> None:
        """Register listen commands for configured chat IDs."""
        if not self._ws:
            return

        for chat_id in self.config.listen_chats:
            if chat_id in self._listen_registered:
                continue
            try:
                payload = {"type": "listen", "chat_id": chat_id}
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                self._listen_registered.add(chat_id)
                logger.info("Registered listener for chat: {}", chat_id)
            except Exception as e:
                logger.error("Failed to register listener for {}: {}", chat_id, e)

    def _should_respond(self, content: str, is_group: bool) -> tuple[bool, str]:
        """Check if the bot should respond to this message.

        Returns (should_respond, cleaned_content).
        In group chats with group_policy="mention", only respond when
        @bot_name is present, and strip the mention from content.
        DMs always pass through.
        """
        if not is_group:
            return True, content

        if self.config.group_policy != "mention":
            return True, content

        bot_name = self.config.bot_name
        if not bot_name:
            # No bot_name configured — can't do mention filtering
            return True, content

        # Check for @bot_name (with optional space/punctuation around it)
        mention_pattern = re.compile(
            r"@" + re.escape(bot_name) + r"\s*", re.IGNORECASE
        )
        if not mention_pattern.search(content):
            return False, content

        # Strip the @mention from content before forwarding
        cleaned = mention_pattern.sub("", content).strip()
        return True, cleaned

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the MaaWxAuto bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "message":
            sender = data.get("sender", "")
            chat_id = data.get("chat_id", "")
            content = data.get("content", "")
            is_group = data.get("is_group", False)

            # Echo suppression: if this is a self-sent message, only
            # suppress it when it matches something the bot recently sent.
            # This lets user-typed messages (e.g. to 文件传输助手) through
            # while preventing infinite loops from bot replies.
            if data.get("is_self", False):
                now = time.time()
                # Prune stale entries
                self._recent_sends = {
                    k: t for k, t in self._recent_sends.items()
                    if now - t < _ECHO_WINDOW
                }
                # Fuzzy match: DB content may differ slightly from sent
                # text (clipboard/OCR artifacts), so compare short prefix
                echo_hit = False
                for (cid, prefix), ts in list(self._recent_sends.items()):
                    if cid == chat_id and content[:20] == prefix[:20]:
                        del self._recent_sends[(cid, prefix)]
                        echo_hit = True
                        break
                if echo_hit:
                    logger.debug("Echo suppressed: {}", content[:40])
                    return
                # Self-sent but NOT a bot echo → let it through

            # Group mention filter
            should_respond, cleaned = self._should_respond(content, is_group)
            if not should_respond:
                # Non-mention → buffer all group messages as context
                if is_group:
                    msg_type_str = data.get("msg_type", "text")
                    self._buffer_context(chat_id, sender, msg_type_str, content, data)
                else:
                    logger.debug("Skipped (no mention): [{}] {}: {}",
                                 chat_id, sender, content[:40])
                return

            # Mention hit → flush buffered context and prepend to content
            context_lines = self._flush_context(chat_id) if is_group else ""
            content = (context_lines + "\n" + cleaned) if context_lines else cleaned

            # Use sender as sender_id for access control
            sender_id = sender

            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=content,
                metadata={
                    "message_id": data.get("message_id"),
                    "timestamp": data.get("timestamp"),
                    "is_group": is_group,
                    "msg_type": data.get("msg_type", "text"),
                },
            )

        elif msg_type == "result":
            success = data.get("success", False)
            command = data.get("command", "")
            message = data.get("message", "")
            if not success:
                logger.warning("Bridge command failed: {} - {}", command, message)
            else:
                logger.debug("Bridge command OK: {} - {}", command, message)

        elif msg_type == "error":
            logger.error("Bridge error: {}", data.get("message"))

    # ------------------------------------------------------------------
    # Context buffer for non-mention messages in group chats
    # ------------------------------------------------------------------

    def _buffer_context(
        self, chat_id: str, sender: str, msg_type: str, content: str, data: dict[str, Any],
    ) -> None:
        """Buffer a non-mention group message for later @mention context."""
        now = time.time()
        buf = self._context_buffer.setdefault(chat_id, [])
        # Evict expired entries
        buf[:] = [(t, s, mt, c, m) for t, s, mt, c, m in buf if now - t < _CONTEXT_BUFFER_TTL]
        # Capacity cap
        if len(buf) >= _CONTEXT_BUFFER_MAX:
            buf.pop(0)
        buf.append((now, sender, msg_type, content, {
            "file_name": data.get("file_name"),
            "file_size": data.get("file_size"),
            "file_path": data.get("file_path"),
            "url": data.get("url"),
            "title": data.get("title"),
        }))
        logger.debug("Buffered {} from {} in [{}] ({} buffered)", msg_type, sender, chat_id, len(buf))

    def _flush_context(self, chat_id: str) -> str:
        """Pop and format all buffered context for *chat_id*."""
        buf = self._context_buffer.pop(chat_id, [])
        if not buf:
            return ""
        now = time.time()
        lines = []
        for ts, sender, msg_type, content, meta in buf:
            if now - ts > _CONTEXT_BUFFER_TTL:
                continue
            line = self._format_context_line(sender, msg_type, content, meta)
            if line:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_context_line(sender: str, msg_type: str, content: str, meta: dict) -> str:
        """Format one buffered message into a human-readable line."""
        if msg_type == "text":
            return f"[{sender}] {content}"
        if msg_type == "file":
            name = meta.get("file_name") or content
            size = meta.get("file_size")
            path = meta.get("file_path") or ""
            size_str = f" ({WeChatChannel._fmt_size(size)})" if size else ""
            path_str = f" [path: {path}]" if path else ""
            return f"[{sender} 发送了文件] {name}{size_str}{path_str}"
        if msg_type == "link":
            title = meta.get("title") or content
            url = meta.get("url") or ""
            return f"[{sender} 分享了链接] {title}" + (f" ({url})" if url else "")
        if msg_type == "image":
            return f"[{sender} 发送了图片]"
        if msg_type == "video":
            return f"[{sender} 发送了视频]"
        if msg_type == "quote":
            return f"[{sender} 引用回复] {content}"
        return f"[{sender} 发送了{msg_type}消息] {content[:50]}"

    @staticmethod
    def _fmt_size(n: int | None) -> str:
        if not n:
            return ""
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n / 1024 / 1024:.1f}MB"
