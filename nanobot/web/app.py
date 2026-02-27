"""FastAPI web management UI for nanobot."""

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from nanobot.config.loader import get_data_dir, load_config, save_config
from nanobot.session.manager import SessionManager
from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule
from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader

_WEB_DIR = Path(__file__).parent


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(title="nanobot", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=str(_WEB_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")

    # --- Shared helpers ---

    config = load_config()
    workspace = config.workspace_path
    session_manager = SessionManager(workspace)
    cron_store_path = get_data_dir() / "cron" / "jobs.json"
    cron_service = CronService(cron_store_path)
    memory_store = MemoryStore(workspace)
    skills_loader = SkillsLoader(workspace)

    def _fmt_time(iso: str | None) -> str:
        if not iso:
            return ""
        try:
            dt = datetime.fromisoformat(iso)
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return iso or ""

    def _fmt_ts_ms(ms: int | None) -> str:
        if not ms:
            return ""
        try:
            return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return str(ms)

    # ========================================================================
    # Page routes (HTML)
    # ========================================================================

    @app.get("/", response_class=RedirectResponse)
    async def index():
        return RedirectResponse(url="/sessions", status_code=302)

    @app.get("/sessions", response_class=HTMLResponse)
    async def sessions_page(request: Request):
        raw = session_manager.list_sessions()
        sessions = []
        for s in raw:
            key = s.get("key", "")
            parts = key.split(":", 1)
            channel = parts[0] if len(parts) == 2 else ""
            chat_id = parts[1] if len(parts) == 2 else key
            sessions.append({
                "key": key,
                "channel": channel,
                "chat_id": chat_id,
                "created_at": _fmt_time(s.get("created_at")),
                "updated_at": _fmt_time(s.get("updated_at")),
            })
        return templates.TemplateResponse("sessions.html", {
            "request": request,
            "sessions": sessions,
            "page": "sessions",
        })

    @app.get("/sessions/{key:path}", response_class=HTMLResponse)
    async def session_detail_page(request: Request, key: str):
        session = session_manager._load(key)
        if not session:
            return templates.TemplateResponse("session_detail.html", {
                "request": request,
                "session_key": key,
                "messages": [],
                "page": "sessions",
                "error": "Session not found",
            })
        messages = []
        for m in session.messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            ts = m.get("timestamp", "")
            # Truncate very long tool results for display
            if role == "tool" and len(content) > 2000:
                content = content[:2000] + "\n... (truncated)"
            messages.append({
                "role": role,
                "content": content,
                "timestamp": _fmt_time(ts) if ts else "",
            })
        return templates.TemplateResponse("session_detail.html", {
            "request": request,
            "session_key": key,
            "messages": messages,
            "page": "sessions",
        })

    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request):
        cfg = load_config()
        config_json = json.dumps(cfg.model_dump(by_alias=True), indent=2, ensure_ascii=False)
        return templates.TemplateResponse("config.html", {
            "request": request,
            "config_json": config_json,
            "page": "config",
        })

    @app.get("/cron", response_class=HTMLResponse)
    async def cron_page(request: Request):
        jobs = cron_service.list_jobs(include_disabled=True)
        job_list = []
        for j in jobs:
            if j.schedule.kind == "every":
                sched = f"every {(j.schedule.every_ms or 0) // 1000}s"
            elif j.schedule.kind == "cron":
                sched = j.schedule.expr or ""
                if j.schedule.tz:
                    sched += f" ({j.schedule.tz})"
            else:
                sched = "one-time"
            job_list.append({
                "id": j.id,
                "name": j.name,
                "enabled": j.enabled,
                "schedule": sched,
                "message": j.payload.message,
                "deliver": j.payload.deliver,
                "channel": j.payload.channel or "",
                "to": j.payload.to or "",
                "next_run": _fmt_ts_ms(j.state.next_run_at_ms),
                "last_run": _fmt_ts_ms(j.state.last_run_at_ms),
                "last_status": j.state.last_status or "",
            })
        return templates.TemplateResponse("cron.html", {
            "request": request,
            "jobs": job_list,
            "page": "cron",
        })

    @app.get("/memory", response_class=HTMLResponse)
    async def memory_page(request: Request):
        long_term = memory_store.read_long_term()
        history = ""
        if memory_store.history_file.exists():
            history = memory_store.history_file.read_text(encoding="utf-8")
        return templates.TemplateResponse("memory.html", {
            "request": request,
            "long_term": long_term,
            "history": history,
            "page": "memory",
        })

    @app.get("/skills", response_class=HTMLResponse)
    async def skills_page(request: Request):
        all_skills = skills_loader.list_skills(filter_unavailable=False)
        skill_list = []
        for s in all_skills:
            meta = skills_loader.get_skill_metadata(s["name"])
            desc = ""
            if meta and meta.get("description"):
                desc = meta["description"]
            available = skills_loader._check_requirements(skills_loader._get_skill_meta(s["name"]))
            skill_list.append({
                "name": s["name"],
                "source": s["source"],
                "description": desc,
                "available": available,
            })
        return templates.TemplateResponse("skills.html", {
            "request": request,
            "skills": skill_list,
            "page": "skills",
        })

    # ========================================================================
    # API routes (JSON)
    # ========================================================================

    @app.post("/api/config")
    async def api_save_config(request: Request):
        try:
            body = await request.json()
            cfg = load_config()
            new_data = body.get("config")
            if not new_data:
                return JSONResponse({"error": "Missing 'config' field"}, status_code=400)
            if isinstance(new_data, str):
                new_data = json.loads(new_data)
            from nanobot.config.schema import Config
            new_cfg = Config.model_validate(new_data)
            save_config(new_cfg)
            return JSONResponse({"ok": True})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/api/cron")
    async def api_add_cron(request: Request):
        try:
            body = await request.json()
            name = body.get("name", "")
            message = body.get("message", "")
            schedule_type = body.get("schedule_type", "every")
            deliver = body.get("deliver", False)
            channel = body.get("channel") or None
            to = body.get("to") or None

            if schedule_type == "every":
                every_s = int(body.get("every_seconds", 60))
                schedule = CronSchedule(kind="every", every_ms=every_s * 1000)
            elif schedule_type == "cron":
                schedule = CronSchedule(
                    kind="cron",
                    expr=body.get("cron_expr", "0 * * * *"),
                    tz=body.get("tz") or None,
                )
            else:
                return JSONResponse({"error": "Invalid schedule_type"}, status_code=400)

            job = cron_service.add_job(
                name=name,
                schedule=schedule,
                message=message,
                deliver=deliver,
                channel=channel,
                to=to,
            )
            return JSONResponse({"ok": True, "id": job.id, "name": job.name})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.delete("/api/cron/{job_id}")
    async def api_delete_cron(job_id: str):
        if cron_service.remove_job(job_id):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Job not found"}, status_code=404)

    @app.put("/api/cron/{job_id}/toggle")
    async def api_toggle_cron(job_id: str):
        # Find current state and flip
        jobs = cron_service.list_jobs(include_disabled=True)
        target = next((j for j in jobs if j.id == job_id), None)
        if not target:
            return JSONResponse({"error": "Job not found"}, status_code=404)
        result = cron_service.enable_job(job_id, enabled=not target.enabled)
        if result:
            return JSONResponse({"ok": True, "enabled": result.enabled})
        return JSONResponse({"error": "Failed to toggle"}, status_code=500)

    @app.delete("/api/sessions/{key:path}")
    async def api_delete_session(key: str):
        path = session_manager._get_session_path(key)
        if path.exists():
            path.unlink()
            session_manager.invalidate(key)
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return app
