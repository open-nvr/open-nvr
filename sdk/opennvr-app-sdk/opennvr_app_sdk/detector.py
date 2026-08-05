# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
The Detector base — the InferenceSubscriber archetype (App SDK spec §02).

A Detector SUBSCRIBES to KAI-C's NATS broadcast surface
(``opennvr.inference.*``) and consumes inference results some other
app is already driving — adapter GPU is paid once, N subscribers fan
out from one inference stream. Contrast with :class:`~.frame_app.FrameApp`,
which DRIVES inference by polling frames into KAI-C itself.

What the base owns (ported verbatim from the loitering-detection
example, the reference migration):

* the NATS connect / subscribe / decode loop with graceful drain;
* per-message JSON decoding + exception isolation (one bad event never
  takes down a long-lived process) — factored into :meth:`Detector._handle_raw`
  so tests can exercise the full path without a broker;
* the §12 ``InferenceCompletedEvent`` payload walk: ``camera_id`` +
  ``result.detections`` extraction with defensive shape checks, and
  ``completed_at`` timestamp parsing with a clock fallback
  (:meth:`Detector.parse_event_ts`);
* alert dispatch: whatever :meth:`Detector.on_detections` returns or
  yields goes through the app's :class:`~.alerts.AlertDispatcher`;
* the CLI / logging / SIGINT+SIGTERM lifecycle behind
  ``app(MyDetector).run()``.

What the app writes: a ``manifest``, an optional ``setup()``, and
``on_detections(camera_id, detections, event)`` — the rule.

``event`` is the raw decoded JSON dict (correlation_id, adapter,
model_fingerprint, completed_at, …). The spec sketches a typed
``InferenceEvent`` view; that is deferred so migrated apps that already
treat the event as a dict keep working unchanged.

The base also carries the app contract surface (spec §03) via
:class:`~.contract.ContractMixin`: when the config has a
``contract_port``, ``run()`` serves ``GET /health`` / ``/manifest`` /
``/state`` on a stdlib HTTP server, and when it has an
``opennvr_url``, boot POSTs ``/api/v1/apps/register`` (best-effort).
Both are off by default — a config without those keys behaves exactly
as before.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import signal
import sys
from typing import Any, Callable, Iterable

from .alerts import (
    DEFAULT_ALERT_SUBJECT_PREFIX,
    Alert,
    AlertDispatcher,
    build_dispatcher,
    reset_default_source,
    scoped_default_source,
)
from .contract import ContractMixin
from .manifest import AppManifest
from .nats_loop import NatsSubscriberMixin
from .state import KeyedState
from .state import keyed_state as _keyed_state

logger = logging.getLogger(__name__)


