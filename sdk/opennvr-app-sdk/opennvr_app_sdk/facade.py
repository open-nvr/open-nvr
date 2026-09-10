# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
The ``App`` facade — the front door of the App SDK.

Everything under this module compiles down to :class:`~.detector.Detector`;
nothing here is a new runtime. An app written against the facade and an
app written against ``Detector`` produce the same process, the same
manifest, the same alerts and the same contract surface. The facade
exists because the common case — "watch for a label, maybe in a zone,
maybe for a while, then alert" — should not require the author to know
about NATS subjects, event envelopes, normalized bboxes, alert
dispatchers or keyed TTL state.

The whole of a first app::

    from opennvr_app_sdk import App

    app = App("driveway-watch", name="Driveway Watch", category="perimeter")

    @app.on_detection("person", zone="driveway", dwell=30)
    def loitering(event):
        event.alert(
            f"Person loitering on {event.camera}",
            severity="high",
        )

    if __name__ == "__main__":
        raise SystemExit(app.run())

What the facade does for you, in order:

* builds the :class:`~.manifest.AppManifest` from the constructor
  arguments plus whatever the handlers imply — a ``zone=`` anywhere
  adds the per-camera ``geometry.polygon`` param the catalog renders as
  a zone editor, an ``AlertType`` is derived per handler;
* builds the config dataclass — :class:`~.config.BaseAppConfig` plus
  one field per declared :func:`param`, so ``config.yml`` and
  ``app.config`` stay in step without a hand-written loader;
* fans one inference event out to one handler call PER MATCHING
  DETECTION, so the handler body is about one object, not a list;
* owns the state that dwell and cooldown need, keyed per camera,
  label and track, with the same TTL/latch semantics apps used to
  hand-roll with :func:`~.state.keyed_state`.

When a rule outgrows this, drop to :class:`~.detector.Detector`: the
facade is additive and the base classes are unchanged. ``@app.on_event``
is the escape hatch that stays inside the facade — it hands you the raw
``(camera_id, detections, event)`` triple the Detector sees.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, make_dataclass
from typing import Any, Callable, Iterable, Sequence

from .alerts import Alert, AlertDispatcher
from .config import BaseAppConfig, load_app_config
from .detector import AppRunner, Detector
from .geometry import Point, Zone, bbox_center, scale_vertices
from .manifest import AlertType, AppManifest, Param
from .state import KeyedState, keyed_state

logger = logging.getLogger(__name__)

#: Handlers see detections at or above this confidence unless they say
#: otherwise. Below this the stock detectors are mostly noise.
DEFAULT_MIN_CONFIDENCE = 0.35

#: How long the facade remembers a (camera, label, track) key for dwell
#: and cooldown bookkeeping once it stops being seen.
_STATE_TTL_FLOOR = 60.0


# ── The event object ────────────────────────────────────────────────


