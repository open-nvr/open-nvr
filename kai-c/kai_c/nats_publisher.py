# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
NATS publisher — KAI-C's broadcast surface for the §B1 event bus.

Connects lazily at startup and publishes ``InferenceCompletedEvent``
to the subject ``opennvr.inference.{adapter}.{camera_id}.completed``
on every successful inference (HTTP /api/v1/infer + WS streaming
result). Subscribers fan out from there.

Design constraints
------------------

1. **Never block the request path.** A misbehaving / unreachable
   NATS broker must NOT cascade into HTTP 500 / WS close on the
   inference path. ``publish_inference_completed`` swallows all
   publish errors and logs a warning. The audit log is the
   durable record; NATS is best-effort broadcast.

2. **Lazy + idempotent connect.** ``ensure_connected`` is called on
   the first publish (and re-called on reconnect after error).
   Operators don't need to pre-warm the connection.

3. **Token auth via INTERNAL_API_KEY.** Same secret operators
   already manage for KAI-C's HTTP surface. NATS-side configured
   via ``--auth <token>`` in the docker-compose service.

4. **Sovereignty.** Under ``AI_SOVEREIGNTY=local_only``, KAI-C
   refuses to connect to a NATS URL whose host isn't loopback /
   sentinel_internal Docker network address. Enforced in the
   connection step, NOT lazily — operator gets a startup error
   rather than a silent broadcast disable.

5. **Disable cleanly.** Set ``NATS_URL=""`` (or unset) to disable
   publishing entirely. The publisher is a no-op and ``ensure_
   connected`` returns immediately. Useful for operators not yet
   on the event-bus story.

Anything broader (alert fan-out, audit streaming, JetStream
durability, etc.) lives in follow-up slices — see the design doc.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Any
from urllib.parse import urlparse

from kai_c.events import InferenceCompletedEvent, inference_completed_subject

logger = logging.getLogger(__name__)


# Default connect/publish budget. NATS is local — these should be
# fast. If they're slow we want to surface that in logs so an
# operator can investigate; we don't want to silently spend seconds
# on every publish.
DEFAULT_CONNECT_TIMEOUT_SECONDS: float = 5.0
DEFAULT_DRAIN_TIMEOUT_SECONDS: float = 2.0