class Detector(ContractMixin, NatsSubscriberMixin):
    """Base class for NATS-subscribing detection apps.

    Subclasses set a class-level ``manifest`` (:class:`AppManifest`),
    optionally override :meth:`setup` to allocate state, and implement
    :meth:`on_detections`. ``cfg`` is the app-parsed config object; the
    NATS loop reads ``cfg.nats_url``, ``cfg.nats_token`` (optional) and
    ``cfg.subject_pattern`` from it.

    ``clock`` is a callable returning a UTC datetime; per-event
    timestamps from the NATS payload are preferred, but timestamp
    parsing needs a "now" fallback for missing / malformed values.
    Tests pass a controlled clock for determinism.
    """

    manifest: AppManifest | None = None

    def __init__(
        self,
        config: Any,
        dispatcher: AlertDispatcher,
        *,
        clock: Callable[[], _dt.datetime] | None = None,
    ) -> None:
        self.cfg = config
        # Compat alias — pre-SDK detectors (and their tests) used
        # ``self._config``.
        self._config = config
        self._dispatcher = dispatcher
        self._clock = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))
        # Opt-in Tier-0 consumption (see docs/tier0-consumption.md): when
        # True, tier0 events are bridged into contract-shaped detections and
        # flow through on_detections like any adapter event. Off by default:
        # an app also subscribed to a heavy adapter would otherwise process
        # the same object twice (double alerts).
        self.consume_tier0: bool = bool(getattr(config, "consume_tier0", False))
        self._stop_event = asyncio.Event()
        self._nc: Any = None
        # This detector emits alerts AS this app. The identity is scoped
        # around each handler call (not set process-wide) so several
        # detectors can share one process — the camera agent's
        # create_monitor case — without clobbering each other's source.
        self._source_block: dict[str, str] | None = (
            {"kind": "app", "name": self.manifest.id, "version": self.manifest.version}
            if self.manifest is not None
            else None
        )
        self._contract_init()
        self.setup()

    # ── App surface ────────────────────────────────────────────────

    def setup(self) -> None:
        """Optional hook — allocate per-app state (``keyed_state`` et
        al.). Runs once at construction, after ``cfg`` is set."""

    def on_detections(
        self,
        camera_id: str,
        detections: list[dict[str, Any]],
        event: dict[str, Any],
    ) -> Iterable[Alert] | None:
        """The rule. Called once per decoded inference event that has a
        ``camera_id`` and a ``result.detections`` list. Return or yield
        the :class:`Alert` objects to fire (or ``None`` / empty)."""
        raise NotImplementedError

    def keyed_state(self, ttl: float, **kwargs: Any) -> KeyedState:
        """Convenience for ``setup()`` — see :func:`~.state.keyed_state`."""
        return _keyed_state(ttl, **kwargs)

    # ``stop()`` comes from :class:`~.nats_loop.NatsSubscriberMixin`.

    # ── Per-message handling (testable without NATS) ───────────────

    def _handle_raw(self, data: bytes, *, subject: str = "") -> list[Alert]:
        """Decode one raw NATS message and run it through the handler.

        Isolation contract (ported from the pre-SDK loop): non-JSON
        payloads are logged + skipped; a raising ``on_detections`` is
        logged + swallowed. A long-lived subscriber never dies to one
        bad message. Returns the alerts fired for this message."""
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError too: ``json.loads(bytes)`` BOM-sniffs
            # the encoding and can fail before JSON parsing starts.
            logger.warning("skipping non-JSON message on %r: %s", subject, exc)
            return []
        try:
            return self.handle_event(payload)
        except Exception:
            logger.exception("handle_event failed for subject=%s", subject)
            return []

    def handle_event(self, event: Any) -> list[Alert]:
        """Process one decoded ``InferenceCompletedEvent`` dict:
        extract ``camera_id`` + ``result.detections`` (defensively —
        malformed shapes return ``[]``), delegate to
        :meth:`on_detections`, and dispatch every returned alert.
        Returns the list of alerts fired."""
        # Contract counters (spec §03): every decoded event counts as
        # "seen" — /health's last_event_age_s is stall detection for
        # the pipe, not a per-shape metric.
        is_tier0 = (
            isinstance(event, dict)
            and (
                event.get("schema") == "opennvr.tier0.v1"
                or event.get("adapter") == "tier0"
            )
        )
        if is_tier0 and not self.consume_tier0:
            # Not counted as "seen": tier0 publishes per-frame, and letting it
            # refresh last_event_age_s would mask a stalled adapter for apps
            # that don't consume tier0 at all.
            return []
        self._contract_note_event()
        if not isinstance(event, dict):
            return []
        camera_id = event.get("camera_id")
        if not camera_id:
            return []
        if is_tier0:
            from .tier0 import tier0_to_detections

            detections: Any = tier0_to_detections(event)
        else:
            result = event.get("result") or {}
            detections = (
                result.get("detections") if isinstance(result, dict) else None
            )
        if not isinstance(detections, list):
            return []

        token = scoped_default_source(self._source_block) if self._source_block else None
        try:
            produced = self.on_detections(camera_id, detections, event)
            fired: list[Alert] = []
            if produced is None:
                return fired
            for alert in produced:
                self._dispatcher.fire(alert)
                fired.append(alert)
            self._contract_note_alerts(len(fired))
            return fired
        finally:
            if token is not None:
                reset_default_source(token)

    def parse_event_ts(self, raw: Any) -> float:
        """Extract a POSIX timestamp from the NATS event bus
        ``completed_at`` ISO string. Falls back to the clock for
        missing / malformed values so a misbehaving publisher doesn't
        break app state machines."""
        if isinstance(raw, str):
            try:
                # Pydantic emits ISO with a trailing 'Z' or offset.
                ts = _dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=_dt.timezone.utc)
                return ts.timestamp()
            except ValueError:
                pass
        return self._clock().timestamp()

    # ── NATS loop ──────────────────────────────────────────────────

    async def run(self, *, once: bool = False) -> None:
        """Connect to NATS, subscribe, drive the handler on every
        received event. Returns when ``stop()`` is called or when
        ``once=True`` and one message has been processed.

        Also owns the app-contract lifecycle (spec §03): starts the
        ``/health`` / ``/manifest`` / ``/state`` server when
        ``cfg.contract_port`` is set and self-registers with the
        OpenNVR app registry when ``cfg.opennvr_url`` is set — both
        best-effort no-ops otherwise.

        The connect / subscribe / drain machinery itself lives on
        :class:`~.nats_loop.NatsSubscriberMixin`, shared with the
        AlertSubscriber archetype."""
        self.start_contract_server()
        self.register_with_opennvr()
        self.start_config_poll()
        try:
            await self._run_nats_loop(once=once)
        finally:
            self.stop_config_poll()
            self.stop_contract_server()


