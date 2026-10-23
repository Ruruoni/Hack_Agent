"""FastAPI application: REST + server-sent events. Binds to localhost by default; mutating requests from foreign
browser origins are rejected (protects against a malicious web page driving your local agent)."""
from __future__ import annotations

import json
import logging
import queue
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..core import AppContext
from ..datasets.manager import DatasetError
from ..llm.base import LLMError
from ..permissions.policy import PermissionDenied
from ..training.manager import TrainingError
from ..web_research.fetcher import WebPolicyError
from ..web_research.research import SearchError

log = logging.getLogger("nexus.api")
_FRONTEND = Path(__file__).resolve().parents[3] / "frontend"


def _frontend_dist() -> Path:
    """Freshly built UI (frontend/dist) wins; otherwise the committed prebuilt copy, so the UI works even when Node cannot build it."""
    for d in (_FRONTEND / "dist", _FRONTEND / "prebuilt"):
        if (d / "index.html").is_file():
            return d
    return _FRONTEND / "dist"


def sse(obj: dict, event: str | None = None) -> str:
    return (f"event: {event}\n" if event else "") + "data: " + json.dumps(obj, default=str) + "\n\n"


def create_app(ctx: AppContext | None = None) -> FastAPI:
    app = FastAPI(title="NEXUS AI", version=__version__)
    if ctx is None:
        ctx = AppContext()
    app.state.ctx = ctx
    s = ctx.settings

    app.add_middleware(CORSMiddleware, allow_origins=s.allow_cors_origins, allow_methods=["*"], allow_headers=["*"], allow_credentials=False)

    @app.middleware("http")
    async def origin_guard(request: Request, call_next):
        # DNS-rebinding guard: a hostile web page can point its own domain at 127.0.0.1; its requests then carry that domain
        # in the Host header (and a matching Origin). Only accept the host names this local app is meant to be reached by.
        host = request.headers.get("host", "")
        hostname = host[: host.rfind("]") + 1] if host.startswith("[") else host.rsplit(":", 1)[0] if ":" in host else host
        if hostname.lower() not in [h.lower() for h in ctx.settings.allowed_hosts]:
            ctx.db.audit("system", "api.blocked_host", {"host": host[:100], "path": request.url.path})
            return JSONResponse({"error": {"code": "host_blocked", "message": f"Requests addressed to '{host}' are not allowed.",
                                           "hint": "Open NEXUS at http://127.0.0.1:8000 or http://localhost:8000, or add the host name under Settings > Privacy > Allowed host names."}}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin not in ctx.settings.allow_cors_origins and origin != f"{request.url.scheme}://{request.headers.get('host', '')}":
                ctx.db.audit("system", "api.blocked_origin", {"origin": origin, "path": request.url.path})
                return JSONResponse({"error": {"code": "origin_blocked", "message": f"Requests from {origin} are not allowed."}}, status_code=403)
        return await call_next(request)

    # ---- error mapping -----------------------------------------------------------------
    @app.exception_handler(LLMError)
    async def _llm(_: Request, e: LLMError):
        return JSONResponse({"error": e.to_dict()}, status_code=503 if e.code in ("server_unreachable", "timeout") else 502 if e.code != "unsupported" else 501)

    @app.exception_handler(DatasetError)
    async def _ds(_: Request, e: DatasetError):
        return JSONResponse({"error": e.to_dict()}, status_code=400)

    @app.exception_handler(TrainingError)
    async def _tr(_: Request, e: TrainingError):
        return JSONResponse({"error": e.to_dict()}, status_code=400)

    @app.exception_handler(SearchError)
    async def _se(_: Request, e: SearchError):
        return JSONResponse({"error": {"code": "search", "message": str(e)}}, status_code=400)

    @app.exception_handler(WebPolicyError)
    async def _wp(_: Request, e: WebPolicyError):
        return JSONResponse({"error": {"code": "web_policy", "message": str(e)}}, status_code=403)

    @app.exception_handler(PermissionDenied)
    async def _pd(_: Request, e: PermissionDenied):
        return JSONResponse({"error": {"code": "forbidden", "message": str(e)}}, status_code=403)

    @app.exception_handler(PermissionError)
    async def _pe(_: Request, e: PermissionError):
        return JSONResponse({"error": {"code": "forbidden", "message": str(e)}}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(_: Request, e: ValueError):
        return JSONResponse({"error": {"code": "bad_request", "message": str(e)}}, status_code=400)

    @app.exception_handler(KeyError)
    async def _ke(_: Request, e: KeyError):
        return JSONResponse({"error": {"code": "bad_request", "message": f"missing field {e}"}}, status_code=400)

    from . import routes_agent, routes_chat, routes_data, routes_system
    for mod in (routes_system, routes_chat, routes_data, routes_agent):
        mod.register(app, ctx)

    # ---- live events ---------------------------------------------------------------------
    @app.get("/api/events")
    def events(request: Request):
        q = ctx.bus.subscribe()

        def gen():
            try:
                for ev in ctx.bus.recent(20):
                    yield sse(ev)
                last = time.time()
                while True:
                    try:
                        yield sse(q.get(timeout=1.0))
                        last = time.time()
                    except queue.Empty:
                        if time.time() - last > 15:
                            yield ": keep-alive\n\n"
                            last = time.time()
            finally:
                ctx.bus.unsubscribe(q)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ---- built frontend (production mode) -----------------------------------------------------
    FRONTEND_DIST = _frontend_dist()
    if (FRONTEND_DIST / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith("api/"):
                return JSONResponse({"error": {"code": "not_found", "message": "unknown API route"}}, status_code=404)
            f = FRONTEND_DIST / path
            return FileResponse(f if f.is_file() else FRONTEND_DIST / "index.html")
    return app
