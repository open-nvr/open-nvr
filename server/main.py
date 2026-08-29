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

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from core.config import settings

# Auth dependency used to protect routers that ship without their own auth guards
# (ONVIF camera control and Suricata intrusion logs/stream were previously open).
from dependencies import get_current_active_user

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
    apps,
    audit_logs,
    auth,
    camera_config,
    camera_settings,
    cameras,
    internal_camera_agent,
    timeline_events,
    cloud as cloud_router,
    cloud_inference,
    cloud_providers,
    cloud_streaming,
    compliance,
    events as events_router,
    firmware as firmware_router,
    integrations,
    media_source,
    mediamtx_admin,
    mediamtx_hooks,
    network as network_router,
    onvif as onvif_router,
    orphaned_recordings,
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
    window_settings,
    device_firewall,
)
from scripts.init_db import create_initial_data
from services.mediamtx_admin_service import MediaMtxAdminService as _MtxAdmin

# The OpenNVR release this build is cut from. Surfaced in /health and in the
# OpenAPI schema, and quoted in bug reports (see SECURITY.md) — so it tracks the
# git tag, not the API shape. Bump it in the release commit.
__version__ = "0.1.4"

# FFmpeg-based RTSP proxy and recorder removed


# Application lifespan context manager
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Initialize logging first
    setup_logging()
    main_logger.info("Logging system initialized")

    # Record the offline-first posture in the audit log before DB init, so it's
    # captured even if the database is unreachable later. See V-009 / V-022.
    try:
        from core.policy import audit_boot_posture, current_posture

        posture = current_posture()
        main_logger.info(
            f"Boot policy: deployment_mode={posture['deployment_mode']} "
            f"ai_sovereignty={posture['ai_sovereignty']}"
        )
        audit_boot_posture()
        # Warn loudly if the retired ALLOW_REMOTE_MEDIAMTX env var is still set
        # (Pydantic would otherwise ignore it silently). See V-015.
        if os.environ.get("ALLOW_REMOTE_MEDIAMTX"):
            main_logger.warning(
                "ALLOW_REMOTE_MEDIAMTX is set but has been retired; there is no "
                "longer an opt-out. If MediaMTX runs behind a TLS reverse proxy, "
                "move the public URL to MEDIAMTX_EXTERNAL_* (see "
                "docs/SECURITY_ARCHITECTURE.md §2.2) and remove this env var."
            )
    except Exception as exc:
        main_logger.error(f"Failed to record boot posture: {exc}", exc_info=True)

    # Startup
    main_logger.info("Starting up FastAPI application...")
    try:
        # Initialize database tables
        init_db()
        main_logger.info("Database initialized successfully")
    except Exception as e:
        main_logger.error(f"Database initialization failed: {e}", exc_info=True)

    # Camera uuid backstop: create_all-bootstrapped databases skip the
    # Alembic backfill, and every identity check (markers, quarantine,
    # webhook gate) keys off cameras.uuid — fill any missing before the
    # reconciler/provisioning tasks start.
    try:
        db = SessionLocal()
        try:
            from services.camera_identity import ensure_camera_uuids

            ensure_camera_uuids(db)
        finally:
            db.close()
    except Exception as e:
        main_logger.error(f"Camera uuid backfill failed: {e}", exc_info=True)

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
                        # Re-seed: create admin with an unguessable placeholder
                        # hash and force the token-gated first-time-setup flow.
                        import secrets as _secrets
                        admin_user = _User(
                            username=settings.default_admin_username,
                            email=settings.default_admin_email,
                            hashed_password=get_password_hash(
                                _secrets.token_urlsafe(64)
                            ),
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

            # Idempotently seed permissions added in later releases (e.g.
            # apps.install); the full seed above only runs on an empty table.
            try:
                from models import Permission as _Permission2

                for _pname, _pdesc in (
                    (
                        "apps.install",
                        "Install/uninstall curated App Store apps",
                    ),
                ):
                    if (
                        db.query(_Permission2)
                        .filter(_Permission2.name == _pname)
                        .first()
                        is None
                    ):
                        db.add(_Permission2(name=_pname, description=_pdesc))
                        db.commit()
                        main_logger.info(
                            "Seeded new permission %r (upgrade path)", _pname
                        )
            except Exception:
                main_logger.warning(
                    "Upgrade-path permission seeding failed", exc_info=True
                )

            # If any user still needs first-time setup, arm a one-time token and
            # print it to stdout; it gates /auth/first-time-setup so nobody can
            # race the operator to claim the admin account. Re-armed each boot.
            try:
                from services.first_time_setup_service import maybe_arm

                token = maybe_arm(db)
                if token is not None:
                    banner = (
                        "\n"
                        "================================================================\n"
                        " OpenNVR first-time setup token (one-time use)\n"
                        "----------------------------------------------------------------\n"
                        f"  {token}\n"
                        "----------------------------------------------------------------\n"
                        " Pass this token in the `setup_token` field of\n"
                        " POST /auth/first-time-setup. It is consumed on first\n"
                        " successful use. Restart the server to mint a new one.\n"
                        "================================================================\n"
                    )
                    # Print first so it lands in container/journald stdout even
                    # if the structured logger is misconfigured; then log the
                    # ARMED event (without the token value) for audit.
                    print(banner, flush=True)
                    main_logger.info(
                        "First-time-setup token armed; see stdout for the value."
                    )
            except Exception as exc:
                # The server must still boot even if token arming fails;
                # without a token, /auth/first-time-setup will refuse all
                # attempts, which is the fail-closed posture we want.
                main_logger.error(
                    f"Failed to arm first-time-setup token: {exc}",
                    exc_info=True,
                )
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
                    # Paused (is_active=False) and binned (deleted_at set)
                    # cameras must stay unprovisioned across restarts —
                    # deactivate/delete tear their MediaMTX path down and this
                    # loop must not resurrect it.
                    rows = (
                        db.query(_CameraConfig, _Camera)
                        .join(_Camera, _Camera.id == _CameraConfig.camera_id)
                        .filter(_Camera.is_active == True)
                        .filter(_Camera.deleted_at.is_(None))
                        .all()
                    )
                    provisioned_count = 0
                    failed_count = 0

                    for cfg, cam in rows:
                        payload = {
                            # The camera row wins over the config row. cfg.source_url
                            # is a snapshot taken when the camera was first
                            # provisioned; reading it here is what used to revert an
                            # edited RTSP URL on every restart. Keeping it as the
                            # fallback lets this loop self-heal rows that drifted
                            # before the edit path started syncing it — no migration.
                            "source_url": cam.rtsp_url or cfg.source_url,
                            # Without this key _provision_substream falls back to
                            # deriving the sub URL from vendor convention, silently
                            # discarding an operator-stored one on every restart.
                            "substream_url": cam.substream_url,
                            "rtsp_transport": cfg.rtsp_transport,
                            "recording_enabled": cfg.recording_enabled,
                            "recording_path": cfg.recording_path,
                            "recording_segment_seconds": cfg.recording_segment_seconds,
                        }
                        try:
                            # upsert, not add: when MediaMTX outlives a server
                            # restart every path already exists, and a plain add
                            # leaves each one on whatever config it had.
                            res = await _MtxAdmin.upsert_path(
                                cam.id, cam.ip_address, payload
                            )
                            # The service returns "ok", never "success" — this
                            # counter used to log 0 successes on every startup.
                            if res.get("status") == "ok":
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
                    # Worker thread: the sweep does file I/O and batched DB
                    # deletes — it must never block the event loop.
                    stats = await asyncio.to_thread(
                        retention_service.cleanup_old_recordings
                    )
                    main_logger.info(f"Retention cleanup completed: {stats}")
                except Exception as e:
                    main_logger.error(f"Retention cleanup failed: {e}", exc_info=True)

                # Wait 24 hours before next cleanup
                await asyncio.sleep(24 * 60 * 60)
        except Exception as e:
            main_logger.error(f"Retention cleanup scheduler failed: {e}", exc_info=True)

    asyncio.create_task(background_retention_cleanup())

    # Disk-pressure loop: a near-full disk cannot wait for the daily sweep —
    # MediaMTX simply fails to write once space runs out. Every 5 minutes:
    # one cheap disk_usage check, and only when below min_free_space_gb an
    # oldest-first batched purge (with hysteresis) in a worker thread.
    async def background_disk_pressure():
        try:
            from services.event_bus_service import publish_system_alert
            from services.retention_service import retention_service

            await asyncio.sleep(120)
            while True:
                try:
                    stats = await asyncio.to_thread(
                        retention_service.check_disk_pressure
                    )
                    if stats:
                        main_logger.warning(f"Disk-pressure purge: {stats}")
                        # The purge thread persisted the SystemEvent rows;
                        # publish the live bus copy from async context.
                        await publish_system_alert(
                            alert_type="disk_pressure_purge",
                            state=None,
                            severity="critical" if stats.get("exhausted")
                            else "warning",
                            payload={
                                "description": (
                                    "Disk pressure purge deleted "
                                    f"{stats.get('deleted_files', 0)} files"
                                ),
                                **{
                                    k: v
                                    for k, v in stats.items()
                                    if isinstance(v, (int, float, bool))
                                },
                            },
                        )
                except Exception as e:
                    main_logger.error(f"Disk pressure check failed: {e}", exc_info=True)
                await asyncio.sleep(5 * 60)
        except Exception as e:
            main_logger.error(f"Disk pressure scheduler failed: {e}", exc_info=True)

    asyncio.create_task(background_disk_pressure())

    # Host resource monitor: CPU/RAM/disk sampling + edge-triggered alerts
    # (system_events + live bus). 15s cadence; work runs in a worker thread.
    async def background_system_monitor():
        try:
            from services.system_monitor_service import (
                SAMPLE_INTERVAL_SECONDS,
                STARTUP_DELAY_SECONDS,
                get_system_monitor,
            )

            monitor = get_system_monitor()
            await asyncio.sleep(STARTUP_DELAY_SECONDS)
            main_logger.info("Starting system resource monitor (15s cadence)")
            while True:
                try:
                    await monitor.run_once()
                except Exception as e:
                    main_logger.error(f"System monitor pass failed: {e}", exc_info=True)
                await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
        except Exception as e:
            main_logger.error(f"System monitor scheduler failed: {e}", exc_info=True)

    asyncio.create_task(background_system_monitor())

    # Recording-health watchdog: surfaces "camera silently stopped recording"
    # as a camera event within minutes instead of being discovered days later.
    async def background_recording_watchdog():
        try:
            from services.event_bus_service import publish_camera_event
            from services.recording_watchdog import (
                CHECK_INTERVAL_SECONDS,
                EVENT_TYPE as STALL_EVENT_TYPE,
                check_recording_health,
            )

            await asyncio.sleep(180)  # let cameras provision + first segments land
            while True:
                try:
                    result = await asyncio.to_thread(check_recording_health)
                    # Publish stall/recovery transitions live — the DB row
                    # alone only surfaces on the next manual page load.
                    for t in result.get("transitions", []):
                        await publish_camera_event(
                            camera_id=t["camera_id"],
                            event_type=STALL_EVENT_TYPE,
                            payload={
                                "state": t["event_state"],
                                "description": t["description"],
                                "camera_name": t.get("camera_name"),
                            },
                        )
                except Exception as e:
                    main_logger.error(f"Recording watchdog failed: {e}", exc_info=True)
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
        except Exception as e:
            main_logger.error(f"Recording watchdog scheduler failed: {e}", exc_info=True)

    asyncio.create_task(background_recording_watchdog())

    # Recordings-index reconciler: startup backfill of the recordings table
    # from the on-disk archive, then a periodic recent-window convergence
    # pass. Keeps the DB (the listing/timeline source of truth) honest even
    # across backend downtime and manual file deletion.
    async def background_recording_reconciler():
        try:
            from services.recording_reconciler import run_reconciler_loop

            await run_reconciler_loop()
        except Exception as e:
            main_logger.error(f"Recording reconciler failed: {e}", exc_info=True)

    asyncio.create_task(background_recording_reconciler())

    # RFC-0002 Phase 0: consume plate.recognized.v1 from the bus and write
    # plate_text onto timeline rows — producer-independent (EVENT_CONTRACTS.md
    # convergence). No NATS_URL / no nats-py degrades to the enrichment
    # fallback's synchronous writes; the loop itself never raises.
    async def background_plate_event_consumer():
        try:
            from services.plate_event_consumer import run_consumer_loop

            await run_consumer_loop()
        except Exception as e:
            main_logger.error(f"Plate event consumer failed: {e}", exc_info=True)

    asyncio.create_task(background_plate_event_consumer())

    # Start camera connectivity reconciler — safety net for the MediaMTX
    # runOnReady/runOnNotReady hooks (catches missed hooks and restarts of
    # either process). The loop delays its first pass internally so startup
    # provisioning can settle.
    try:
        from services.camera_status_service import get_camera_status_service

        camera_status_task = asyncio.create_task(
            get_camera_status_service().reconcile_loop()
        )
        main_logger.info("Camera status reconciler started")
    except Exception as e:
        camera_status_task = None
        main_logger.error(f"Failed to start camera status reconciler: {e}")

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

    # Stop all camera event-stream subscriptions
    try:
        from services.camera_event_manager import get_camera_event_manager

        await get_camera_event_manager().stop_all()
        main_logger.info("All camera event subscriptions stopped")
    except Exception as e:
        main_logger.error(f"Error stopping camera event subscriptions: {e}")

    # Close the shared MediaMTX async HTTP client
    try:
        from services import mediamtx_client

        await mediamtx_client.aclose()
    except Exception as e:
        main_logger.error(f"Error closing MediaMTX client: {e}")

    # Stop the camera connectivity reconciler
    if camera_status_task is not None:
        camera_status_task.cancel()

    # FFmpeg-based RTSP proxy/recorder cleanup removed


# Create FastAPI application
app = FastAPI(
    title="OpenNVR Surveillance System API",
    description="A comprehensive surveillance system API with user management, camera control, and recording management",
    version=__version__,
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

# Compress text responses (JSON, HTML, JS/CSS when served by uvicorn directly).
# Selective: media-plane paths (HLS byte-ranges, clip export) pass through
# untouched — video is already compressed and gzipping it wastes CPU on the
# hottest request path.
from middleware.compression import SelectiveGZipMiddleware

app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024)

# Device firewall — refuse API access from unapproved client IPs. Added before
# request logging so RequestLogging (added last = outermost) still records
# blocked attempts.
from middleware.device_firewall import DeviceFirewallMiddleware

app.add_middleware(DeviceFirewallMiddleware)

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
        "version": __version__,
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
app.include_router(camera_settings.router, prefix=settings.api_prefix)
app.include_router(internal_camera_agent.router, prefix=settings.api_prefix)
app.include_router(timeline_events.router, prefix=settings.api_prefix)
app.include_router(streams.router, prefix=settings.api_prefix)
app.include_router(camera_config.router, prefix=settings.api_prefix)
app.include_router(roles.router, prefix=settings.api_prefix)
app.include_router(permissions.router, prefix=settings.api_prefix)
app.include_router(password_policy.router, prefix=settings.api_prefix)
app.include_router(security.router, prefix=settings.api_prefix)
app.include_router(webrtc.router, prefix=settings.api_prefix)
app.include_router(window_settings.router, prefix=settings.api_prefix)
app.include_router(device_firewall.router, prefix=settings.api_prefix)
app.include_router(media_source.router, prefix=settings.api_prefix)
app.include_router(mediamtx_admin.router, prefix=settings.api_prefix)
app.include_router(mediamtx_hooks.router, prefix=settings.api_prefix)
app.include_router(audit_logs.router, prefix=settings.api_prefix)
app.include_router(recordings.router, prefix=settings.api_prefix)
app.include_router(orphaned_recordings.router, prefix=settings.api_prefix)
app.include_router(
    onvif_router.router,
    prefix=settings.api_prefix,
    dependencies=[Depends(get_current_active_user)],
)
app.include_router(network_router.router, prefix=settings.api_prefix)
app.include_router(integrations.router, prefix=settings.api_prefix)
app.include_router(cloud_router.router, prefix=settings.api_prefix)
app.include_router(cloud_streaming.router, prefix=settings.api_prefix)
app.include_router(firmware_router.router, prefix=settings.api_prefix)
app.include_router(ai_models.router, prefix=settings.api_prefix)
app.include_router(ai_model_management.router, prefix=settings.api_prefix)
app.include_router(ai_detection_results.router, prefix=settings.api_prefix)
app.include_router(apps.router, prefix=settings.api_prefix)
app.include_router(cloud_providers.router, prefix=settings.api_prefix)
app.include_router(cloud_inference.router, prefix=settings.api_prefix)
app.include_router(compliance.router, prefix=settings.api_prefix)

app.include_router(
    suricata_logs,
    prefix=settings.api_prefix,
    dependencies=[Depends(get_current_active_user)],
)
app.include_router(
    suricata_stream,
    prefix=settings.api_prefix,
    dependencies=[Depends(get_current_active_user)],
)
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

    # Mount /assets explicitly (Vite default output folder). Vite emits
    # content-hashed filenames, so these are immutable: tell the browser to
    # cache them for a year and never revalidate. A new deploy changes the
    # hash, which changes the URL, so stale caches can't survive an update.
    class ImmutableStaticFiles(StaticFiles):
        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            if response.status_code == 200:
                response.headers["Cache-Control"] = (
                    "public, max-age=31536000, immutable"
                )
            return response

    if os.path.exists(os.path.join(FRONTEND_DIST, "assets")):
        app.mount(
            "/assets",
            ImmutableStaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")),
            name="assets",
        )

    # Serve other static files (logos, manifest.json, robots.txt) from the root of /dist
    # We do this manually to avoid conflict with the SPA catch-all route.
    @app.get("/{file_path:path}")
    async def serve_static_or_spa(file_path: str):
        # 1. API routes are already handled above (FastAPI checks them first).

        # 2. Check if a physical file exists at the requested path in build/dist
        #    (This handles /logo.png, /manifest.json, /favicon.ico).
        #
        #    SECURITY: os.path.join happily escapes FRONTEND_DIST when
        #    file_path contains ``..`` segments or is absolute, and FileResponse
        #    would then serve arbitrary files (.env, JWT signing keys, DB creds)
        #    to an unauthenticated client. Resolve the path (collapsing ``..``
        #    and symlinks) and require it to stay inside FRONTEND_DIST before
        #    serving; anything that escapes falls through to the SPA index.
        dist_root = os.path.realpath(FRONTEND_DIST)
        requested = os.path.realpath(os.path.join(dist_root, file_path))
        if os.path.isfile(requested) and (
            requested == dist_root or requested.startswith(dist_root + os.sep)
        ):
            return FileResponse(requested)

        # 3. If no file found (or the path escaped the build dir), and it's not
        #    an API route, assume it's a client-side route
        #    (e.g. /dashboard, /login) -> Serve index.html.
        #    no-cache (revalidate, not "never store") so a new deploy's
        #    index.html — and with it the new hashed asset URLs — is picked
        #    up immediately.
        return FileResponse(
            os.path.join(dist_root, "index.html"),
            headers={"Cache-Control": "no-cache"},
        )

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