# ── CLI runner ──────────────────────────────────────────────────────


class AppRunner:
    """The ``app(MyDetector)`` return value — owns argparse, logging
    setup, dispatcher construction, and the signal-driven lifecycle.
    Behavior ported from the loitering-detection example's ``main()``."""

    def __init__(
        self,
        detector_cls: type[Detector],
        *,
        load_config: Callable[[str], Any] | None = None,
    ) -> None:
        loader = load_config or getattr(detector_cls, "load_config", None)
        if loader is None:
            raise TypeError(
                f"app({detector_cls.__name__}): pass load_config= or define "
                f"a load_config classmethod on the detector class"
            )
        self._detector_cls = detector_cls
        self._load_config = loader

    def run(self, argv: list[str] | None = None) -> int:
        manifest = self._detector_cls.manifest
        parser = argparse.ArgumentParser(
            prog=manifest.id if manifest else self._detector_cls.__name__,
            description=(manifest.summary or manifest.name) if manifest else None,
        )
        parser.add_argument("--config", required=True, help="Path to config.yml")
        parser.add_argument(
            "--once",
            action="store_true",
            help="Process one event then exit (smoke testing).",
        )
        parser.add_argument(
            "--log-level",
            default="INFO",
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        )
        args = parser.parse_args(argv)

        logging.basicConfig(
            level=args.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        try:
            config = self._load_config(args.config)
        except (ValueError, OSError) as exc:
            print(f"config error: {exc}", file=sys.stderr)
            return 2

        dispatcher = build_dispatcher(
            webhook_url=getattr(config, "webhook_url", None),
            nats_alerts_url=getattr(config, "nats_alerts_url", None),
            nats_alerts_token=getattr(config, "nats_alerts_token", None),
            nats_alerts_subject_prefix=getattr(
                config, "nats_alerts_subject_prefix", DEFAULT_ALERT_SUBJECT_PREFIX,
            ),
        )
        detector = self._detector_cls(config, dispatcher)

        loop = asyncio.new_event_loop()

        def _handle_signal(_signum: int, _frame: Any) -> None:
            logger.info("signal received, stopping…")
            loop.call_soon_threadsafe(detector.stop)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        try:
            loop.run_until_complete(detector.run(once=args.once))
        finally:
            # Drain in-flight NATS alert publishes BEFORE we close the
            # asyncio loop — the dispatcher runs its NATS client on its
            # own daemon thread, but ``close`` blocks until drain
            # completes, which is what we want at shutdown.
            dispatcher.close()
            loop.close()
        return 0


def app(
    detector_cls: type[Detector],
    *,
    load_config: Callable[[str], Any] | None = None,
) -> AppRunner:
    """Wrap a Detector subclass in a CLI runner::

        if __name__ == "__main__":
            raise SystemExit(app(Loitering, load_config=load_config).run())
    """
    return AppRunner(detector_cls, load_config=load_config)


__all__ = ["Detector", "AppRunner", "app"]
