# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
Alert emission — the canonical §11.5 implementation.

Promoted verbatim from ``examples/loitering-detection/alerts.py`` (which
was itself a copy of ``examples/intrusion-detection/alerts.py`` per the
old "copy-as-template" model). This module is now the single canonical
copy; the examples import from here via thin shims.

Three delivery channels in v1:

* **stdout** — always fires. Operator-visible log line, machine-grep-able.
* **webhook** — optional HTTP POST. Slack, Discord, Teams, PagerDuty —
  any service that accepts an incoming-webhook JSON payload works.
* **nats** — optional. Publishes the alert's §11.5 JSON onto a NATS
  subject derived from the alert source + camera. Lets the operator
  UI inbox, SIEM bridges, or any other subscriber fan out off the same
  bus that already carries KAI-C inference events (NATS event bus).

Future channels (OpenNVR alerts-API native endpoint, SMS/email via
OpenNVR's notification settings) plug in alongside via the AlertChannel
protocol without touching the detector loop.

The Alert shape on the wire matches §11.5 of the contract design so
downstream consumers parse it identically to KAI-C-emitted alerts. The
contract calls this the "app-emitted alert" shape.

Per-app identity
----------------
Each app process identifies itself through the ``source`` block of the
envelope. In the copy-as-template era each copy hardcoded its own
``AlertSource.name`` default; the SDK instead keeps a process-wide
default that apps set once via :func:`set_default_source` (the
``Detector`` base does this automatically from ``manifest.id``).
Explicitly-constructed ``AlertSource`` instances are unaffected.

NATS subject scheme — mirrors the §11.5 ``source`` block:

    opennvr.alerts.{source.kind}.{source.name}.{camera_id}

Example subjects:

    opennvr.alerts.app.intrusion-detection.cam-front-door
    opennvr.alerts.app.loitering-detection.cam-back-shed
    opennvr.alerts.adapter.yolov8.cam-X          (future, adapter-emitted)
    opennvr.alerts.kai-c.policy-violation.cam-X  (future, KAI-C-emitted)

Wildcards for subscribers:

    opennvr.alerts.>                          → every alert
    opennvr.alerts.app.>                      → every app-emitted alert
    opennvr.alerts.*.*.cam-front-door         → every alert about one camera
    opennvr.alerts.app.intrusion-detection.>  → one app's alerts

Note: ``opennvr.alerts.app.>`` is preferred over ``opennvr.alerts.app.*.*``
even though both match the current 4-token subject. ``>`` matches one
or more tokens so it survives a future contract revision that adds a
fifth segment (e.g. ``track_id``) without subscribers needing to
re-subscribe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


# Default subject prefix. Operators can override via config; we sit on
# ``opennvr.alerts.*`` to mirror ``opennvr.inference.*`` from the NATS event bus.
DEFAULT_ALERT_SUBJECT_PREFIX = "opennvr.alerts"

# Connect + publish budgets. NATS is local; these are tight on purpose
# so a misbehaving broker doesn't stall the detector loop.
_NATS_CONNECT_TIMEOUT_SECONDS = 5.0
_NATS_PUBLISH_TIMEOUT_SECONDS = 2.0
_NATS_DRAIN_TIMEOUT_SECONDS = 2.0


# ── Default source identity ────────────────────────────────────────
#
# A ContextVar rather than a plain module global: several Detectors can
# share one process (the camera agent's create_monitor instantiates SDK
# detectors at runtime), and each stamps its own identity around its
# handler invocations without clobbering its neighbours.

_DEFAULT_SOURCE: ContextVar[dict[str, str]] = ContextVar(
    "opennvr_app_sdk_default_source",
    default={"kind": "app", "name": "opennvr-app", "version": "1.0.0"},
)


def set_default_source(
    *,
    kind: str | None = None,
    name: str | None = None,
    version: str | None = None,
) -> None:
    """Set the default ``AlertSource`` identity for the current context.

    Call once at app startup for single-app processes. The ``Detector``
    base instead scopes its identity around each handler call, so
    multiple detectors in one process don't fight. Only affects
    ``AlertSource`` instances created WITHOUT explicit values."""
    current = dict(_DEFAULT_SOURCE.get())
    if kind is not None:
        current["kind"] = kind
    if name is not None:
        current["name"] = name
    if version is not None:
        current["version"] = version
    _DEFAULT_SOURCE.set(current)


def get_default_source() -> dict[str, str]:
    """Snapshot of the current default source (for save/restore in tests)."""
    return dict(_DEFAULT_SOURCE.get())


def scoped_default_source(source: dict[str, str]) -> Token:
    """Install ``source`` as the default for the current context; returns
    a token for :func:`reset_default_source`. Used by the Detector loop."""
    return _DEFAULT_SOURCE.set(dict(source))


def reset_default_source(token: Token) -> None:
    _DEFAULT_SOURCE.reset(token)


# ── Alert payload (matches §11.5 wire shape) ───────────────────────


@dataclass
class AlertSource:
    """The ``source`` block of the §11.5 alert envelope.

    Field defaults come from the context default set via
    :func:`set_default_source` — one of: kind = kai-c / adapter / app."""

    kind: str = field(default_factory=lambda: _DEFAULT_SOURCE.get()["kind"])
    name: str = field(default_factory=lambda: _DEFAULT_SOURCE.get()["name"])
    version: str = field(default_factory=lambda: _DEFAULT_SOURCE.get()["version"])


@dataclass
class Alert:
    """A single fired alert.

    Maps 1:1 to the §11.5 alert wire shape: ``alert_id``, ``fired_at``,
    ``title``, ``description``, ``severity``, ``source``, ``camera_id``,
    ``correlation_id``, ``evidence``, ``tags``.

    Severity levels are operator-visible — ``low`` / ``medium`` /
    ``high`` / ``critical`` per the design doc.
    """

    title: str
    description: str
    camera_id: str
    severity: str = "high"  # low / medium / high / critical
    source: AlertSource = field(default_factory=AlertSource)
    correlation_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    alert_id: str = field(default_factory=lambda: f"alrt_{uuid.uuid4().hex[:12]}")
    fired_at: str = field(default_factory=lambda: _utcnow_iso())

    def to_wire(self) -> dict[str, Any]:
        """Serialize to the §11.5 JSON shape."""
        return {
            "alert_id": self.alert_id,
            "fired_at": self.fired_at,
            "title": self.title,
            "description": self.description,
            "severity": self.severity,
            "source": asdict(self.source),
            "camera_id": self.camera_id,
            "correlation_id": self.correlation_id,
            "evidence": dict(self.evidence),
            "tags": list(self.tags),
        }


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp without microseconds."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── Channels ───────────────────────────────────────────────────────


class AlertChannel(Protocol):
    """Anything that can ``send(alert)`` is a channel. Stdout + webhook
    are the v1 implementations; future channels (OpenNVR alerts API)
    plug in here without touching the detector loop."""

    def send(self, alert: Alert) -> bool:
        ...  # pragma: no cover — Protocol


class StdoutChannel:
    """Always-on channel: human-readable line to stdout."""

    name = "stdout"

    def send(self, alert: Alert) -> bool:
        # Single line, machine-grep-friendly. Severity in CAPS so a
        # tail -f operator spots high/critical quickly.
        line = (
            f"ALERT [{alert.severity.upper()}] "
            f"{alert.fired_at} "
            f"camera={alert.camera_id} "
            f"title={alert.title!r} "
            f"correlation_id={alert.correlation_id or '-'} "
            f"alert_id={alert.alert_id}"
        )
        print(line, flush=True)
        return True


def alert_subject(alert: "Alert", *, prefix: str = DEFAULT_ALERT_SUBJECT_PREFIX) -> str:
    """Derive the NATS subject for one alert.

    Shape: ``{prefix}.{source.kind}.{source.name}.{camera_id}`` with
    each segment sanitized so the result is a valid NATS subject (no
    spaces, no dots, no NATS reserved ``*`` / ``>``).

    Sanitization rule: any character outside ``[A-Za-z0-9_-]`` becomes
    ``_``. Empty-after-sanitization segments fall back to ``"unknown"``
    so a malformed Alert can't produce ``opennvr.alerts...cam-X``.

    Pulled out as a module-level function so tests can assert subject
    derivation without spinning up NATS.
    """
    return (
        f"{prefix}."
        f"{_sanitize_subject_token(alert.source.kind)}."
        f"{_sanitize_subject_token(alert.source.name)}."
        f"{_sanitize_subject_token(alert.camera_id)}"
    )


_SUBJECT_TOKEN_BAD = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_subject_token(value: str) -> str:
    cleaned = _SUBJECT_TOKEN_BAD.sub("_", value).strip("_")
    return cleaned or "unknown"


class WebhookChannel:
    """POST the alert's JSON wire shape to an operator-configured URL.

    Failures are LOGGED but never raise — a dead webhook should not
    prevent stdout alerts from firing or crash the detector loop. This
    matches the §11.2 "audit forwarding failures are themselves
    audited" pattern (though we don't yet have an audit sink here —
    that lands when the SDK talks to KAI-C's audit log directly).
    """

    name = "webhook"

    def __init__(self, url: str, *, timeout_seconds: float = 5.0) -> None:
        self._url = url
        self._timeout = timeout_seconds

    def send(self, alert: Alert) -> bool:
        body = alert.to_wire()
        try:
            response = httpx.post(
                self._url,
                json=body,
                timeout=self._timeout,
                # trust_env=False so an operator-side HTTP_PROXY env
                # doesn't redirect alert delivery through a proxy that
                # might not honor the schema.
                trust_env=False,
            )
            if response.status_code >= 400:
                logger.warning(
                    "webhook %s returned %d: %s",
                    self._url,
                    response.status_code,
                    response.text[:200],
                )
                return False
            return True
        except Exception as exc:
            logger.warning("webhook %s failed: %s", self._url, exc)
            return False


class NatsAlertChannel:
    """Publish each alert as JSON onto a NATS subject derived from the
    §11.5 source block.

    Why this exists
    ---------------
    The other two channels are point-to-point: stdout goes to one
    operator, the webhook goes to one URL. NATS is bus-shaped — N
    subscribers can fan out off the same publish (operator UI inbox,
    SIEM bridge, Slack bot, audit forwarder) without the publishing
    app knowing they exist. Same pattern KAI-C uses for inference
    events under the NATS event bus.

    Implementation notes
    --------------------
    ``AlertChannel.send`` is synchronous and called from a detector
    loop that may itself be sync (HTTP poll mode) or async (NATS
    subscriber / WS streaming mode). To keep the protocol uniform we
    run a background daemon thread that owns an asyncio event loop;
    ``send`` schedules the publish coroutine onto it via
    ``run_coroutine_threadsafe`` and waits for the result with a hard
    timeout. This insulates the dispatcher from the async-ness of
    ``nats-py`` and keeps publish failures isolated to this channel.

    Failures (broker down, slow connect, bad credentials) are LOGGED
    but never raise — same contract as ``WebhookChannel``. The detector
    loop should never crash because the bus is down.
    """

    name = "nats"

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        subject_prefix: str = DEFAULT_ALERT_SUBJECT_PREFIX,
        connect_timeout_seconds: float = _NATS_CONNECT_TIMEOUT_SECONDS,
        publish_timeout_seconds: float = _NATS_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        # Validate the prefix at construction (not at publish-time)
        # so a bogus operator config fails loudly before alerts start
        # flowing. NATS subjects allow dots as token separators (the
        # prefix is multi-token, e.g. ``opennvr.alerts``), so we
        # split-and-validate per-token rather than re-using the
        # single-token sanitizer (peer-review L6).
        if not subject_prefix or not subject_prefix.strip("."):
            raise ValueError(
                f"NatsAlertChannel: subject_prefix must not be empty, "
                f"got {subject_prefix!r}"
            )
        for token_seg in subject_prefix.split("."):
            if not token_seg or _SUBJECT_TOKEN_BAD.search(token_seg):
                raise ValueError(
                    f"NatsAlertChannel: subject_prefix {subject_prefix!r} "
                    f"contains a NATS-invalid token {token_seg!r}. Each "
                    f"dot-separated segment must match [A-Za-z0-9_-]+."
                )
        self._url = url
        self._token = token
        self._subject_prefix = subject_prefix
        self._connect_timeout = connect_timeout_seconds
        self._publish_timeout = publish_timeout_seconds
        # Created lazily on first send so importing this module
        # doesn't spawn a thread for users who didn't enable NATS.
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._nc: Any = None
        # Guards the (loop, thread) tuple's lifecycle from cross-thread
        # ``send`` calls and ``close`` calls. Distinct from the in-loop
        # ``_connect_lock`` below, which guards against two concurrent
        # ``_publish_once`` coroutines both calling ``nats.connect``.
        self._lock = threading.Lock()
        # Created together with the loop because asyncio.Lock can't be
        # safely constructed outside the loop's thread.
        self._connect_lock: asyncio.Lock | None = None

    def send(self, alert: Alert) -> bool:
        subject = alert_subject(alert, prefix=self._subject_prefix)
        return self.publish_json(subject, alert.to_wire())

    def publish_json(self, subject: str, obj: Any) -> bool:
        """Publish ``obj`` as JSON on ``subject`` — the channel's
        machinery (background loop, lazy connect, hard timeouts,
        log-never-raise) for ANY payload. ``send`` is now a thin
        wrapper; ``domain_events.DomainEventPublisher`` composes this
        to publish contracted ``opennvr.events.*`` envelopes."""
        payload = json.dumps(obj).encode("utf-8")
        try:
            self._ensure_thread()
        except Exception as exc:  # noqa: BLE001
            logger.warning("NATS channel thread start failed: %s", exc)
            return False
        assert self._loop is not None
        budget = self._connect_timeout + self._publish_timeout
        future = asyncio.run_coroutine_threadsafe(
            self._publish_once(subject, payload),
            self._loop,
        )
        try:
            return future.result(timeout=budget)
        except Exception as exc:  # noqa: BLE001 — includes TimeoutError
            logger.warning(
                "NATS publish to %r timed out / failed: %s", subject, exc,
            )
            return False

    def close(self) -> None:
        """Drain pending publishes and stop the background thread.

        Called from the detector's shutdown path. Safe to call even if
        ``send`` was never invoked (no thread to clean up). After
        ``close()`` the channel is re-init safe: the next ``send()``
        spins a fresh loop + thread and reconnects from scratch.
        """
        with self._lock:
            loop = self._loop
            nc = self._nc
            thread = self._thread
            # Reset so a subsequent ``send`` rebuilds cleanly rather
            # than scheduling on a stopped loop and timing out at
            # ``budget`` (peer-review M1).
            self._loop = None
            self._thread = None
            self._nc = None
            self._connect_lock = None

        if loop is None:
            return

        if nc is not None and loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    nc.drain(), loop,
                ).result(timeout=_NATS_DRAIN_TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                logger.warning("NATS drain failed: %s", exc)

        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=3.0)

    # ── Internals ──────────────────────────────────────────────────

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._loop is not None:
                return
            self._loop = asyncio.new_event_loop()
            # asyncio.Lock binds to the running loop on first use; build
            # it now under the same loop the publish coroutines will run
            # on so ``async with self._connect_lock`` works correctly
            # cross-thread.
            self._connect_lock = asyncio.Lock()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                name="nats-alert-channel",
                daemon=True,
            )
            self._thread.start()

    async def _publish_once(self, subject: str, payload: bytes) -> bool:
        # Serialize the connect path so two concurrent ``send`` calls
        # don't both call ``nats.connect`` (peer-review H1). The lock
        # is awaited INSIDE the loop, so it correctly suspends one
        # coroutine until the other finishes connecting.
        assert self._connect_lock is not None  # set in _ensure_thread
        if self._nc is None:
            async with self._connect_lock:
                if self._nc is None:
                    try:
                        await self._connect()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "NATS alert connect to %s failed: %s",
                            self._url, exc,
                        )
                        self._nc = None
                        return False
        try:
            await self._nc.publish(subject, payload)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "NATS alert publish on %r failed: %s — dropping cached "
                "connection for next-call retry",
                subject, exc,
            )
            # Drop the cached connection so transient socket errors
            # heal on the next send rather than wedging permanently.
            self._nc = None
            return False

    async def _connect(self) -> None:
        # Lazy import — nats-py is only pulled in when the operator
        # actually wires up the NATS channel via config.
        import nats
        kwargs: dict[str, Any] = {
            "servers": [self._url],
            "connect_timeout": self._connect_timeout,
            "reconnect_time_wait": 1.0,
            "max_reconnect_attempts": 5,
        }
        if self._token:
            kwargs["token"] = self._token
        self._nc = await nats.connect(**kwargs)
        logger.info(
            "NATS alert channel connected to %s (token=%s, prefix=%s)",
            self._url,
            "set" if self._token else "none",
            self._subject_prefix,
        )


