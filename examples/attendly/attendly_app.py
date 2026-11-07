"""Attendly on OpenNVR — the application server.

One FastAPI process serves the API, the built React UI (same origin, so the
browser never needs CORS against core), and the RFC-0002 contract surface
core's App Catalog probes (``/health``, ``/manifest``, ``/state``). On boot it
self-registers with the catalog exactly like the Camera Agent does, with
``ui_mode="external"`` so the catalog's "Open app" button opens this UI in a
new tab.

    python attendly_app.py --port 9300

Environment: ``OPENNVR_URL``, ``KAIC_URL``, ``OPENNVR_INTERNAL_API_KEY`` (the
platform wiring, as the compose overlay sets it) and ``ATTENDLY_*`` (see
attendly/config.py).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from attendly import APP_ID, APP_VERSION
from attendly.config import Settings, get_settings
from attendly.gate import auth_middleware, is_api_path
from attendly.manifest import app_manifest, app_ui_url
from attendly.platform import get_platform_app, reset_platform_app
from attendly.routers import (
    attendance,
    auth,
    cameras,
    classrooms,
    courses,
    gates,
    meals,
    planner,
    recognition,
    registry,
    schedules,
    staff,
    staff_attendance,
    streams,
    students,
    video_registration,
)
from attendly.services.access import GateEngine
from attendly.services.engine import StreamEngine
from attendly.services.staff_attendance import StaffEngine
from attendly.storage.db import init_db
from attendly.utils.logging_utils import get_request_id, new_request_id, set_request_id

logger = logging.getLogger("attendly")

HERE = Path(__file__).resolve().parent
STARTED_AT = time.time()


def _setup_logging(settings: Settings) -> None:
    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s", force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).setLevel(level)


def contract_url(settings: Settings) -> str:
    """How core reaches our contract surface — the container's hostname
    on our port unless the operator said otherwise."""
    if settings.contract_url:
        return settings.contract_url.rstrip("/")
    return f"http://{socket.gethostname()}:{settings.port}"


def registration_payload(settings: Settings) -> Dict[str, Any]:
    return {"url": contract_url(settings), "manifest": app_manifest(settings)}


async def register_with_app_catalog(settings: Settings, *, attempts: int = 30, delay: float = 10.0) -> bool:
    """Best-effort ``POST /api/v1/apps/register`` with the site key — the
    platform seeing the app gates nothing; attendly runs the same without
    the catalog. Retries while core is still coming up."""
    base = settings.opennvr_url.rstrip("/")
    if not base:
        return False
    headers: Dict[str, str] = {}
    if settings.internal_api_key:
        headers["X-Internal-Api-Key"] = settings.internal_api_key
        headers["Authorization"] = f"Bearer {settings.internal_api_key}"
    payload = registration_payload(settings)
    for i in range(max(1, attempts)):
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                resp = await client.post(f"{base}/api/v1/apps/register", json=payload, headers=headers)
            if resp.status_code < 400:
                logger.info("registered with the App Catalog as %s (ui %s)", payload["url"], app_ui_url(settings))
                return True
            logger.warning("app-catalog registration rejected: HTTP %d %s", resp.status_code, resp.text[:200])
            if 400 <= resp.status_code < 500:
                return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("app-catalog registration failed (%s); retrying in %.0fs", exc, delay)
        await asyncio.sleep(delay)
    return False


def _web_dir() -> Optional[Path]:
    candidate = Path(os.environ.get("ATTENDLY_WEB_DIR") or (HERE / "web" / "dist"))
    return candidate if (candidate / "index.html").is_file() else None


def build_app(settings: Optional[Settings] = None, *, start_engine: bool = True, register: bool = True) -> FastAPI:
    settings = settings or get_settings()
    _setup_logging(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        logger.info("Attendly %s starting: db=%s auth=%s ui=%s", APP_VERSION, settings.database_url,
                    settings.auth_mode, app_ui_url(settings))
        if settings.auth_mode == "none" and settings.listen_host not in ("127.0.0.1", "localhost", "::1"):
            logger.warning("auth_mode is 'none' but the app listens on %s — every endpoint is open. "
                           "Use auth_mode: opennvr for any non-loopback deployment.", settings.listen_host)
        task = None
        if register and settings.opennvr_url:
            task = asyncio.create_task(register_with_app_catalog(settings), name="attendly-app-catalog-register")
        if start_engine:
            for name, eng in (("StreamEngine", StreamEngine), ("GateEngine", GateEngine), ("StaffEngine", StaffEngine)):
                try:
                    eng.get_shared().start()
                except Exception as exc:
                    logger.error("Failed to start %s: %s", name, exc)
        yield
        if task is not None and not task.done():
            task.cancel()
        for eng in (StreamEngine, GateEngine, StaffEngine):
            try:
                eng.get_shared().shutdown()
            except Exception:
                pass

    app = FastAPI(title="Attendly on OpenNVR", version=APP_VERSION, lifespan=lifespan,
                  description="Classroom attendance on OpenNVR cameras, detection and face recognition.")

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",") if o.strip()],
                           allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        req_id = new_request_id()
        set_request_id(req_id)
        try:
            response = await call_next(request)
            if request.url.path.startswith(("/api/", "/students", "/attendance", "/streams", "/registry", "/staff", "/gates", "/meals", "/departments")):
                logger.info("[req_id=%s] %s %s -> %s", req_id, request.method, request.url.path, response.status_code)
            return response
        finally:
            set_request_id(None)

    app.middleware("http")(auth_middleware)

    # ── Contract surface (open) — answered by the SDK's ContractApp ──
    reset_platform_app()
    platform = get_platform_app(settings)

    @app.get("/health")
    async def health():
        return platform.health()

    @app.get("/manifest")
    async def manifest():
        return platform.manifest_snapshot()

    @app.get("/state")
    async def state():
        return platform.state_snapshot()

    # ── API ─────────────────────────────────────────────────────────
    app.include_router(auth.router, prefix="/api/auth", tags=["auth"])
    app.include_router(cameras.router, prefix="/cameras", tags=["cameras"])
    app.include_router(students.router, prefix="/students", tags=["students"])
    app.include_router(recognition.router, prefix="/recognition", tags=["recognition"])
    app.include_router(attendance.router, prefix="/attendance", tags=["attendance"])
    app.include_router(streams.router, prefix="/streams", tags=["streams"])
    app.include_router(classrooms.router, prefix="/classrooms", tags=["classrooms"])
    app.include_router(courses.router, prefix="/courses", tags=["courses"])
    app.include_router(schedules.router, prefix="/schedules", tags=["schedules"])
    app.include_router(video_registration.router, prefix="/video_registration", tags=["video_registration"])
    app.include_router(registry.router, prefix="/registry", tags=["registry"])
    app.include_router(planner.router, prefix="/planner", tags=["planner"])
    app.include_router(staff.router, prefix="/staff", tags=["staff"])
    app.include_router(staff.departments_router, prefix="/departments", tags=["staff"])
    app.include_router(staff_attendance.router, prefix="/staff-attendance", tags=["staff"])
    app.include_router(gates.router, prefix="/gates", tags=["gates"])
    app.include_router(meals.router, prefix="/meals", tags=["meals"])

    @app.post("/classes/{class_id}/finalize")
    def finalize_class_attendance(class_id: str):
        from attendly.services import streams_service as svc

        return svc.finalize_attendance(class_id)

    # ── Errors ──────────────────────────────────────────────────────
    def _error(status_code: int, message, *, error: Optional[str] = None):
        payload: Dict[str, Any] = {"message": message, "detail": message}
        if error:
            payload["error"] = error
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        return JSONResponse(status_code=status_code, content=payload)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "message": exc.detail,
                                                                   "request_id": get_request_id()}, headers=exc.headers)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception("Unhandled error")
        return _error(500, "Internal server error", error=exc.__class__.__name__)

    # ── The web UI (same origin) ────────────────────────────────────
    web = _web_dir()
    if web is not None:
        if (web / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=str(web / "assets")), name="assets")
        index = web / "index.html"

        # Every method, not only GET: a ``POST /courses`` (no trailing
        # slash) used to match this route by path and fail with 405
        # before the slash redirect could happen.
        @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
        async def spa(path: str, request: Request):
            # The UI's pages (/attendance, /gates, /staff, /meals…) share
            # their first segment with API routers. A browser navigating
            # to a page asks for text/html; the UI's own API calls and
            # every other client ask for JSON — that is the difference.
            wants_page = request.method in ("GET", "HEAD") and "text/html" in request.headers.get("accept", "")
            # An API path that reached the catch-all is one without its
            # trailing slash (the UI sends ``/courses``): redirect to the
            # router's spelling — 307 keeps the method and the body.
            if is_api_path(f"/{path}") and not wants_page:
                if not path.endswith("/"):
                    url = request.url.replace(path=f"/{path}/")
                    return RedirectResponse(str(url), status_code=307)
                raise HTTPException(status_code=404, detail="Not found")
            if request.method not in ("GET", "HEAD"):
                raise HTTPException(status_code=405, detail="Method not allowed")
            target = (web / path).resolve() if path else index
            if path and target.is_file() and str(target).startswith(str(web.resolve())):
                return FileResponse(str(target))
            return FileResponse(str(index), headers={"Cache-Control": "no-store"})
    else:
        @app.get("/", include_in_schema=False)
        async def root():
            return {"app": APP_ID, "version": APP_VERSION, "ui": "not built (web/dist missing)", "docs": "/docs"}

    return app


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Attendly on OpenNVR")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    settings = get_settings()
    host = args.host or settings.listen_host
    port = args.port or settings.port
    import uvicorn

    uvicorn.run(build_app(settings), host=host, port=port, log_level=str(settings.log_level).lower())


app = None  # built lazily by main()/tests so settings can be overridden first

if __name__ == "__main__":
    main()