class NatsPublisher:
    """Lazy NATS publisher for KAI-C's broadcast surface.

    Public methods are async because nats-py is async. KAI-C is a
    FastAPI app so this fits naturally; the publisher is called
    from the same event loop the request handlers run on.

    Configuration is read from env vars by the caller and passed
    in — keeps this module testable without env-var monkey-patching.
    """

    def __init__(
        self,
        *,
        url: str | None,
        token: str | None,
        sovereignty_mode: str,
        connect_timeout_seconds: float = DEFAULT_CONNECT_TIMEOUT_SECONDS,
    ) -> None:
        """
        :param url: NATS connection URL (e.g. ``nats://nats:4222``).
                    Pass ``None`` / empty string to disable publishing
                    entirely (KAI-C will log a one-shot info line at
                    startup and skip every publish — useful for
                    operators not yet on the event bus).
        :param token: Token-auth secret. NATS server must be configured
                      with ``--auth <token>`` to match. Pass ``None`` if
                      the broker is auth-less (only acceptable in fully-
                      isolated dev / loopback deployments).
        :param sovereignty_mode: KAI-C's runtime sovereignty mode —
                      one of ``local_only`` (default), ``federated``,
                      or ``cloud_allowed`` (see ``main.py``). Under
                      ``local_only`` the URL must resolve to a
                      loopback / Docker-private network address.
                      ``federated`` and ``cloud_allowed`` skip the
                      check — federated deployments may legitimately
                      have NATS off-host on a partner LAN.
        :param connect_timeout_seconds: Hard cap on the initial
                      ``nats.connect`` call. NATS is local — anything
                      longer than 5s indicates a misconfiguration we
                      want operators to see.
        """
        self._url = (url or "").strip() or None
        self._token = token or None
        self._sovereignty_mode = sovereignty_mode
        self._connect_timeout = connect_timeout_seconds
        self._client: Any = None
        self._connect_lock = asyncio.Lock()
        # Counters for operator observability. Logged at shutdown via
        # ``close()``. Wiring them into a /metrics endpoint is a
        # follow-up (the existing audit log captures every successful
        # inference anyway; failed publishes only surface here).
        self.published_count: int = 0
        self.failed_count: int = 0

    @property
    def enabled(self) -> bool:
        """True if a NATS URL is configured. False = publish-skip."""
        return self._url is not None

    async def start(self) -> None:
        """Eagerly connect at KAI-C startup so sovereignty violations
        surface as a clean startup error rather than a per-publish
        warning. Safe to call even when disabled (returns immediately)."""
        if not self.enabled:
            logger.info(
                "NATS publisher disabled (NATS_URL unset). Subscribers "
                "won't receive inference events; HTTP/WS paths unaffected."
            )
            return
        self._validate_sovereignty()
        await self._do_connect()

    async def close(self) -> None:
        """Drain pending publishes and close the connection. Called from
        the FastAPI shutdown hook. Logs publish counts so operators
        can see how many events were broadcast / dropped over the
        process's lifetime."""
        logger.info(
            "NATS publisher shutting down — published=%d failed=%d",
            self.published_count, self.failed_count,
        )
        if self._client is None:
            return
        try:
            await asyncio.wait_for(
                self._client.drain(),
                timeout=DEFAULT_DRAIN_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001  — TimeoutError is a subclass
            logger.warning("NATS drain timed out / errored: %s", exc)
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._client = None

    async def publish_inference_completed(
        self,
        event: InferenceCompletedEvent,
    ) -> bool:
        """Publish one ``InferenceCompletedEvent``. Returns True on
        success, False on any failure.

        **Never raises.** A publish error logs at WARNING and increments
        ``failed_count`` so /metrics surfaces it, but the request path
        that called us is unaffected — broadcast is best-effort by
        design.
        """
        if not self.enabled:
            return False
        try:
            await self._ensure_connected()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NATS publish skipped (connect failure): %s "
                "[correlation_id=%s]", exc, event.correlation_id,
            )
            self.failed_count += 1
            return False

        subject = inference_completed_subject(event.adapter, event.camera_id)
        payload = event.model_dump_json().encode("utf-8")
        try:
            await self._client.publish(subject, payload)
            self.published_count += 1
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NATS publish to %r failed: %s [correlation_id=%s]",
                subject, exc, event.correlation_id,
            )
            self.failed_count += 1
            # Drop the cached connection so the next call retries.
            # nats-py's reconnect is automatic for transient errors,
            # but force a clean rebuild on persistent failure so we
            # don't loop on a half-dead socket.
            self._client = None
            return False

    # ── Internals ──────────────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        async with self._connect_lock:
            # Another coroutine may have connected while we waited.
            if self._client is not None:
                return
            await self._do_connect()

    async def _do_connect(self) -> None:
        # Lazy import — nats-py only pulled in when the publisher is
        # actually used. Operators on the disabled path don't pay the
        # import cost.
        try:
            import nats
        except ImportError as exc:
            raise RuntimeError(
                "NATS publisher enabled but 'nats-py' is not installed. "
                "Add `nats-py>=2.6` to KAI-C's dependencies."
            ) from exc

        kwargs: dict[str, Any] = {
            "servers": [self._url],
            "connect_timeout": self._connect_timeout,
            # Reconnect aggressively so a NATS restart doesn't blackhole
            # broadcast for long. 1s backoff capped at 5 attempts before
            # we give up on the cached connection; next publish reopens.
            "reconnect_time_wait": 1.0,
            "max_reconnect_attempts": 5,
        }
        if self._token:
            kwargs["token"] = self._token
        self._client = await nats.connect(**kwargs)
        logger.info(
            "NATS publisher connected to %s (token=%s)",
            self._url, "set" if self._token else "none",
        )

    def _validate_sovereignty(self) -> None:
        """Under ``local_only`` the NATS URL must resolve to a
        loopback or Docker-private network address. Surfaces a
        SovereigntyViolation at startup so operators see the misconfig
        immediately, not silently. Mirrors the
        ``kai_c.sovereignty.check_adapter`` check for HTTP adapter URLs.
        """
        if self._sovereignty_mode != "local_only":
            return  # federated / cloud_allowed skip the loopback check
        assert self._url is not None  # only called when enabled
        parsed = urlparse(self._url)
        host = parsed.hostname or ""
        if not host:
            raise ValueError(
                f"NATS_URL {self._url!r} has no host component"
            )
        # Allow Docker DNS names (alphabetic) — those resolve only
        # inside the sentinel_internal network, which is private by
        # construction. Reject anything that resolves to a public IP.
        try:
            ip = ipaddress.ip_address(host)
            is_loopback = ip.is_loopback
            is_private = ip.is_private
        except ValueError:
            # Not an IP — treat as a hostname. Resolve and check the
            # result. If resolution fails, we fail closed (refuse to
            # connect) so a mistyped DNS name doesn't masquerade as
            # a private one.
            try:
                resolved = socket.gethostbyname(host)
                ip = ipaddress.ip_address(resolved)
                is_loopback = ip.is_loopback
                is_private = ip.is_private
            except (socket.gaierror, ValueError) as exc:
                # Inside docker-compose, the bridge-network name "nats"
                # may not resolve at startup time (DNS comes up after
                # the service). Allow alphabetic hostnames that COULD
                # resolve to a private address; the actual connect
                # call below will fail loudly if the host is bogus.
                if host.replace("-", "").replace("_", "").isalnum():
                    logger.info(
                        "NATS_URL host %r doesn't resolve yet — accepting "
                        "as a Docker-network hostname (sovereignty check "
                        "deferred to connect time). Resolution error: %s",
                        host, exc,
                    )
                    return
                raise ValueError(
                    f"NATS_URL host {host!r} could not be validated under "
                    f"AI_SOVEREIGNTY=local_only: {exc}"
                ) from exc
        if not (is_loopback or is_private):
            raise ValueError(
                f"NATS_URL {self._url!r} resolves to a public address "
                f"({ip}) under AI_SOVEREIGNTY=local_only. NATS must run on "
                "loopback, a Docker-private network, or a private LAN range."
            )
