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
KAI-C Service - Backend service to communicate with KAI-C connector

This service handles communication between the backend and KAI-C connector,
which then forwards requests to AI Adapter servers.
"""

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import quote as urlquote

import httpx

from core.logging_config import main_logger
from services.adapter_contract import build_infer_payload, flatten_infer_response
from services.frame_capture import PersistentCapturePool

# Frame-transport mode for the live inference loop.
#   "governed" (default) → AI Adapter Contract v1 over KAI-C's governed
#               /api/v1/infer/{adapter} surface: persistent-pool JPEG
#               bytes, base64 in the body (+ camera_id + internal key).
#               KAI-C applies sovereignty/fingerprint governance AND
#               publishes the result on NATS, so subscriber example apps
#               and the metrics tap see the server's own camera
#               inference. KAI-C seeds its v1 registry from
#               ADAPTER_REGISTRY at startup, so adapters that were
#               reachable at KAI-C boot are governed with no extra setup;
#               an adapter that came up later can be re-registered via
#               POST /api/v1/adapters/register.
#   "v1"      → same contract body, but POSTed to the legacy
#               /infer/local passthrough (no NATS, no audit, no
#               correlation IDs). Escape hatch for registry problems.
#   "legacy"  → original behaviour: write latest.jpg, send an
#               opennvr:// file URI, expect a flat response. Only works
#               with the in-tree app/ adapters over a shared volume.
_ADAPTER_CONTRACT_MODE = os.environ.get("OPENNVR_ADAPTER_CONTRACT", "governed").strip().lower()
if _ADAPTER_CONTRACT_MODE not in ("governed", "v1", "legacy"):
    # A typo must not silently demote inference to the ungoverned path.
    import logging

    logging.getLogger("main").warning(
        "OPENNVR_ADAPTER_CONTRACT=%r is not one of governed|v1|legacy; using 'governed'",
        _ADAPTER_CONTRACT_MODE,
    )
    _ADAPTER_CONTRACT_MODE = "governed"

try:
    import cv2

    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False
    main_logger.warning(
        "OpenCV (cv2) not available. RTSP frame capture will be disabled."
    )


class KaiCService:
    """Service to communicate with KAI-C HTTP service."""

    def __init__(self, kai_c_url: str = "http://localhost:8100"):
        """
        Initialize KAI-C service.

        Args:
            kai_c_url: Base URL of KAI-C HTTP service (default: http://localhost:8100)
        """
        self.kai_c_url = kai_c_url.rstrip("/")
        # Resolve frames directory: prefer FRAMES_DIR env var (set in Docker),
        # fall back to dev-layout path (workspace_root/ai-adapter/frames).
        import os
        env_frames = os.environ.get("FRAMES_DIR")
        if env_frames:
            self.frames_dir = Path(env_frames)
        else:
            workspace_root = Path(__file__).parent.parent.parent.parent
            self.frames_dir = workspace_root / "ai-adapter" / "frames"
        self.frames_dir.mkdir(exist_ok=True, parents=True)

        # Thread pool for blocking operations (RTSP capture)
        self.executor = ThreadPoolExecutor(max_workers=10)

        # Persistent per-camera RTSP capture pool. Keeps one capture open
        # per camera instead of reconnecting every frame, and returns
        # JPEG bytes in memory (no latest.jpg disk round-trip). Used by
        # the v1 contract path; the legacy URI path still uses
        # _capture_frame_sync for back-compat.
        self._capture_pool = PersistentCapturePool()

        # Async HTTP client for non-blocking requests
        self.http_client = httpx.AsyncClient(timeout=30.0)

        # Cached JWT for MediaMTX loopback-tap reads (see
        # _get_inference_mediamtx_jwt). Wildcard read scope, refreshed
        # on a sliding TTL so we don't pay the ~1ms RSA-sign cost on
        # every frame. ``None`` triggers a mint on first use.
        #
        # Thread safety: KaiCService is a singleton accessed from
        # multiple async inference loops via run_in_executor. The
        # token field is written without a lock — a benign race at
        # expiry can wastefully mint twice but never produce wrong
        # data. We accept that for the simplicity it buys; a real
        # lock would serialise every mint and per-request URL build.
        self._inference_jwt: str | None = None
        self._inference_jwt_expires_at: float = 0.0
        # Timestamp (time.monotonic) of the most recent
        # capture-failure-triggered invalidation. Used to short-circuit
        # mint-storm behaviour during sustained MediaMTX outages.
        self._inference_jwt_invalidated_at: float = 0.0

        main_logger.info(f"KaiCService initialized with KAI-C URL: {self.kai_c_url}")
        main_logger.info(f"Frames directory: {self.frames_dir}")

    async def check_kai_c_health(self) -> dict[str, Any]:
        """
        Check if KAI-C and its configured adapters are healthy asynchronously.

        Flow: Backend â†’ KAI-C â†’ (KAI-C checks its adapters)

        Returns:
            Dictionary with KAI-C and adapter health status
        """
        try:
            # Call KAI-C health check
            response = await self.http_client.get(
                f"{self.kai_c_url}/adapters/health",
                timeout=10.0,
                headers={"Accept": "application/json"},
            )
            if response.status_code == 200:
                return response.json()
            return {
                "kai_c_status": "error",
                "message": f"KAI-C returned {response.status_code}",
            }
        except Exception as e:
            main_logger.error(f"KAI-C health check failed: {e}")
            return {"kai_c_status": "error", "message": str(e)}

    async def get_capabilities(self) -> dict[str, Any]:
        """
        Fetch available capabilities from KAI-C asynchronously.

        KAI-C will query all its configured adapters and return combined capabilities.

        Flow: Backend â†’ KAI-C â†’ (KAI-C queries adapters) â†’ KAI-C â†’ Backend

        Returns:
            Dictionary with all available models, tasks, and capabilities
        """
        try:
            # Call KAI-C to get all capabilities
            response = await self.http_client.get(
                f"{self.kai_c_url}/capabilities",
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            main_logger.error(f"Failed to fetch capabilities from KAI-C: {e}")
            raise

    @staticmethod
    def _redact_rtsp_url_for_log(url: str) -> str:
        """Strip both query-string secrets and basic-auth userinfo from
        an RTSP URL for safe logging.

        Two secret-bearing parts of a URL we never want in log files:

        * ``?jwt=<token>`` — the MediaMTX loopback-tap token. Short-
          lived (60 minutes) but still grants wildcard read access
          until expiry.
        * ``user:password@`` — camera credentials embedded in the
          direct-pull URL. The previous logger format logged these
          unredacted, which was a pre-existing finding; stripping them
          here closes that gap at the same time.

        Returns a URL of the form ``scheme://[<redacted>@]host[:port]/path[?<redacted>]``.
        Best-effort parse — if ``urlparse`` can't make sense of the
        input we fall back to the query-string strip alone.
        """
        from urllib.parse import urlparse, urlunparse

        try:
            parsed = urlparse(url)
        except Exception:
            # Defensive — if parsing somehow fails, at least drop the
            # query string (cheap substring op, can't raise).
            if "?" in url:
                base, _, _ = url.partition("?")
                return f"{base}?<redacted>"
            return url

        netloc = parsed.hostname or ""
        if parsed.port is not None:
            netloc = f"{netloc}:{parsed.port}"
        if parsed.username or parsed.password:
            netloc = f"<redacted>@{netloc}"

        query = "<redacted>" if parsed.query else ""
        # Drop fragments too — they don't normally appear in RTSP URLs
        # but if one ever did, log discipline is still right.
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, query, "")
        )

    def _capture_frame_sync(self, rtsp_url: str, camera_id: int) -> str | None:
        """
        Synchronous frame capture (runs in thread pool).

        Args:
            rtsp_url: RTSP stream URL
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file, or None if capture failed
        """
        if not CV2_AVAILABLE:
            main_logger.error("OpenCV not available. Cannot capture frames from RTSP.")
            return None

        try:
            # Create camera-specific directory
            camera_dir = self.frames_dir / f"camera_{camera_id}"
            camera_dir.mkdir(exist_ok=True, parents=True)

            frame_path = camera_dir / "latest.jpg"

            # Capture frame from RTSP
            cap = cv2.VideoCapture(rtsp_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Reduce latency

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                main_logger.warning(
                    "Failed to capture frame from %s",
                    self._redact_rtsp_url_for_log(rtsp_url),
                )
                return None

            # Save frame
            cv2.imwrite(str(frame_path), frame)

            # Return opennvr:// URI format expected by AI Adapter
            return f"opennvr://frames/camera_{camera_id}/latest.jpg"

        except Exception as e:
            main_logger.error(f"Error capturing frame from RTSP: {e}", exc_info=True)
            return None

    async def capture_frame_from_rtsp(
        self, rtsp_url: str, camera_id: int
    ) -> str | None:
        """
        Capture a frame from RTSP stream asynchronously.

        When ``INFERENCE_USE_MEDIAMTX_TAP=true`` (default) and
        ``MEDIAMTX_RTSP_URL`` is configured, the actual capture happens
        from MediaMTX's loopback path for ``camera_id`` instead of
        ``rtsp_url`` — see ``_resolve_inference_rtsp_url`` for the
        rationale. ``rtsp_url`` is preserved as the fallback for the
        disabled-tap / distributed-deployment case.

        On capture failure with the tap active, we invalidate the
        cached JWT so the next call re-mints. Without this self-heal,
        a token that MediaMTX has rejected (clock skew, JWKS rotation,
        a MediaMTX restart between mint and use) would keep being
        replayed for the full 50-minute cache TTL — silently breaking
        inference for that whole window. Invalidating lets the loop
        recover on the very next polling cycle (~2 seconds at default
        interval) instead.

        Args:
            rtsp_url: Fallback RTSP stream URL (typically camera_config.
                source_url). Used as-is when the tap is disabled.
            camera_id: Camera ID for file naming and tap path resolution.

        Returns:
            Path to saved frame file, or None if capture failed
        """
        capture_url = self._resolve_inference_rtsp_url(camera_id, rtsp_url)
        tap_was_used = capture_url != rtsp_url
        # Run blocking capture in thread pool
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            self.executor, self._capture_frame_sync, capture_url, camera_id
        )
        if result is None and tap_was_used:
            # Don't strand inference on a stale token. cv2.VideoCapture
            # collapses all RTSP errors into a None return — we can't
            # distinguish 401 from "MediaMTX is down" from "camera
            # stream not yet active" from here. Invalidating on tap
            # failure self-heals the auth-error class of failures
            # without needing to parse cv2 / ffmpeg internals.
            #
            # The back-off timestamp is recorded so _get_inference_
            # mediamtx_jwt can skip subsequent mint attempts during a
            # sustained MediaMTX outage — without it, every inference
            # cycle across every camera would mint a fresh token only
            # to throw it away on the next capture failure, defeating
            # the cache. See _INFERENCE_JWT_INVALIDATION_BACKOFF_SECONDS.
            self._inference_jwt = None
            self._inference_jwt_expires_at = 0.0
            self._inference_jwt_invalidated_at = time.monotonic()
        return result

    def _internal_api_key(self) -> str:
        """The shared secret KAI-C's governed ``/api/v1/*`` surface
        requires in the ``X-Internal-Api-Key`` header. Read lazily from
        settings (empty string in dev/single-host loopback, where KAI-C
        treats the key as optional)."""
        try:
            from core.config import settings
            return settings.internal_api_key or ""
        except Exception:
            import os
            return os.environ.get("INTERNAL_API_KEY", "")

    async def capture_frame_bytes(
        self, rtsp_url: str, camera_id: int
    ) -> bytes | None:
        """Capture the latest frame as JPEG bytes via the persistent
        capture pool — the v1 contract path. Same MediaMTX-tap URL
        resolution and stale-JWT self-heal as ``capture_frame_from_rtsp``,
        but it keeps the RTSP session open across frames and returns bytes
        in memory instead of writing ``latest.jpg`` and returning a URI.
        """
        capture_url = self._resolve_inference_rtsp_url(camera_id, rtsp_url)
        tap_was_used = capture_url != rtsp_url
        loop = asyncio.get_event_loop()
        jpeg = await loop.run_in_executor(
            self.executor, self._capture_pool.get_jpeg, camera_id, capture_url
        )
        if jpeg is None and tap_was_used:
            # Same self-heal rationale as capture_frame_from_rtsp: a
            # rejected/rotated MediaMTX token surfaces here as a capture
            # failure, so drop the cached JWT to re-mint next cycle. The
            # pool will reopen the capture with the fresh ?jwt= URL.
            self._inference_jwt = None
            self._inference_jwt_expires_at = 0.0
            self._inference_jwt_invalidated_at = time.monotonic()
        return jpeg

    # JWT cache TTL knobs — the token MediaMtxJwtService mints has the
    # lifetime below; we refresh 10 minutes before that boundary to
    # leave margin for clock skew between processes and avoid a flurry
    # of last-second refreshes if many cameras fire inference around
    # the same time. Refresh is derived from lifetime so the two stay
    # in sync if either knob changes.
    _INFERENCE_JWT_LIFETIME_MINUTES: int = 60
    _INFERENCE_JWT_REFRESH_AT_SECONDS: int = (_INFERENCE_JWT_LIFETIME_MINUTES - 10) * 60

    # Back-off after a tap-capture failure that invalidated the JWT
    # cache. Without this, a sustained MediaMTX outage causes every
    # inference cycle to mint a fresh token (~1ms RSA-sign each) only
    # to throw it away on the next capture failure — a "mint storm"
    # that defeats the whole point of caching. After an invalidation
    # we skip the next mint for this many seconds so the loop stays
    # cheap during the outage.
    _INFERENCE_JWT_INVALIDATION_BACKOFF_SECONDS: float = 30.0

    def _get_inference_mediamtx_jwt(self) -> str | None:
        """Mint (or return a cached) JWT granting wildcard read access
        to MediaMTX for the inference loopback tap.

        MediaMTX's ``authMethod: jwt`` global setting means every
        listener — including the plaintext loopback :8554 — requires a
        signed token. We mint one with the system identity ``kai-c-
        inference`` and an explicit regex-wildcard ``read`` scope
        (``~.*``), cache it for ``_INFERENCE_JWT_REFRESH_AT_SECONDS``,
        and append it to RTSP URLs as ``?jwt=<token>``.
        ``authJWTInHTTPQuery: true`` in mediamtx.docker.yml is what
        lets the token ride on the URL — ffmpeg / OpenCV's RTSP client
        forwards the query string through unchanged.

        Why an explicit ``~.*`` regex instead of "let MediaMTX default
        an absent path to wildcard": the latter behavior is documented
        inconsistently in the MediaMTX permissions model, and a tiny
        bit of explicitness here is cheaper than the on-call page
        from "MediaMTX 1.16 changed default-path semantics".

        Returns ``None`` if the JWT service can't mint (missing keys,
        crypto error). Callers fall back to the non-tap URL in that
        case so the inference path degrades gracefully instead of
        breaking entirely.
        """
        now = time.monotonic()
        if self._inference_jwt is not None and now < self._inference_jwt_expires_at:
            return self._inference_jwt

        # Back-off after a recent invalidation. See
        # _INFERENCE_JWT_INVALIDATION_BACKOFF_SECONDS for the rationale
        # (avoiding mint-storm during sustained MediaMTX outage).
        if (
            self._inference_jwt_invalidated_at > 0.0
            and (now - self._inference_jwt_invalidated_at)
            < self._INFERENCE_JWT_INVALIDATION_BACKOFF_SECONDS
        ):
            return None

        try:
            # Late import — MediaMtxJwtService eagerly loads keys at
            # first call, which we don't want happening at module
            # import time.
            from services.mediamtx_jwt_service import MediaMtxJwtService

            # Pass an explicit regex-wildcard path so the permission
            # MediaMTX sees is unambiguous: ``{"action": "read",
            # "path": "~.*"}`` matches every camera path under the
            # MediaMTX permissions regex grammar.
            token = MediaMtxJwtService.create_stream_token(
                user_id=0,
                username="kai-c-inference",
                camera_id=None,
                camera_path="~.*",
                actions=["read"],
                expiry_minutes=self._INFERENCE_JWT_LIFETIME_MINUTES,
            )
        except Exception as exc:
            main_logger.warning(
                "inference RTSP tap: failed to mint MediaMTX JWT (%s) — "
                "falling back to direct camera URL",
                exc,
            )
            return None

        self._inference_jwt = token
        self._inference_jwt_expires_at = now + self._INFERENCE_JWT_REFRESH_AT_SECONDS
        # A successful mint clears the back-off — if MediaMTX comes back
        # the next capture-failure-triggered invalidation should mint
        # immediately, not wait out the back-off.
        self._inference_jwt_invalidated_at = 0.0
        return token

    def _resolve_inference_rtsp_url(self, camera_id: int, fallback_url: str) -> str:
        """Return the URL the inference frame-capture loop should read.

        When ``INFERENCE_USE_MEDIAMTX_TAP=true`` (the default) and
        ``MEDIAMTX_RTSP_URL`` is configured, this returns the MediaMTX
        loopback path for the camera — e.g.
        ``rtsp://mediamtx:8554/cam-42?jwt=<token>``. Reading frames from
        MediaMTX's already-active publisher session avoids opening a
        second concurrent RTSP connection to the camera (which many
        consumer cameras refuse) and avoids the per-frame TLS overhead
        that ``rtsps://mediamtx:8322`` would impose on a same-kernel
        hop.

        When the tap is disabled, MediaMTX URL isn't configured, or the
        JWT mint fails, falls back to the caller's URL (typically the
        camera's raw RTSP URL from camera_config.source_url). The JWT
        failure path keeps inference working at the cost of the
        optimization — a degraded mode is better than no inference.

        See docs/SECURITY_ARCHITECTURE.md §"RTSP encryption posture"
        for the trust-boundary rationale.
        """
        # Late import to avoid pulling the full settings module into
        # KaiCService's import graph at module import time.
        from core.config import settings

        if not settings.inference_use_mediamtx_tap:
            return fallback_url

        base = settings.mediamtx_rtsp_url
        if not base:
            return fallback_url

        # MediaMTX path naming has two modes (see _build_stream_name in
        # stream_service.py): ``id`` → ``cam-{camera_id}`` and ``ip`` →
        # ``cam-{ip_with_dots_to_underscores}``. The resolver only has
        # ``camera_id`` in scope, so we can construct a correct tap URL
        # only under ``id`` mode. Under ``ip`` mode we'd have to wire
        # camera_ip through every caller of capture_frame_from_rtsp —
        # a non-trivial cross-file refactor we defer past v0.1.
        # Degrade safely: skip the tap in ``ip`` mode so inference
        # still works (using the camera URL fallback) instead of
        # serving a 404 to MediaMTX.
        path_mode = (getattr(settings, "mediamtx_path_mode", "id") or "id").lower()
        if path_mode != "id":
            main_logger.debug(
                "inference RTSP tap disabled: mediamtx_path_mode=%r (only 'id' "
                "supported in v0.1; falling back to direct camera URL)",
                path_mode,
            )
            return fallback_url

        token = self._get_inference_mediamtx_jwt()
        if not token:
            # JWT mint failed — degrade to direct camera URL rather than
            # serve a URL MediaMTX will refuse.
            return fallback_url

        prefix = settings.mediamtx_stream_prefix or "cam-"
        # urlquote the token defensively. JWTs are header.payload.signature
        # where each part is base64url (chars A-Za-z0-9-_); the only
        # reserved char we'd realistically see is the literal "." that
        # separates the three parts. ``safe='.'`` keeps the structural
        # dots un-encoded so the URL reads cleanly in logs and matches
        # what every other JWT-in-URL client sends.
        tap_url = f"{base.rstrip('/')}/{prefix}{camera_id}?jwt={urlquote(token, safe='.')}"
        main_logger.debug(
            "inference RTSP tap: camera_id=%s tap=%s (was %s)",
            camera_id,
            tap_url.split('?', 1)[0],  # log without token
            fallback_url,
        )
        return tap_url

    async def process_inference(
        self,
        camera_id: int,
        rtsp_url: str,
        model_name: str,
        task: str,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Process inference request asynchronously: capture frame and send to KAI-C.

        KAI-C will route to the correct AI Adapter based on model_name.

        Flow: Server â†’ KAI-C â†’ (KAI-C routes to correct adapter) â†’ KAI-C â†’ Server

        Args:
            camera_id: Camera ID
            rtsp_url: RTSP stream URL — typically the camera's raw RTSP
                URL from camera_config.source_url. When the MediaMTX
                loopback tap is enabled (default), the actual capture
                happens from MediaMTX's path for this camera instead;
                ``rtsp_url`` is only used as the fallback for the
                disabled-tap / distributed-deployment case.
            model_name: Model name (e.g., yolov8, yolov11) - KAI-C routes based on this
            task: Task name (e.g., person_detection, person_counting)
            options: Additional options/parameters

        Returns:
            Inference result via KAI-C
        """
        try:
            # Three transports (see _ADAPTER_CONTRACT_MODE). All swap to
            # the MediaMTX loopback tap when enabled (the local-edge
            # default) — see _resolve_inference_rtsp_url.
            #
            #  governed: persistent-pool JPEG bytes → contract-v1 body
            #    (+ camera_id) → POST /api/v1/infer/{model} with the
            #    internal key. KAI-C applies sovereignty/fingerprint
            #    governance AND publishes the result on NATS, so the
            #    subscriber example apps (occupancy / loitering /
            #    line-crossing / abandoned-object) see the server's own
            #    camera inference without a separate producer app
            #    running. Returns the adapter body directly.
            #    (footage-search used to be in that list; since 2.0.0 it
            #    subscribes to nothing and reads the event store.)
            #
            #  v1 (default): same contract body → POST /infer/local
            #    (legacy passthrough; no NATS/governance). Safe when the
            #    v1 registry isn't configured. Returns {status, response}.
            #
            #  legacy: write latest.jpg, send an opennvr:// file URI to
            #    /infer/local. Only the in-tree app/ adapters understand
            #    this, and it needs a shared frames volume.
            if _ADAPTER_CONTRACT_MODE == "legacy":
                frame_uri = await self.capture_frame_from_rtsp(rtsp_url, camera_id)
                if not frame_uri:
                    return {
                        "status": "error",
                        "message": "Failed to capture frame from RTSP stream",
                    }
                payload = {
                    "task": task,
                    "input": {"frame": {"uri": frame_uri}, "params": options or {}},
                }
                endpoint = f"{self.kai_c_url}/infer/local"
                headers = {"Content-Type": "application/json", "Accept": "application/json"}
                response_wrapped = True
                main_logger.debug(
                    "inference (legacy URI): camera=%s task=%s frame=%s",
                    camera_id, task, frame_uri,
                )
            else:
                jpeg = await self.capture_frame_bytes(rtsp_url, camera_id)
                if not jpeg:
                    return {
                        "status": "error",
                        "message": "Failed to capture frame from RTSP stream",
                    }
                if _ADAPTER_CONTRACT_MODE == "governed":
                    # camera_id is read by KAI-C's v1 route for the NATS
                    # subject + audit; thread it through as a string so
                    # subscriber subjects line up.
                    params = dict(options or {})
                    params["camera_id"] = str(camera_id)
                    payload = build_infer_payload(task=task, jpeg_bytes=jpeg, params=params)
                    endpoint = f"{self.kai_c_url}/api/v1/infer/{model_name}"
                    headers = {
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "X-Internal-Api-Key": self._internal_api_key(),
                    }
                    response_wrapped = False
                else:
                    payload = build_infer_payload(
                        task=task, jpeg_bytes=jpeg, params=options or {},
                    )
                    endpoint = f"{self.kai_c_url}/infer/local"
                    headers = {"Content-Type": "application/json", "Accept": "application/json"}
                    response_wrapped = True
                main_logger.debug(
                    "inference (%s): camera=%s task=%s frame_bytes=%d",
                    _ADAPTER_CONTRACT_MODE, camera_id, task, len(jpeg),
                )

            # Send async HTTP POST request to KAI-C, which forwards to the
            # AI Adapter.
            response = await self.http_client.post(endpoint, json=payload, headers=headers)

            if response.status_code != 200:
                error_text = response.text
                main_logger.error(f"KAI-C request failed: {error_text}")
                return {
                    "status": "error",
                    "message": f"KAI-C service failed: {error_text}",
                }

            result = response.json()

            # The legacy /infer/local wraps the adapter body in
            # {status, response}; the governed v1 route returns the
            # adapter body directly. Unwrap accordingly.
            if response_wrapped and isinstance(result, dict) and result.get("status") == "error":
                return {
                    "status": "error",
                    "message": result.get("message", "Unknown error from KAI-C"),
                }
            adapter_response = result.get("response", result) if response_wrapped else result

            # Translate the adapter's response. Under the contract the
            # adapter returns a structured ``{"result": {...}}`` body;
            # flatten_infer_response bridges it to the flat shape the
            # inference loop persists (label / confidence / bbox / count /
            # caption / latency_ms). It passes a legacy flat response
            # through unchanged, so a mixed deployment still works.
            if _ADAPTER_CONTRACT_MODE != "legacy" and isinstance(adapter_response, dict):
                adapter_response = flatten_infer_response(adapter_response)

            return {
                "status": "success",
                "camera_id": camera_id,
                "model_used": result.get("model_used", model_name),
                "task": task,
                "response": adapter_response,
            }

        except httpx.RequestError as e:
            main_logger.error(f"Failed to connect to KAI-C service: {e}", exc_info=True)
            return {
                "status": "error",
                "message": f"Cannot connect to KAI-C service at {self.kai_c_url}. Please ensure KAI-C is running.",
            }
        except Exception as e:
            main_logger.error(f"Inference processing failed: {e}", exc_info=True)
            return {"status": "error", "message": str(e)}

    def _extract_frame_from_video_sync(
        self, video_path: str, frame_number: int, camera_id: int
    ) -> str | None:
        """
        Extract a specific frame from video file (synchronous).

        Args:
            video_path: Absolute path to video file
            frame_number: Frame number to extract (0-indexed)
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file (opennvr:// URI), or None if extraction failed
        """
        if not CV2_AVAILABLE:
            main_logger.error("OpenCV not available. Cannot extract frames from video.")
            return None

        try:
            # Create camera-specific directory
            camera_dir = self.frames_dir / f"camera_{camera_id}"
            camera_dir.mkdir(exist_ok=True, parents=True)

            frame_path = camera_dir / f"frame_{frame_number}.jpg"

            # Open video file
            cap = cv2.VideoCapture(str(video_path))

            # Set frame position
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_number)

            ret, frame = cap.read()
            cap.release()

            if not ret or frame is None:
                main_logger.warning(
                    f"Failed to extract frame {frame_number} from {video_path}"
                )
                return None

            # Save frame
            cv2.imwrite(str(frame_path), frame)

            # Return opennvr:// URI format expected by AI Adapter
            return f"opennvr://frames/camera_{camera_id}/frame_{frame_number}.jpg"

        except Exception as e:
            main_logger.error(f"Error extracting frame from video: {e}", exc_info=True)
            return None

    async def extract_frame_from_video(
        self, video_path: str, frame_number: int, camera_id: int
    ) -> str | None:
        """
        Extract a frame from video file asynchronously.

        Args:
            video_path: Absolute path to video file
            frame_number: Frame number to extract (0-indexed)
            camera_id: Camera ID for file naming

        Returns:
            Path to saved frame file, or None if extraction failed
        """
        # Run blocking extraction in thread pool
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self.executor,
            self._extract_frame_from_video_sync,
            video_path,
            frame_number,
            camera_id,
        )

    async def process_recording_inference(
        self,
        camera_id: int,
        recording_path: str,
        model_name: str,
        task: str,
        frame_interval: int = 30,  # Process every Nth frame (default: 1 fps at 30fps video)
        options: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Process inference on a recorded video file.

        Extracts frames at specified intervals and runs inference on each frame.

        Args:
            camera_id: Camera ID
            recording_path: Relative path to recording file (e.g., "cam-95/2025/12/...")
            model_name: Model name (e.g., yolov8, yolov11)
            task: Task name (e.g., person_detection, person_counting)
            frame_interval: Extract every Nth frame (default: 30 = ~1fps for 30fps video)
            options: Additional options/parameters

        Returns:
            List of inference results for all processed frames
        """
        if not CV2_AVAILABLE:
            return [
                {
                    "status": "error",
                    "message": "OpenCV not available. Cannot process video files.",
                }
            ]

        try:
            # Build absolute path to recording
            from core.database import SessionLocal
            from services.storage_service import get_effective_recordings_base_path

            db = SessionLocal()
            try:
                recordings_base = get_effective_recordings_base_path(db)
            finally:
                db.close()

            video_path = Path(recordings_base) / recording_path

            if not video_path.exists():
                return [
                    {
                        "status": "error",
                        "message": f"Recording not found: {recording_path}",
                    }
                ]

            main_logger.info(f"Processing recording: {video_path}")

            # Get video properties
            cap = cv2.VideoCapture(str(video_path))
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            cap.release()

            main_logger.info(f"Video info: {total_frames} frames, {fps:.2f} fps")

            results = []
            frames_to_process = range(0, total_frames, frame_interval)

            main_logger.info(
                f"Processing {len(frames_to_process)} frames (every {frame_interval} frames)"
            )

            # Process each frame
            for frame_num in frames_to_process:
                # Extract frame
                frame_uri = await self.extract_frame_from_video(
                    str(video_path), frame_num, camera_id
                )

                if not frame_uri:
                    main_logger.warning(f"Failed to extract frame {frame_num}")
                    continue

                # Prepare payload for KAI-C (correct format)
                payload = {
                    "task": task,
                    "input": {
                        "frame": {
                            "uri": frame_uri
                        },
                        "params": options or {}
                    }
                }

                # Send inference request
                try:
                    response = await self.http_client.post(
                        f"{self.kai_c_url}/infer",
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        timeout=30.0,
                    )
                    response.raise_for_status()
                    result = response.json()

                    # Add frame metadata
                    result["frame_number"] = frame_num
                    result["timestamp_seconds"] = frame_num / fps if fps > 0 else 0

                    results.append(result)

                except Exception as e:
                    main_logger.error(f"Inference failed for frame {frame_num}: {e}")
                    results.append(
                        {
                            "status": "error",
                            "frame_number": frame_num,
                            "message": str(e),
                        }
                    )

            main_logger.info(
                f"Completed processing {len(results)} frames from recording"
            )

            return results

        except Exception as e:
            main_logger.error(f"Error processing recording: {e}", exc_info=True)
            return [{"status": "error", "message": str(e)}]

    async def get_task_schema(self, task: str | None = None) -> dict[str, Any]:
        """
        Get schema documentation via KAI-C asynchronously.

        Flow: Backend â†’ KAI-C â†’ (KAI-C queries adapters)

        Args:
            task: Optional task name

        Returns:
            Schema dictionary
        """
        try:
            params = {"task": task} if task else {}
            response = await self.http_client.get(
                f"{self.kai_c_url}/schema",
                params=params,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            main_logger.error(f"Failed to fetch schema from KAI-C: {e}")
            raise

    async def get_fleet_metrics(self) -> dict[str, Any]:
        """Every adapter's metrics rollup in ONE call (KAI-C's
        /api/v1/adapters-metrics) — feeds the AI Adapters page's fleet
        strip without a per-adapter fan-out. Same auth/error contract
        as get_adapter_metrics."""
        headers = {"Accept": "application/json"}
        internal_key = self._internal_api_key()
        if internal_key:
            headers["X-Internal-Api-Key"] = internal_key
        response = await self.http_client.get(
            f"{self.kai_c_url}/api/v1/adapters-metrics",
            timeout=10.0,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    async def get_adapter_metrics(self, adapter_name: str) -> dict[str, Any]:
        """
        Fetch the windowed metrics rollup for one adapter from KAI-C's
        governed surface (observability spec §05).

        Flow: Backend → KAI-C → (KAI-C serves its in-memory rollup,
        fed by the /metrics scrape on the 60s registry poll)

        Args:
            adapter_name: The registered adapter name.

        Returns:
            The rollup dict (latency percentiles, outcome counts,
            saturation gauges, fingerprint-change timeline).

        Raises:
            httpx.HTTPStatusError: KAI-C answered non-2xx (404 for an
                unknown adapter — the router maps status codes).
            httpx.HTTPError: KAI-C unreachable.
        """
        headers = {"Accept": "application/json"}
        internal_key = self._internal_api_key()
        if internal_key:
            headers["X-Internal-Api-Key"] = internal_key
        response = await self.http_client.get(
            f"{self.kai_c_url}/api/v1/adapters/{urlquote(adapter_name, safe='')}/metrics",
            timeout=10.0,
            headers=headers,
        )
        response.raise_for_status()
        return response.json()

    # ── Adapter permission-approval proxy (§8 / §11) ────────────────
    #
    # Thin proxies onto KAI-C's governed permission endpoints. Same
    # X-Internal-Api-Key auth as get_adapter_metrics; the mutating calls
    # additionally forward an ``X-Actor`` header so KAI-C's own audit
    # trail records WHICH OpenNVR user made the grant/revoke. All raise
    # httpx errors verbatim so the router can map 404→404 / down→502.

    def _kai_c_headers(self, *, actor: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        internal_key = self._internal_api_key()
        if internal_key:
            headers["X-Internal-Api-Key"] = internal_key
        if actor:
            headers["X-Actor"] = actor
        return headers

    async def get_adapter_permissions(self, adapter_name: str) -> dict[str, Any]:
        """Fetch the permission view for one adapter (declared / granted
        / pending / approval_status). Raises httpx errors on failure."""
        response = await self.http_client.get(
            f"{self.kai_c_url}/api/v1/adapters/{urlquote(adapter_name, safe='')}/permissions",
            timeout=10.0,
            headers=self._kai_c_headers(),
        )
        response.raise_for_status()
        return response.json()

    async def grant_adapter_permissions(
        self, adapter_name: str, keys: list[str], actor: str | None = None
    ) -> dict[str, Any]:
        """Grant permission keys for an adapter. Returns the updated view."""
        response = await self.http_client.post(
            f"{self.kai_c_url}/api/v1/adapters/{urlquote(adapter_name, safe='')}/permissions/grant",
            json={"keys": keys},
            timeout=10.0,
            headers=self._kai_c_headers(actor=actor),
        )
        response.raise_for_status()
        return response.json()

    async def revoke_adapter_permissions(
        self, adapter_name: str, keys: list[str], actor: str | None = None
    ) -> dict[str, Any]:
        """Revoke permission keys for an adapter. Returns the updated view."""
        response = await self.http_client.post(
            f"{self.kai_c_url}/api/v1/adapters/{urlquote(adapter_name, safe='')}/permissions/revoke",
            json={"keys": keys},
            timeout=10.0,
            headers=self._kai_c_headers(actor=actor),
        )
        response.raise_for_status()
        return response.json()

    async def approve_all_adapter_permissions(
        self, adapter_name: str, actor: str | None = None
    ) -> dict[str, Any]:
        """Grant every declared key (the "approve" button). Returns the
        updated view."""
        response = await self.http_client.post(
            f"{self.kai_c_url}/api/v1/adapters/{urlquote(adapter_name, safe='')}/permissions/approve-all",
            timeout=10.0,
            headers=self._kai_c_headers(actor=actor),
        )
        response.raise_for_status()
        return response.json()

    async def close(self):
        """Cleanup resources."""
        await self.http_client.aclose()
        self.executor.shutdown(wait=False)


# Singleton instance
_kai_c_service: KaiCService | None = None


def get_kai_c_service() -> KaiCService:
    """Get singleton KAI-C service instance."""
    global _kai_c_service
    if _kai_c_service is None:
        from core.config import settings

        kai_c_url = getattr(settings, "kai_c_url", "http://localhost:8100")
        _kai_c_service = KaiCService(kai_c_url)
    return _kai_c_service