class DetectionEvent:
    """One detection, in context — what a ``@app.on_detection`` handler
    is called with.

    The object is deliberately flat: the things a rule asks about are
    attributes or one-word methods, and the raw envelope stays
    available as :attr:`raw` for anything the facade does not model.

    Coordinates are NORMALIZED (0–1 of the frame) throughout, matching
    the platform's ``NormalizedBBox`` wire shape, so a rule written
    against one camera resolution works on all of them.
    """

    __slots__ = (
        "detection", "camera", "raw", "config", "state", "_zones",
        "_alerts", "_record", "_key", "_app", "_severity",
    )

    def __init__(
        self,
        *,
        detection: dict[str, Any],
        camera: str,
        raw: dict[str, Any],
        config: Any,
        state: KeyedState,
        zones: dict[str, Zone],
        record: Any,
        key: tuple,
        owner: "App",
        severity: str = "medium",
    ) -> None:
        self.detection = detection
        self.camera = camera
        self.raw = raw
        self.config = config
        self.state = state
        self._zones = zones
        self._record = record
        self._key = key
        self._app = owner
        self._severity = severity
        self._alerts: list[Alert] = []

    # ── What was seen ──────────────────────────────────────────────

    @property
    def label(self) -> str:
        """The detection's class label, lowercased (``"person"``)."""
        return str(self.detection.get("label", "")).lower()

    @property
    def confidence(self) -> float:
        """Detector confidence in [0, 1]; ``0.0`` when absent."""
        try:
            return float(self.detection.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def track_id(self) -> str | None:
        """Tracker identity for this object, when the adapter emits one.

        Present ⇒ dwell and cooldown follow the OBJECT. Absent ⇒ they
        fall back to the (camera, label) pair, which is coarser but
        still keeps a parked car from re-alerting every frame."""
        raw = self.detection.get("track_id")
        return str(raw) if raw not in (None, "") else None

    @property
    def bbox(self) -> dict[str, float]:
        """Normalized ``{x, y, w, h}`` box; missing keys read as 0."""
        box = self.detection.get("bbox") or self.detection.get("bbox_normalized") or {}
        if not isinstance(box, dict):
            return {"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}
        out: dict[str, float] = {}
        for key in ("x", "y", "w", "h"):
            try:
                out[key] = float(box.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                out[key] = 0.0
        return out

    @property
    def center(self) -> Point:
        """Normalized centre point of the box — what zone tests use."""
        return bbox_center(self.bbox, 1, 1)

    # ── Where it was ───────────────────────────────────────────────

    def in_zone(self, name: str | None = None) -> bool:
        """True when the detection's centre falls inside a configured
        zone. With no ``name``, true when it falls inside ANY zone.

        An unknown zone name is False, not an error: zones are operator
        config and a camera may simply not have one drawn yet."""
        if name is None:
            return any(z.contains(self.center) for z in self._zones.values())
        zone = self._zones.get(name)
        return bool(zone and zone.contains(self.center))

    @property
    def zone(self) -> str | None:
        """Name of the first configured zone containing the detection,
        or ``None`` when it is outside every zone."""
        point = self.center
        for zone_name, zone in self._zones.items():
            if zone.contains(point):
                return zone_name
        return None

    @property
    def zones(self) -> list[str]:
        """Every configured zone the detection falls inside."""
        point = self.center
        return [n for n, z in self._zones.items() if z.contains(point)]

    # ── When, and for how long ─────────────────────────────────────

    @property
    def dwell_s(self) -> float:
        """Seconds this object has been continuously present.

        "Continuously" means without a gap longer than the state TTL —
        an object that leaves and comes back starts a fresh episode."""
        return float(self._record.age)

    @property
    def first_seen(self) -> bool:
        """True on the first event of a presence episode."""
        return self._record.first_seen == self._record.last_seen

    @property
    def ts(self) -> float:
        """POSIX timestamp of the inference event (clock fallback)."""
        return float(self._record.last_seen)

    # ── The rest of the frame ──────────────────────────────────────

    @property
    def detections(self) -> list[dict[str, Any]]:
        """Every detection in the same inference event, this one
        included — for rules that need company ("a person AND a car")."""
        result = self.raw.get("result") or {}
        found = result.get("detections") if isinstance(result, dict) else None
        return found if isinstance(found, list) else []

    def count(self, label: str | None = None) -> int:
        """How many objects of ``label`` (or of any label) the same
        event carried."""
        if label is None:
            return len(self.detections)
        wanted = label.lower()
        return sum(
            1 for d in self.detections
            if isinstance(d, dict) and str(d.get("label", "")).lower() == wanted
        )

    @property
    def correlation_id(self) -> str:
        """The platform's correlation id for this inference, carried
        onto every alert the handler fires."""
        return str(self.raw.get("correlation_id") or "")

    @property
    def adapter(self) -> str:
        """Which KAI-C adapter produced the detection."""
        return str(self.raw.get("adapter") or "")

    # ── What to do about it ────────────────────────────────────────

    def alert(
        self,
        title: str,
        description: str = "",
        *,
        severity: str | None = None,
        evidence: dict[str, Any] | None = None,
        tags: Iterable[str] | None = None,
        camera_id: str | None = None,
    ) -> Alert:
        """Fire an alert. Everything the platform needs — camera,
        correlation id, the label, the confidence, the zone — is filled
        in from the event; pass ``evidence`` to add your own.

        ``severity`` defaults to the rule's declared severity, so the
        manifest's ``emits`` block and the alerts actually fired can
        never disagree.

        Returns the :class:`~.alerts.Alert` so a handler can adjust it,
        but there is no need to return it: it is dispatched either way.
        """
        body = dict(evidence or {})
        body.setdefault("label", self.label)
        body.setdefault("confidence", self.confidence)
        body.setdefault("adapter", self.adapter)
        if self.track_id:
            body.setdefault("track_id", self.track_id)
        zone_name = self.zone
        if zone_name:
            body.setdefault("zone", zone_name)
        if self.dwell_s > 0:
            body.setdefault("dwell_s", round(self.dwell_s, 1))
        tag_list = [self._app.id, self.label]
        if zone_name:
            tag_list.append(zone_name)
        if tags:
            tag_list.extend(str(t) for t in tags)
        alert = Alert(
            title=title,
            description=description or title,
            camera_id=camera_id or self.camera,
            severity=severity or self._severity,
            correlation_id=self.correlation_id or None,
            evidence=body,
            tags=list(dict.fromkeys(tag_list)),
        )
        self._alerts.append(alert)
        return alert

    def remember(self, **values: Any) -> None:
        """Stash values on this object's presence record — readable on
        the next event for the same object via :meth:`recall`."""
        self._record.data.update(values)

    def recall(self, name: str, default: Any = None) -> Any:
        """Read back what :meth:`remember` stored for this object."""
        return self._record.data.get(name, default)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (
            f"<DetectionEvent {self.label} on {self.camera} "
            f"conf={self.confidence:.2f} zone={self.zone!r} "
            f"dwell={self.dwell_s:.1f}s>"
        )


# ── Handler registration ────────────────────────────────────────────


@dataclass
class _Rule:
    """One registered ``@app.on_detection`` handler and its filters."""

    fn: Callable[[DetectionEvent], Any]
    labels: tuple[str, ...]
    cameras: tuple[str, ...]
    zone: str | None
    min_confidence: float
    dwell: float
    cooldown: float
    severity: str

    @property
    def name(self) -> str:
        return getattr(self.fn, "__name__", "rule")

    def matches(self, event: DetectionEvent) -> bool:
        if self.labels and event.label not in self.labels:
            return False
        if self.cameras and event.camera not in self.cameras:
            return False
        if event.confidence < self.min_confidence:
            return False
        if self.zone is not None and not event.in_zone(self.zone):
            return False
        return True


class App:
    """A whole OpenNVR app: identity, config, rules, lifecycle.

    Construct one at module scope, decorate handlers on it, and call
    :meth:`run` from ``__main__``. Everything else — the NATS loop, the
    alert dispatcher, the contract server, registry self-registration,
    live config, the CLI, signal handling — is inherited from
    :class:`~.detector.Detector`, which this compiles to.

    Constructor arguments beyond the four below are passed straight
    through to :class:`~.manifest.AppManifest`, so anything the catalog
    understands (``description``, ``use_cases``, ``pricing``,
    ``requires_scopes``, ``provides``, …) is available without leaving
    the facade.
    """

    def __init__(
        self,
        app_id: str,
        *,
        name: str | None = None,
        version: str = "0.1.0",
        category: str = "analytics",
        summary: str = "",
        requires_tasks: Sequence[str] | None = None,
        **manifest_kwargs: Any,
    ) -> None:
        if not app_id or not app_id.strip():
            raise ValueError("App(app_id): an app id is required")
        self.id = app_id.strip()
        self._name = name or self.id.replace("-", " ").title()
        self._version = version
        self._category = category
        self._summary = summary
        self._requires_tasks = list(requires_tasks or ["object_detection"])
        self._manifest_kwargs = manifest_kwargs
        self._rules: list[_Rule] = []
        self._raw_handlers: list[Callable[..., Any]] = []
        self._setup_hooks: list[Callable[[Any], Any]] = []
        self._params: list[Param] = []
        self._emits: list[AlertType] = []
        self._uses_zones = False
        #: Populated at ``run()`` — the parsed config, for handlers that
        #: prefer ``app.config.dwell_s`` to ``event.config.dwell_s``.
        self.config: Any = None

    # ── Declaration ────────────────────────────────────────────────

    def param(
        self,
        name: str,
        type_: Any = str,
        *,
        default: Any = None,
        description: str = "",
        per_camera: bool = False,
        required: bool = False,
        suggestions: Sequence[str] | None = None,
    ) -> "App":
        """Declare one operator-settable knob.

        It becomes a manifest param (so the catalog renders a form
        field), a field on the generated config dataclass (so
        ``config.yml`` fills it), and an attribute on
        :attr:`App.config` and ``event.config``. Chainable."""
        self._params.append(Param(
            name=name,
            type=type_,
            default=default,
            per_camera=per_camera,
            description=description,
            required=required,
            suggestions=list(suggestions or []),
        ))
        return self

    def emits(self, name: str, *, severity: str = "medium",
              description: str = "") -> "App":
        """Declare an alert kind for the catalog. Optional — one is
        derived per handler when nothing is declared. Chainable."""
        self._emits.append(AlertType(name=name, severity=severity,
                                     description=description))
        return self

    def on_detection(
        self,
        *labels: str,
        camera: str | Sequence[str] | None = None,
        zone: str | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        dwell: float = 0.0,
        cooldown: float = 0.0,
        severity: str = "medium",
    ) -> Callable[[Callable[[DetectionEvent], Any]], Callable[[DetectionEvent], Any]]:
        """Register a rule. The handler is called once per detection
        that passes every filter, with a :class:`DetectionEvent`.

        ``labels``
            Class labels to react to (``"person"``, ``"car"``). None
            given ⇒ every label.
        ``camera``
            One camera id or a list; omitted ⇒ every camera.
        ``zone``
            Only fire when the object's centre is inside this
            configured zone. Declaring one adds the per-camera
            ``zones`` geometry param to the manifest, which is what
            gives the operator a zone editor in the catalog.
        ``min_confidence``
            Floor on detector confidence; defaults to
            :data:`DEFAULT_MIN_CONFIDENCE`.
        ``dwell``
            Seconds the object must have been continuously present
            before the handler runs, and then only ONCE per presence
            episode — the loitering pattern, without the state machine.
        ``cooldown``
            Minimum seconds between handler calls for the same object.
            The way to stop a parked car alerting on every frame.

        The handler may fire alerts with ``event.alert(...)``, or return
        an :class:`~.alerts.Alert` (or a list of them), or return
        nothing at all. All three are supported; ``event.alert`` is the
        one the documentation leads with.
        """
        if zone is not None:
            self._uses_zones = True
        if camera is None:
            cameras: tuple[str, ...] = ()
        elif isinstance(camera, str):
            cameras = (camera,)
        else:
            cameras = tuple(str(c) for c in camera)

        def decorate(fn: Callable[[DetectionEvent], Any]):
            self._rules.append(_Rule(
                fn=fn,
                labels=tuple(str(label).lower() for label in labels),
                cameras=cameras,
                zone=zone,
                min_confidence=float(min_confidence),
                dwell=float(dwell),
                cooldown=float(cooldown),
                severity=severity,
            ))
            return fn

        return decorate

    def on_event(
        self,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Escape hatch: see every inference event whole.

        The handler is called as ``fn(camera_id, detections, event)`` —
        the same triple :meth:`Detector.on_detections` receives — and
        may return alerts to fire. Use it for rules about the frame
        rather than about one object (crowding, absence, ratios), and
        drop to :class:`~.detector.Detector` when the app is mostly
        this."""

        def decorate(fn: Callable[..., Any]):
            self._raw_handlers.append(fn)
            return fn

        return decorate

    def on_setup(self) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Register a startup hook, called once with the parsed config
        before any event is handled — open a database, build a client,
        load a denylist."""

        def decorate(fn: Callable[[Any], Any]):
            self._setup_hooks.append(fn)
            return fn

        return decorate

    # ── Compilation ────────────────────────────────────────────────

    def manifest(self) -> AppManifest:
        """The :class:`~.manifest.AppManifest` this app compiles to —
        declared fields plus what the handlers imply."""
        params = list(self._params)
        if self._uses_zones and not any(p.name == "zones" for p in params):
            params.insert(0, Param(
                "zones", "geometry.polygon", default=None, per_camera=True,
                description="Regions this app watches on each camera.",
            ))
        emits = list(self._emits)
        if not emits:
            seen: dict[str, str] = {}
            for rule in self._rules:
                seen.setdefault(rule.name.replace("_", "-"), rule.severity)
            emits = [AlertType(name=n or self.id, severity=s)
                     for n, s in (seen.items() or {self.id: "medium"}.items())]
        kwargs = dict(self._manifest_kwargs)
        kwargs.setdefault("subscribes", "opennvr.inference.>")
        return AppManifest(
            id=self.id,
            name=self._name,
            version=self._version,
            category=self._category,
            summary=self._summary,
            requires_tasks=list(self._requires_tasks),
            params=params,
            emits=emits,
            **kwargs,
        )

    def config_class(self) -> type:
        """The dataclass ``config.yml`` is loaded into:
        :class:`~.config.BaseAppConfig` plus one field per declared
        param (and ``zones`` when any rule uses one)."""
        fields_spec: list[tuple[str, Any, Any]] = []
        names = {p.name for p in self._params}
        if self._uses_zones and "zones" not in names:
            fields_spec.append((
                "zones", Any,
                field(default_factory=dict),  # type: ignore[arg-type]
            ))
        for param in self._params:
            default = param.default
            if isinstance(default, (list, dict, set)):
                frozen = default
                spec: Any = field(default_factory=lambda frozen=frozen: type(frozen)(frozen))
            else:
                spec = field(default=default)
            fields_spec.append((param.name, Any, spec))
        return make_dataclass(
            f"{_pascal(self.id)}Config",
            fields_spec,
            bases=(BaseAppConfig,),
        )

    def load_config(self, path: str) -> Any:
        """Load ``path`` into :meth:`config_class`, defaulting the NATS
        subject to the inference broadcast."""
        cfg = load_app_config(path, self.config_class())
        if getattr(cfg, "subject_pattern", None) is None:
            cfg.subject_pattern = "opennvr.inference.>"
        return cfg

    def detector_class(self) -> type[Detector]:
        """Compile the app to a :class:`~.detector.Detector` subclass.

        This is the whole trick: the facade is a code generator with one
        output. Anything that accepts a Detector — the test helpers, the
        camera agent's runtime monitors, a custom runner — accepts this.
        """
        owner = self
        manifest = self.manifest()

        class FacadeDetector(Detector):
            __doc__ = f"{owner._name} — compiled from the App facade."

            def setup(self) -> None:
                owner.config = self.cfg
                ttl = max(
                    _STATE_TTL_FLOOR,
                    *(r.dwell * 2 for r in owner._rules),
                    *(r.cooldown * 2 for r in owner._rules),
                ) if owner._rules else _STATE_TTL_FLOOR
                self._facade_state: KeyedState = keyed_state(ttl)
                self._facade_zones: dict[str, dict[str, Zone]] = {}
                for hook in owner._setup_hooks:
                    hook(self.cfg)

            # ── Zones ──────────────────────────────────────────────

            def _zones_for(self, camera_id: str) -> dict[str, Zone]:
                cached = self._facade_zones.get(camera_id)
                if cached is not None:
                    return cached
                built: dict[str, Zone] = {}
                raw = getattr(self.cfg, "zones", None) or {}
                if isinstance(raw, dict):
                    per_camera = raw.get(camera_id)
                    source = per_camera if isinstance(per_camera, dict) else raw
                    for zone_name, vertices in source.items():
                        if not isinstance(vertices, (list, tuple)):
                            continue
                        try:
                            built[str(zone_name)] = Zone.from_config(
                                str(zone_name), scale_vertices(vertices, 1, 1),
                            )
                        except (ValueError, TypeError, IndexError) as exc:
                            logger.warning(
                                "%s: ignoring malformed zone %r on %s: %s",
                                owner.id, zone_name, camera_id, exc,
                            )
                self._facade_zones[camera_id] = built
                return built

            # ── Dispatch ───────────────────────────────────────────

            def on_detections(
                self,
                camera_id: str,
                detections: list[dict[str, Any]],
                event: dict[str, Any],
            ) -> list[Alert]:
                fired: list[Alert] = []
                now = self.parse_event_ts(event.get("completed_at"))
                zones = self._zones_for(camera_id)
                if owner._rules:
                    for detection in detections:
                        if not isinstance(detection, dict):
                            continue
                        fired.extend(
                            self._run_rules(detection, camera_id, event, zones, now)
                        )
                for handler in owner._raw_handlers:
                    try:
                        produced = handler(camera_id, detections, event)
                    except Exception:
                        logger.exception("%s: on_event handler failed", owner.id)
                        continue
                    fired.extend(_as_alerts(produced))
                return fired

            def _run_rules(
                self,
                detection: dict[str, Any],
                camera_id: str,
                event: dict[str, Any],
                zones: dict[str, Zone],
                now: float,
            ) -> list[Alert]:
                label = str(detection.get("label", "")).lower()
                track = detection.get("track_id")
                key = (camera_id, label, str(track) if track not in (None, "") else "-")
                record = self._facade_state.touch(key, at=now)
                fired: list[Alert] = []
                for rule in owner._rules:
                    ctx = DetectionEvent(
                        detection=detection,
                        camera=camera_id,
                        raw=event,
                        config=self.cfg,
                        state=self._facade_state,
                        zones=zones,
                        record=record,
                        key=key,
                        owner=owner,
                        severity=rule.severity,
                    )
                    if not rule.matches(ctx):
                        continue
                    if not self._gate(rule, record, now):
                        continue
                    try:
                        produced = rule.fn(ctx)
                    except Exception:
                        logger.exception(
                            "%s: rule %s failed on %s", owner.id, rule.name, camera_id,
                        )
                        continue
                    fired.extend(ctx._alerts)
                    fired.extend(_as_alerts(produced))
                    self._mark(rule, record, now)
                return fired

            @staticmethod
            def _gate(rule: _Rule, record: Any, now: float) -> bool:
                """Apply ``dwell`` and ``cooldown`` for one rule."""
                if rule.dwell > 0.0:
                    if record.age < rule.dwell:
                        return False
                    if record.data.get(f"_fired:{rule.name}"):
                        return False
                if rule.cooldown > 0.0:
                    last = record.data.get(f"_last:{rule.name}")
                    if last is not None and (now - float(last)) < rule.cooldown:
                        return False
                return True

            @staticmethod
            def _mark(rule: _Rule, record: Any, now: float) -> None:
                if rule.dwell > 0.0:
                    record.data[f"_fired:{rule.name}"] = True
                if rule.cooldown > 0.0:
                    record.data[f"_last:{rule.name}"] = now

        FacadeDetector.manifest = manifest
        FacadeDetector.__name__ = f"{_pascal(self.id)}App"
        FacadeDetector.__qualname__ = FacadeDetector.__name__
        return FacadeDetector

    # ── Lifecycle ──────────────────────────────────────────────────

    def build(self, config: Any, dispatcher: AlertDispatcher) -> Detector:
        """Instantiate the compiled detector directly — for tests and
        for embedding an app in another process."""
        return self.detector_class()(config, dispatcher)

    def run(self, argv: list[str] | None = None) -> int:
        """Parse the CLI, load the config, and run until signalled.
        The return value is the process exit code::

            if __name__ == "__main__":
                raise SystemExit(app.run())
        """
        if not self._rules and not self._raw_handlers:
            raise RuntimeError(
                f"App({self.id!r}).run(): no handlers registered — decorate "
                f"at least one function with @app.on_detection(...)"
            )
        return AppRunner(
            self.detector_class(), load_config=self.load_config,
        ).run(argv)


# ── Helpers ─────────────────────────────────────────────────────────


def _as_alerts(produced: Any) -> list[Alert]:
    """Normalize a handler's return value to a list of alerts."""
    if produced is None:
        return []
    if isinstance(produced, Alert):
        return [produced]
    if isinstance(produced, (list, tuple)):
        return [a for a in produced if isinstance(a, Alert)]
    return []


def _pascal(app_id: str) -> str:
    return "".join(part.capitalize() for part in app_id.replace("_", "-").split("-") if part)


__all__ = ["App", "DetectionEvent", "DEFAULT_MIN_CONFIDENCE"]
