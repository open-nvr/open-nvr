# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Main FastAPI application entry point.
Configures the application, middleware, and includes all routers.
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import settings

# Auto-provision imports
from core.database import SessionLocal, init_db

# Import logging configuration
from core.logging_config import main_logger, setup_logging

# Import request logging middleware
from middleware import RequestLoggingMiddleware
from models import (
    Camera as _Camera,
    CameraConfig as _CameraConfig,
    Permission as _Permission,
)

# Added streams and camera-config routers
from routers import (
    ai_detection_results,
    ai_model_management,
    ai_models,
    audit_logs,
    auth,
    camera_config,
    cameras,
    cloud as cloud_router,
    cloud_inference,
    cloud_providers,
    cloud_streaming,
    compliance,
    events as events_router,
    firmware as firmware_router,
    general,
    integrations,
    media_source,
    mediamtx_admin,
    mediamtx_hooks,
    network as network_router,
    onvif as onvif_router,
    password_policy,
    permissions,
    recordings,
    roles,
    security,
    streams,
    suricata_logs,
    suricata_stream,
    system,
    users,
    webrtc,
)
from scripts.init_db import create_initial_data
from services.mediamtx_admin_service import MediaMtxAdminService as _MtxAdmin

# FFmpeg-based RTSP proxy and recorder removed


# Application lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Initialize logging first
    setup_logging()
    main_logger.info("Logging system initialized")

    # Startup
    main_logger.info("Starting up FastAPI application...")
    try:
        # Initialize database tables
        init_db()
        main_logger.info("Database initialized successfully")
    except Exception as e:
        main_logger.error(f"Database initialization failed: {e}", exc_info=True)

    # Seed defaults (roles, permissions, admin user) and ensure admin user exists
    try:
        db = SessionLocal()
        try:
            if db.query(_Permission).count() == 0:
                main_logger.info("Seeding default roles and permissions...")
                create_initial_data()
                main_logger.info("Default data seeding completed")
            else:
                # Always ensure admin user exists on startup
                from core.auth import get_password_hash
                from models import Role as _Role, User as _User

                admin_user = (
                    db.query(_User)
                    .filter(_User.username == settings.default_admin_username)
                    .first()
                )
                if not admin_user:
                    main_logger.info(
                        "Admin user not found, creating default admin user..."
                    )
                    admin_role = db.query(_Role).filter(_Role.name == "admin").first()
                    if admin_role:
                        # Create admin with NO password - requires first-time setup
                        admin_user = _User(
                            username=settings.default_admin_username,
                            email=settings.default_admin_email,
                            hashed_password=get_password_hash(
                                "__UNSET__"
                            ),  # Placeholder - user must set password
                            first_name=settings.default_admin_first_name,
                            last_name=settings.default_admin_last_name,
                            is_active=True,
                            is_superuser=True,
                            password_set=False,  # Requires first-time setup
                            mfa_enabled=True,  # MFA enabled by default
                            role_id=admin_role.id,
                        )
                        db.add(admin_user)
                        db.commit()
                        main_logger.info(
                            f"Default admin user created ({settings.default_admin_username}) - First-time setup required"
                        )
                    else:
                        main_logger.warning(
                            "Admin role not found, running full seed..."
                        )
                        create_initial_data()
        finally:
            db.close()
    except Exception as e:
        main_logger.error(f"Seeding failed or skipped: {e}", exc_info=True)

    # Auto-provision MediaMTX paths from stored configs (if admin API configured)
    # This runs in background to avoid blocking application startup
    async def background_mediamtx_provisioning():
        """Background task for MediaMTX provisioning that doesn't block startup."""
        try:
            if settings.mediamtx_admin_api and settings.mediamtx_auto_provision:
                main_logger.info(
                    "[MTX] Admin API detected; starting background provisioning..."
                )

                # Add a small delay to allow MediaMTX to start if it's starting up
                import asyncio

                await asyncio.sleep(2)

                db = SessionLocal()
                try:
                    rows = (
                        db.query(_CameraConfig, _Camera)
                        .join(_Camera, _Camera.id == _CameraConfig.camera_id)
                        .all()
                    )
                    provisioned_count = 0
                    failed_count = 0

                    for cfg, cam in rows:
                        payload = {
                            "source_url": cfg.source_url,
                            "rtsp_transport": cfg.rtsp_transport,
                            "recording_enabled": cfg.recording_enabled,
                            "recording_path": cfg.recording_path,
                            "recording_segment_seconds": cfg.recording_segment_seconds,
                        }
                        try:
                            res = await _MtxAdmin.provision_path(
                                cam.id, cam.ip_address, payload
                            )
                            if res.get("status") == "success":
                                provisioned_count += 1
                            else:
                                failed_count += 1

                            main_logger.log_action(
                                "mediamtx.path_provision",
                                camera_id=cam.id,
                                message=f"MediaMTX path provisioned: path={res.get('path')} status={res.get('status')} http={res.get('http_status')}",
                                extra_data={"provision_result": res},
                            )
                        except Exception as e:
                            failed_count += 1
                            main_logger.warning(
                                f"[MTX] provision error camera_id={cam.id}: {e}",
                                extra={"camera_id": cam.id},
                            )
                            # Don't log full traceback for connection errors to reduce noise
                            if "ConnectionError" not in str(type(e)):
                                main_logger.error(
                                    f"[MTX] Unexpected error camera_id={cam.id}: {e}",
                                    extra={"camera_id": cam.id},
                                    exc_info=True,
                                )

                    main_logger.info(
                        f"[MTX] Background provisioning completed: {provisioned_count} success, {failed_count} failed"
                    )

                finally:
                    db.close()
            else:
                if not settings.mediamtx_admin_api:
                    main_logger.info(
                        "[MTX] Admin API not configured; skipping auto-provisioning"
                    )
                elif not settings.mediamtx_auto_provision:
                    main_logger.info("[MTX] Auto-provisioning disabled; skipping")
        except Exception as e:
            main_logger.error(
                f"[MTX] Background provisioning failed: {e}", exc_info=True
            )

    # Start background provisioning task
    import asyncio

    asyncio.create_task(background_mediamtx_provisioning())

    # Start retention cleanup scheduler
    async def background_retention_cleanup():
        """Background task for daily retention cleanup."""
        try:
            from services.retention_service import retention_service

            # Wait a bit before first cleanup (allow system to fully start)
            await asyncio.sleep(60)  # Wait 60 seconds after startup

            main_logger.info("Starting retention cleanup scheduler (runs daily)")

            while True:
                try:
                    main_logger.info("Running scheduled retention cleanup...")
                    stats = retention_service.cleanup_old_recordings()
                    main_logger.info(f"Retention cleanup completed: {stats}")
                except Exception as e:
                    main_logger.error(f"Retention cleanup failed: {e}", exc_info=True)

                # Wait 24 hours before next cleanup
                await asyncio.sleep(24 * 60 * 60)
        except Exception as e:
            main_logger.error(f"Retention cleanup scheduler failed: {e}", exc_info=True)

    asyncio.create_task(background_retention_cleanup())

    # FFmpeg-based RTSP proxy/recorder startup removed

    yield

    # Shutdown
    main_logger.info("Shutting down FastAPI application...")

    # Stop all running inference tasks
    try:
        from services.inference_manager import get_inference_manager

        inference_manager = get_inference_manager()
        await inference_manager.stop_all()
        main_logger.info("All inference tasks stopped")
    except Exception as e:
        main_logger.error(f"Error stopping inference tasks: {e}")

    # FFmpeg-based RTSP proxy/recorder cleanup removed


