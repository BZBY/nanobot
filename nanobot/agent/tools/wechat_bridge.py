"""WeChat bridge tools for AI-driven operations via MaaWxAuto bridge REST API."""

from typing import Any

from nanobot.agent.tools.base import Tool

_DEFAULT_BRIDGE = "http://127.0.0.1:9574"


class WeChatListSessionsTool(Tool):
    """List visible WeChat chat sessions."""

    def __init__(self, bridge_url: str = _DEFAULT_BRIDGE):
        self._base = bridge_url.rstrip("/")

    @property
    def name(self) -> str:
        return "wechat_list_sessions"

    @property
    def description(self) -> str:
        return (
            "List all visible WeChat chat sessions (contacts and groups). "
            "Returns session names which can be used as chat_id for sending messages."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base}/sessions")
                r.raise_for_status()
            sessions = r.json()
            if not sessions:
                return "No WeChat sessions visible."
            lines = [f"- {s['name']} (unread: {s.get('unread', 0)})" for s in sessions]
            return f"WeChat sessions ({len(sessions)}):\n" + "\n".join(lines)
        except Exception as e:
            return f"Error querying bridge: {e}"


class WeChatAddListenerTool(Tool):
    """Add a chat listener to monitor new messages."""

    def __init__(self, bridge_url: str = _DEFAULT_BRIDGE):
        self._base = bridge_url.rstrip("/")

    @property
    def name(self) -> str:
        return "wechat_add_listener"

    @property
    def description(self) -> str:
        return (
            "Start monitoring a WeChat chat for new messages. "
            "The chat will be added to the listen list and new messages "
            "will be forwarded to the bot automatically."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "Name of the chat/contact/group to monitor",
                },
            },
            "required": ["chat_id"],
        }

    async def execute(self, chat_id: str, **kwargs: Any) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{self._base}/listeners/add",
                    json={"chat_id": chat_id},
                )
                r.raise_for_status()
            data = r.json()
            if data.get("success"):
                return f"Now listening to '{chat_id}': {data.get('message', 'OK')}"
            return f"Failed to add listener: {data.get('message', 'unknown error')}"
        except Exception as e:
            return f"Error: {e}"


class WeChatHealthTool(Tool):
    """Check MaaWxAuto bridge health status."""

    def __init__(self, bridge_url: str = _DEFAULT_BRIDGE):
        self._base = bridge_url.rstrip("/")

    @property
    def name(self) -> str:
        return "wechat_health"

    @property
    def description(self) -> str:
        return (
            "Check the MaaWxAuto bridge health: whether WeChat is connected, "
            "DB is available, and monitoring is active."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base}/health")
                r.raise_for_status()
            data = r.json()
            lines = [
                f"Status: {data.get('status', 'unknown')}",
                f"WeChat connected: {data.get('wechat_connected', False)}",
                f"DB available: {data.get('db_available', False)}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Error: Bridge unreachable ({e})"


# All WeChat bridge tool classes for easy registration
WECHAT_BRIDGE_TOOLS = [
    WeChatListSessionsTool,
    WeChatAddListenerTool,
    WeChatHealthTool,
]