# ── Dispatcher ─────────────────────────────────────────────────────


class AlertDispatcher:
    """Holds an ordered list of channels and fires an alert through
    all of them, isolating each channel's failures.

    ``fire()`` returns a per-channel report so the caller can audit
    delivery outcomes; the detector loop ignores this in v1 but the
    OpenNVR alerts-API integration (planned follow-up) will record it.
    """

    def __init__(self, channels: list[AlertChannel]) -> None:
        if not channels:
            raise ValueError("AlertDispatcher requires at least one channel.")
        self._channels = channels

    def fire(self, alert: Alert) -> dict[str, bool]:
        results: dict[str, bool] = {}
        for channel in self._channels:
            channel_name = getattr(channel, "name", channel.__class__.__name__)
            try:
                ok = channel.send(alert)
            except Exception as exc:
                logger.exception("channel %s raised: %s", channel_name, exc)
                ok = False
            results[channel_name] = ok
        return results

    def close(self) -> None:
        """Drain + tear down any channel that has a ``close`` method.

        Stdout and webhook channels are stateless and have no close()
        — only ``NatsAlertChannel`` does. Wire this into the detector's
        SIGINT/SIGTERM finally clause so in-flight NATS publishes get
        drained instead of dropped (peer-review H2). Per-channel
        failures are logged but never raise, so shutdown stays clean
        even if one channel's close hangs.
        """
        for channel in self._channels:
            close = getattr(channel, "close", None)
            if close is None:
                continue
            channel_name = getattr(channel, "name", channel.__class__.__name__)
            try:
                close()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "channel %s close failed: %s", channel_name, exc,
                )


def build_dispatcher(
    *,
    webhook_url: str | None,
    nats_alerts_url: str | None = None,
    nats_alerts_token: str | None = None,
    nats_alerts_subject_prefix: str = DEFAULT_ALERT_SUBJECT_PREFIX,
) -> AlertDispatcher:
    """Convenience factory used by app config loading. stdout is always
    included; webhook and NATS are independently opt-in via config.

    Order matters: stdout fires first (operator-visible immediately),
    then webhook (still typically the fastest external sink), then NATS
    (bus fan-out for consumers that don't need synchronous delivery).
    Each channel's failure is isolated by ``AlertDispatcher.fire``.
    """
    channels: list[AlertChannel] = [StdoutChannel()]
    if webhook_url:
        channels.append(WebhookChannel(webhook_url))
    if nats_alerts_url:
        channels.append(
            NatsAlertChannel(
                nats_alerts_url,
                token=nats_alerts_token,
                subject_prefix=nats_alerts_subject_prefix,
            )
        )
    return AlertDispatcher(channels)