# Create FastAPI application
app = FastAPI(
    title="OpenNVR Surveillance System API",
    description="A comprehensive surveillance system API with user management, camera control, and recording management",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# Configure CORS middleware with security-hardened settings
# Parse comma-separated origins from config
cors_origins = [
    origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,  # Whitelist specific origins only
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "OPTIONS",
    ],  # Explicit methods
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Accept",
        "Origin",
        "X-Requested-With",
    ],  # Explicit headers
    expose_headers=["Content-Range", "Accept-Ranges", "Content-Length"],
)

# Add request logging middleware
app.add_middleware(RequestLoggingMiddleware)


# HTTPException handler (preserve proper status codes like 401/403/404)
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    main_logger.log_action(
        "http.exception",
        message=f"HTTP Exception: {exc.status_code} - {exc.detail}",
        extra_data={
            "status_code": exc.status_code,
            "detail": exc.detail,
            "url": str(request.url),
            "method": request.method,
        },
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


# Global exception handler (catch-all)
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Global exception handler for unhandled errors."""
    main_logger.error(
        f"Unhandled exception: {type(exc).__name__}: {exc}",
        extra={
            "url": str(request.url),
            "method": request.method,
            "ip_address": request.client.host if request.client else None,
            "user_agent": request.headers.get("user-agent"),
            "exception_type": type(exc).__name__,
        },
        exc_info=True,
    )
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# Health check endpoint
@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "OpenNVR Surveillance System API",
        "version": "1.0.0",
    }


# JWKS endpoint for MediaMTX JWT validation
@app.get("/.well-known/jwks.json")
async def get_jwks():
    """
    JWKS (JSON Web Key Set) endpoint for MediaMTX JWT authentication.

    MediaMTX fetches public keys from this endpoint to validate JWTs.
    This enables the backend to be the sole authority for stream access.
    """
    from services.mediamtx_jwt_service import MediaMtxJwtService

    return MediaMtxJwtService.get_jwks()


# Include routers
app.include_router(auth.router, prefix=settings.api_prefix)
app.include_router(users.router, prefix=settings.api_prefix)
app.include_router(cameras.router, prefix=settings.api_prefix)
app.include_router(streams.router, prefix=settings.api_prefix)
app.include_router(camera_config.router, prefix=settings.api_prefix)
app.include_router(roles.router, prefix=settings.api_prefix)
app.include_router(permissions.router, prefix=settings.api_prefix)
app.include_router(password_policy.router, prefix=settings.api_prefix)
app.include_router(security.router, prefix=settings.api_prefix)
app.include_router(webrtc.router, prefix=settings.api_prefix)
app.include_router(media_source.router, prefix=settings.api_prefix)
app.include_router(mediamtx_admin.router, prefix=settings.api_prefix)
app.include_router(mediamtx_hooks.router, prefix=settings.api_prefix)
app.include_router(general.router, prefix=settings.api_prefix)
app.include_router(audit_logs.router, prefix=settings.api_prefix)
app.include_router(recordings.router, prefix=settings.api_prefix)
app.include_router(onvif_router.router, prefix=settings.api_prefix)
app.include_router(network_router.router, prefix=settings.api_prefix)
app.include_router(integrations.router, prefix=settings.api_prefix)
app.include_router(cloud_router.router, prefix=settings.api_prefix)
app.include_router(cloud_streaming.router, prefix=settings.api_prefix)
app.include_router(firmware_router.router, prefix=settings.api_prefix)
app.include_router(ai_models.router, prefix=settings.api_prefix)
app.include_router(ai_model_management.router, prefix=settings.api_prefix)
app.include_router(ai_detection_results.router, prefix=settings.api_prefix)
app.include_router(cloud_providers.router, prefix=settings.api_prefix)
app.include_router(cloud_inference.router, prefix=settings.api_prefix)
app.include_router(compliance.router, prefix=settings.api_prefix)

app.include_router(suricata_logs, prefix=settings.api_prefix)
app.include_router(suricata_stream, prefix=settings.api_prefix)
app.include_router(system, prefix=settings.api_prefix)
app.include_router(events_router, prefix=settings.api_prefix)


# =============================================================================
# Frontend Static Files Serving (SPA Support)
# =============================================================================
# Determine path to frontend build (dist)
# In Docker: /app/app/dist
# Local: ../app/dist (relative to server/main.py)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIST = os.path.join(BASE_DIR, "app", "dist")

if os.path.exists(FRONTEND_DIST):
    main_logger.info(f"Serving frontend from {FRONTEND_DIST}")

    # Mount /assets explicitly (Vite default output folder)
    if os.path.exists(os.path.join(FRONTEND_DIST, "assets")):
        app.mount(
            "/assets",
            StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")),
            name="assets",
        )

    # Serve other static files (logos, manifest.json, robots.txt) from the root of /dist
    # We do this manually to avoid conflict with the SPA catch-all route.
    @app.get("/{file_path:path}")
    async def serve_static_or_spa(file_path: str):
        # 1. API routes are already handled above (FastAPI checks them first).

        # 2. Check if a physical file exists at the requested path in build/dist
        #    (This handles /logo.png, /manifest.json, /favicon.ico)
        full_path = os.path.join(FRONTEND_DIST, file_path)
        if os.path.isfile(full_path):
            return FileResponse(full_path)

        # 3. If no file found, and it's not an API route, assume it's a client-side route
        #    (e.g. /dashboard, /login) -> Serve index.html
        return FileResponse(os.path.join(FRONTEND_DIST, "index.html"))

else:
    main_logger.warning(
        f"Frontend build not found at {FRONTEND_DIST}. Serving API-only mode."
    )

    @app.get("/")
    def root():
        return {
            "message": "OpenNVR API is running (Frontend not found)",
            "docs": "/docs",
        }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=5,
    )
