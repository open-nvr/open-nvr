# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
The ``App`` facade — the front door of the App SDK.

Everything under this module compiles down to :class:`~.detector.Detector`;
nothing here is a new runtime. An app written against the facade and an
app written against ``Detector`` produce the same process, the same
manifest, the same alerts and the same contract surface. The facade
exists because an app author should not have to know about NATS
subjects, inference envelopes, normalized bboxes, alert dispatchers,
keyed TTL state or method-override hooks to write a rule.

A whole app::

    from opennvr_app_sdk import App

    app = App("driveway-watch", name="Driveway Watch", category="perimeter")
    app.param("dwell_s", float, default=30.0)

    @app.on_detection("person", zone="driveway", dwell="$dwell_s")
    def loitering(event):
        event.alert(f"Person loitering on {event.camera}", severity="high")

    if __name__ == "__main__":
        raise SystemExit(app.run())

Declaring a surface implements it
---------------------------------

The catalog renders an app from what its manifest declares. The facade's
whole thesis is that the declaration and the implementation should be
the same line of code, so every decorator registers both:

===========================  ===========================================
``@app.on_detection(...)``   the rule; ``zone=`` also declares the
                             per-camera zone the operator draws
``@app.on_event()``          the raw ``(camera_id, detections, event)``
                             triple, for rules about the whole frame
``app.param(...)``           a config-form field, a ``config.yml`` key
                             and an attribute on ``event.config``
``app.metric`` / ``gauge`` / a dashboard tile over ``@app.state``
``table`` / ``log`` /
``gallery``
``@app.state()``             what ``GET /state`` returns
``@app.action(...)``         an operator button, with a typed form
``@app.ui()``                an embedded HTML dashboard
``@app.on_license()``        the licence gate for a paid app
``@app.on_config()``         live config, delivered without a restart
``@app.on_setup()`` /        process lifecycle
``@app.on_shutdown()``
===========================  ===========================================

And from inside a rule, ``event.alert()`` reaches an operator,
``event.publish()`` reaches other apps, and ``event.nvr`` is the
platform — cameras, snapshots, recordings, timeline, durable state.

When a rule outgrows this, drop to :class:`~.detector.Detector`: the
facade is additive and the base classes are unchanged.
"""
from __future__ import annotations

import copy
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass, field, make_dataclass
from typing import Any, Callable, Iterable, Sequence

from .alerts import Alert, AlertDispatcher
from .config import BaseAppConfig, load_app_config
from .contract import Entitlement
from .detector import AppRunner, Detector
from .geometry import Point, Zone, bbox_center, scale_vertices
from .manifest import Action, AlertType, AppManifest, Param, StateView
from .state import KeyedState, keyed_state

logger = logging.getLogger(__name__)

#: Floor on how long an object may go unseen before the facade decides
#: it left. The default a rule actually uses is
#: ``max(DEFAULT_ABSENCE_S, dwell)`` — a gap has to be long relative to
#: what is being measured to count as an absence, and detection streams
#: vary from several frames a second to one every few seconds.
#: Override per rule with ``forget=``.
DEFAULT_ABSENCE_S = 30.0

#: Kept for compatibility. The facade no longer applies a hidden
#: confidence floor: a rule without ``min_confidence=`` sees every
#: detection, so the only threshold in an app is the one it declares.
DEFAULT_MIN_CONFIDENCE = 0.0

_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Manifest fields the facade derives from the decorators. Passing one
#: to ``App(...)`` would silently lose whatever the decorators declared,
#: so it is refused with a pointer to the decorator that owns it.
_DERIVED_FIELDS = {
    "id": "the first argument to App()",
    "params": "app.param(...)",
    "emits": "app.emits(...) or the rule's severity=",
    "state_schema": "app.metric/gauge/table/log/gallery(...)",
    "actions": "@app.action(...)",
    "has_ui": "@app.ui()",
    "entitlement": "@app.on_license()",
}

#: The detector currently handling a call, so ``app.config`` / ``app.store``
#: / ``app.nvr`` resolve to the right one when several apps share a
#: process (the camera agent's runtime monitors do this).
_ACTIVE: ContextVar[Any] = ContextVar("opennvr_app_sdk_active_app", default=None)


# ── Config-bound filter values ──────────────────────────────────────


@dataclass(frozen=True)
class Setting:
    """A rule filter that reads an operator-set config value.

    Decorator arguments are evaluated at import time, so a literal
    ``dwell=30`` can never follow the config form. Wrap the param name
    instead — ``dwell=setting("dwell_s")``, or the shorthand
    ``dwell="$dwell_s"`` — and the value is read from the parsed config
    when the app starts."""

    name: str

    def resolve(self, config: Any) -> Any:
        if not hasattr(config, self.name):
            raise ValueError(
                f"rule filter refers to setting {self.name!r}, which this app "
                f"does not declare — add app.param({self.name!r}, ...)"
            )
        return getattr(config, self.name)


def setting(name: str) -> Setting:
    """Bind a rule filter to a config value: ``dwell=setting("dwell_s")``.
    ``dwell="$dwell_s"`` means the same thing."""
    return Setting(name)


def _spec(value: Any) -> Any:
    """Normalize a filter argument: ``"$name"`` becomes a :class:`Setting`."""
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        return Setting(value[1:])
    return value


def _resolve(value: Any, config: Any, default: Any) -> Any:
    if isinstance(value, Setting):
        value = value.resolve(config)
    return default if value is None else value


# ── The event object ────────────────────────────────────────────────


class DetectionEvent:
    """One detection, in context — what a ``@app.on_detection`` handler
    is called with.

    The object is deliberately flat: what a rule asks about is an
    attribute or a one-word method, and the raw envelope stays available
    as :attr:`raw` for anything the facade does not model.

    Coordinates are NORMALIZED (0–1 of the frame) throughout, matching
    the platform's ``NormalizedBBox`` wire shape, so a rule written
    against one camera resolution works on all of them.
    """

    __slots__ = (
        "detection", "camera", "raw", "config", "state", "_zones",
        "_alerts", "_record", "_app", "_severity", "_owner",
    )

    def __init__(
        self,
        *,
        detection: dict[str, Any],
        camera: str,
        raw: dict[str, Any],
        config: Any,
        zones: dict[str, Zone],
        owner: "App",
        state: KeyedState | None = None,
        record: Any = None,
        severity: str = "medium",
    ) -> None:
        self.detection = detection
        self.camera = camera
        self.raw = raw
        self.config = config
        self.state = state
        self._zones = zones
        self._record = record
        self._app = owner
        self._severity = severity
        self._alerts: list[Alert] = []

    def _bind(self, record: Any, state: KeyedState) -> None:
        """Attach the presence record once the rule's filters have
        passed — dwell must not accrue while the object is outside the
        zone or below the confidence floor."""
        self._record = record
        self.state = state

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
        still keeps a parked car from re-alerting on every frame."""
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
        """True when the detection's centre falls inside a zone the
        operator drew. With no ``name``, true when it falls inside ANY
        of this app's zones.

        A zone with no polygon drawn yet is False, not an error: zones
        are operator config and a camera may simply not have one."""
        if name is None:
            return any(z.contains(self.center) for z in self._zones.values())
        zone = self._zones.get(name)
        return bool(zone and zone.contains(self.center))

    @property
    def zone(self) -> str | None:
        """Name of the first zone containing the detection, or ``None``
        when it is outside every zone."""
        point = self.center
        for zone_name, zone in self._zones.items():
            if zone.contains(point):
                return zone_name
        return None

    @property
    def zones(self) -> list[str]:
        """Every zone the detection falls inside."""
        point = self.center
        return [n for n, z in self._zones.items() if z.contains(point)]

    # ── When, and for how long ─────────────────────────────────────

    @property
    def dwell_s(self) -> float:
        """Seconds this object has continuously satisfied THIS rule.

        The clock starts when the rule's filters first match, so
        ``zone="driveway", dwell=30`` means thirty seconds *in the
        driveway* — not thirty seconds on camera followed by one frame
        in the driveway. A gap longer than ``forget=`` ends the episode.
        """
        return 0.0 if self._record is None else float(self._record.age)

    @property
    def first_seen(self) -> bool:
        """True on the first event of a presence episode."""
        if self._record is None:
            return True
        return self._record.first_seen == self._record.last_seen

    @property
    def ts(self) -> float:
        """POSIX timestamp of the inference event."""
        return 0.0 if self._record is None else float(self._record.last_seen)

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
        """The platform's id for this inference. Thread it onto anything
        you emit and the whole causal chain — inference, audit line,
        evidence frame, alert — joins up in the timeline."""
        return str(self.raw.get("correlation_id") or "")

    @property
    def adapter(self) -> str:
        """Which detector produced this — an adapter name, or ``tier0``
        for the always-on detector every camera runs."""
        return str(self.raw.get("adapter") or "")

    # ── The platform ───────────────────────────────────────────────

    @property
    def nvr(self):
        """The platform client, built from this app's own config and
        credential: ``event.nvr.cameras()``, ``.timeline.search(...)``,
        ``.state.set(...)``, ``.ai.infer(...)``. See
        :class:`~.client.OpenNVR`."""
        return self._app.nvr

    def snapshot(self) -> bytes | None:
        """The current frame from this event's camera, as JPEG bytes."""
        return self.nvr.snapshot(self.camera)

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
        """Fire an alert — the thing a human sees.

        Everything the platform needs is filled in from the event:
        camera, correlation id, label, confidence, track, zone and
        dwell. ``severity`` defaults to the rule's, so the manifest's
        ``emits`` block and the alerts actually fired cannot disagree.

        The alert is dispatched whether or not you return it; the return
        value is there so a handler can adjust it first."""
        body = dict(evidence or {})
        body.setdefault("label", self.label)
        body.setdefault("confidence", self.confidence)
        if self.adapter:
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

    def publish(self, schema: str, payload: dict[str, Any], *,
                camera_id: str | None = None) -> bool:
        """Publish a contracted domain event — the thing another APP
        sees. The envelope, the producer (``app:<id>``), the camera and
        the correlation id are filled in; ``schema`` and ``payload`` are
        yours (docs/EVENT_CONTRACTS.md)."""
        return self._app.publisher.publish(
            schema, camera_id=camera_id or self.camera, payload=payload,
            correlation_id=self.correlation_id or None,
        )

    def publish_typed(self, payload: Any, *, camera_id: str | None = None) -> bool:
        """As :meth:`publish`, from a typed payload
        (``PlateRecognized(...)``) — the schema comes from the class, so
        a missing required field fails here rather than in someone
        else's app."""
        return self._app.publisher.publish_typed(
            payload, camera_id=camera_id or self.camera,
            correlation_id=self.correlation_id or None,
        )

    # ── Remembering across events ──────────────────────────────────

    def remember(self, **values: Any) -> None:
        """Stash values on this object's presence record — readable on
        the next event for the same object via :meth:`recall`."""
        if self._record is not None:
            self._record.data.update(values)

    def recall(self, name: str, default: Any = None) -> Any:
        """Read back what :meth:`remember` stored for this object."""
        if self._record is None:
            return default
        return self._record.data.get(name, default)

    def __repr__(self) -> str:  # pragma: no cover — debugging aid
        return (
            f"<DetectionEvent {self.label} on {self.camera} "
            f"conf={self.confidence:.2f} zone={self.zone!r} "
            f"dwell={self.dwell_s:.1f}s>"
        )


# ── Rules ───────────────────────────────────────────────────────────


@dataclass
class _Rule:
    """One registered ``@app.on_detection`` handler and its filters, as
    written. Values that may be :class:`Setting` references stay
    unresolved until an app is built with a config."""

    fn: Callable[[DetectionEvent], Any]
    index: int
    labels: tuple[str, ...]
    cameras: tuple[str, ...]
    zone: str | None
    min_confidence: Any
    dwell: Any
    cooldown: Any
    forget: Any
    severity: str
    emits: str | None

    @property
    def key(self) -> str:
        """Identity for state and logs. Positional, never the function
        name: two lambdas, or two handlers that happen to share a name,
        must not share a dwell latch."""
        return f"{self.index}:{getattr(self.fn, '__name__', 'rule')}"

    @property
    def label(self) -> str:
        """Human name for logs and the derived alert type."""
        return getattr(self.fn, "__name__", "rule")


@dataclass
class _Resolved:
    """A rule with its config-bound filters resolved, plus the presence
    state that belongs to it alone."""

    rule: _Rule
    min_confidence: float
    dwell: float
    cooldown: float
    forget: float
    severity: str
    state: KeyedState

    def presence(self, key: tuple, now: float) -> Any:
        """The presence record for one object, starting a fresh episode
        when it has been away longer than ``forget``.

        ``KeyedState.touch`` deliberately never evicts the key it is
        touching, so an object that leaves and returns would otherwise
        keep one endless episode — and its once-per-episode latch would
        never re-arm, which is how the second person of the day stops
        alerting. The cooldown memory in ``data`` survives the reset,
        because "don't alert about this again for an hour" should not be
        defeated by a brief occlusion."""
        existing = self.state.get(key)
        if existing is not None and (now - existing.last_seen) > self.forget:
            existing.first_seen = now
            existing.alerted = False
        return self.state.touch(key, at=now)

    def matches(self, event: DetectionEvent) -> bool:
        rule = self.rule
        if rule.labels and event.label not in rule.labels:
            return False
        if rule.cameras and event.camera not in rule.cameras:
            return False
        if event.confidence < self.min_confidence:
            return False
        if rule.zone is not None and not event.in_zone(rule.zone):
            return False
        return True


# ── The app ─────────────────────────────────────────────────────────


class App:
    """A whole OpenNVR app: identity, config, rules, surfaces, lifecycle.

    Construct one at module scope, decorate handlers on it, and call
    :meth:`run` from ``__main__``. The NATS loop, alert dispatch, the
    contract server, registry self-registration, live config, the CLI
    and signal handling are inherited from :class:`~.detector.Detector`,
    which this compiles to.

    Constructor arguments beyond the ones below are passed to
    :class:`~.manifest.AppManifest`, so anything the catalog understands
    (``description``, ``use_cases``, ``pricing``, ``requires_scopes``,
    ``provides``, ``author`` …) is available without leaving the facade.
    Fields the decorators derive — ``params``, ``emits``,
    ``state_schema``, ``actions``, ``has_ui``, ``entitlement`` — are
    refused here, with a pointer to the decorator that owns them.
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
        consume_tier0: bool = True,
        **manifest_kwargs: Any,
    ) -> None:
        if not app_id or not app_id.strip():
            raise ValueError("App(app_id): an app id is required")
        app_id = app_id.strip()
        if not _ID_RE.match(app_id):
            raise ValueError(
                f"App({app_id!r}): the app id must be kebab-case — lowercase "
                f"letters and digits, single hyphens between words "
                f"(e.g. 'gate-watch'). It becomes the app's identity "
                f"everywhere: the catalog entry, the NATS subject, the "
                f"container name."
            )
        for field_name, owner in _DERIVED_FIELDS.items():
            if field_name in manifest_kwargs:
                raise TypeError(
                    f"App({app_id!r}, {field_name}=…): the facade derives "
                    f"{field_name!r} from your decorators — declare it with "
                    f"{owner} instead."
                )
        self.id = app_id
        self._name = name or app_id.replace("-", " ").title()
        self._version = version
        self._category = category
        self._summary = summary
        self._requires_tasks = list(requires_tasks or ["object_detection"])
        #: Consume the always-on Tier-0 detector. True by default,
        #: because on a stock install Tier-0 is the ONLY detection
        #: stream on the bus — an app that ignores it registers, shows a
        #: green dot, and fires nothing, forever. Set False when the app
        #: also subscribes to a heavy adapter and would otherwise see
        #: every object twice.
        self.consume_tier0 = bool(consume_tier0)
        self._manifest_kwargs = manifest_kwargs

        self._rules: list[_Rule] = []
        self._raw_handlers: list[Callable[..., Any]] = []
        self._setup_hooks: list[Callable[[Any], Any]] = []
        self._shutdown_hooks: list[Callable[[], Any]] = []
        self._config_hooks: list[Callable[[dict[str, Any]], Any]] = []
        self._params: list[Param] = []
        self._zones: dict[str, str] = {}
        self._emits: list[AlertType] = []
        self._views: list[StateView] = []
        self._state_fn: Callable[[], dict[str, Any]] | None = None
        self._actions: list[tuple[Action, Callable[..., Any]]] = []
        self._ui_fn: Callable[[], str] | None = None
        self._license_fn: Callable[[str], Any] | None = None
        self._publishes: list[str] = []
        self._last_detector: Any = None

    # ── Declaration: config and zones ──────────────────────────────

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
        ``config.yml`` fills it), and an attribute on ``event.config``.
        Bind a rule filter to it with ``"$name"``. Chainable."""
        self._check_name(name, "param")
        self._params.append(Param(
            name=name, type=type_, default=default, per_camera=per_camera,
            description=description, required=required,
            suggestions=list(suggestions or []),
        ))
        return self

    def zone(self, name: str, description: str = "") -> "App":
        """Declare a zone the operator draws on each camera.

        ``@app.on_detection(..., zone="driveway")`` declares it too;
        call this to give it a description the operator will see in the
        catalog's geometry editor. Chainable."""
        self._check_name(name, "zone")
        self._zones[name] = description or self._zones.get(name, "")
        return self

    def emits(self, name: str, *, severity: str = "medium",
              description: str = "") -> "App":
        """Declare an alert kind for the catalog. Optional — one is
        derived per rule otherwise. Chainable."""
        self._emits.append(AlertType(name=name, severity=severity,
                                     description=description))
        return self

    def publishes(self, schema: str) -> "App":
        """Declare a domain event this app publishes, so it appears in
        the app's AsyncAPI document. Chainable."""
        if schema not in self._publishes:
            self._publishes.append(schema)
        return self

    def _check_name(self, name: str, kind: str) -> None:
        if not _NAME_RE.match(name or ""):
            raise ValueError(
                f"{kind} name {name!r} must be snake_case — a lowercase "
                f"letter followed by lowercase letters, digits or "
                f"underscores. It becomes a config key and a form field."
            )
        taken = {p.name for p in self._params} | set(self._zones)
        if name in taken:
            raise ValueError(
                f"{kind} name {name!r} is already declared by this app — "
                f"params and zones share one config namespace."
            )

    # ── Declaration: rules ─────────────────────────────────────────

    def on_detection(
        self,
        *labels: str,
        camera: str | Sequence[str] | None = None,
        zone: str | None = None,
        min_confidence: Any = None,
        dwell: Any = 0.0,
        cooldown: Any = 0.0,
        forget: Any = None,
        severity: str = "medium",
        emits: str | None = None,
    ) -> Callable[[Callable[[DetectionEvent], Any]], Callable[[DetectionEvent], Any]]:
        """Register a rule. The handler is called once per detection
        that passes every filter, with a :class:`DetectionEvent`.

        ``labels``
            Class labels to react to (``"person"``, ``"car"``). None
            given ⇒ every label.
        ``camera``
            One camera id or a list; omitted ⇒ every camera.
        ``zone``
            Only fire inside this zone. Declaring one adds a per-camera
            ``geometry.polygon`` param of that name, which is how the
            operator knows which zones to draw and where each one goes.
        ``min_confidence``
            Floor on detector confidence. No hidden default: omit it and
            every detection reaches the rule.
        ``dwell``
            Seconds the object must have satisfied THIS rule's filters
            continuously before the handler runs, and then only once per
            presence episode — the loitering pattern without the state
            machine. The clock starts when the filters first match, so a
            zone rule times presence *in the zone*.
        ``cooldown``
            Minimum seconds between handler calls for the same object.
            What stops a parked car alerting on every frame.
        ``forget``
            Seconds without a sighting that end a presence episode and
            re-arm ``dwell``. Defaults to
            ``max(DEFAULT_ABSENCE_S, dwell)`` — long enough that a few
            dropped frames are not mistaken for the object leaving.
        ``severity``
            Default severity for alerts this rule fires, and the
            severity of the alert type derived for the manifest.
        ``emits``
            Name of the alert type this rule fires. Defaults to the
            handler's name, slugified; declare it when the name matters
            (it is part of the catalog listing, so a later rename of the
            function would otherwise change the app's public contract).

        The handler may fire alerts with ``event.alert(...)``, return an
        :class:`~.alerts.Alert` (or a list), or return nothing.
        """
        if zone is not None:
            self.zone(zone) if zone not in self._zones else None
        if camera is None:
            cameras: tuple[str, ...] = ()
        elif isinstance(camera, str):
            cameras = (camera,)
        else:
            cameras = tuple(str(c) for c in camera)

        def decorate(fn: Callable[[DetectionEvent], Any]):
            self._rules.append(_Rule(
                fn=fn,
                index=len(self._rules),
                labels=tuple(str(label).lower() for label in labels),
                cameras=cameras,
                zone=zone,
                min_confidence=_spec(min_confidence),
                dwell=_spec(dwell),
                cooldown=_spec(cooldown),
                forget=_spec(forget),
                severity=severity,
                emits=emits,
            ))
            return fn

        return decorate

    def on_event(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Escape hatch: see every inference event whole.

        The handler is called as ``fn(camera_id, detections, event)`` —
        the triple :meth:`Detector.on_detections` receives — and may
        return alerts to fire. Use it for rules about the frame rather
        than about one object (crowding, absence, ratios)."""

        def decorate(fn: Callable[..., Any]):
            self._raw_handlers.append(fn)
            return fn

        return decorate

    # ── Declaration: the app's surfaces ────────────────────────────

    def state(self) -> Callable[[Callable[[], dict[str, Any]]], Callable[[], dict[str, Any]]]:
        """Register what ``GET /state`` returns — the live data the
        catalog renders through the tiles declared below.

        Anything in :attr:`store` is included automatically, so an app
        that only keeps counters needs no ``@app.state`` at all."""

        def decorate(fn: Callable[[], dict[str, Any]]):
            self._state_fn = fn
            return fn

        return decorate

    def metric(self, path: str, *, label: str | None = None,
               description: str = "") -> "App":
        """A single number from ``/state``, shown as a stat chip."""
        return self._view("metric", path, label, description)

    def gauge(self, path: str, *, label: str | None = None, min: float = 0.0,
              max: float = 100.0, warn: float | None = None,
              danger: float | None = None, unit: str = "",
              description: str = "") -> "App":
        """A number between bounds, shown as a bar — amber past ``warn``,
        red past ``danger``."""
        return self._view("gauge", path, label, description, min=min, max=max,
                          warn=warn, danger=danger, unit=unit)

    def table(self, path: str, *, label: str | None = None,
              columns: Sequence[str] = (), description: str = "") -> "App":
        """A list from ``/state``, shown as a table."""
        return self._view("table", path, label, description,
                          columns=list(columns))

    def log(self, path: str, *, label: str | None = None, limit: int = 20,
            description: str = "") -> "App":
        """A recent-events feed, newest first."""
        return self._view("log", path, label, description, limit=limit)

    def gallery(self, path: str, *, label: str | None = None, limit: int = 12,
                description: str = "") -> "App":
        """A thumbnail wall — plate crops, doorbell snapshots. Entries
        are ``{image|url, label, time}``; ``image`` may be a data URI."""
        return self._view("gallery", path, label, description, limit=limit)

    def _view(self, kind: str, path: str, label: str | None,
              description: str, **extra: Any) -> "App":
        name = _SLUG_RE.sub("-", path.lower()).strip("-") or kind
        self._views.append(StateView(
            name=name, label=label or path.replace("_", " ").capitalize(),
            kind=kind, path=path, description=description, **extra,
        ))
        return self

    def action(
        self,
        name: str,
        *,
        label: str | None = None,
        params: Sequence[Param] = (),
        description: str = "",
        confirm: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register an operator verb — a button in the catalog with a
        form generated from ``params``.

        The handler is called with the declared params as keyword
        arguments, defaults filled in. Raise ``ValueError`` for params
        you reject (the operator sees the message); return anything
        JSON-serializable.

        Actions are reached only through core's proxy, which is
        **user-JWT only**: an action is always invoked by a person, and
        :func:`~.usercontext.current_user` tells you which one."""
        self._check_action_name(name)

        def decorate(fn: Callable[..., Any]):
            self._actions.append((Action(
                name=name, label=label or name.replace("_", " ").capitalize(),
                params=list(params), description=description or (fn.__doc__ or "").strip(),
                confirm=confirm,
            ), fn))
            return fn

        return decorate

    def _check_action_name(self, name: str) -> None:
        if not _NAME_RE.match(name or ""):
            raise ValueError(
                f"action name {name!r} must be snake_case — it becomes the "
                f"URL path POST /actions/{name}."
            )
        if any(a.name == name for a, _ in self._actions):
            raise ValueError(f"action {name!r} is already declared by this app")

    def ui(self) -> Callable[[Callable[[], str]], Callable[[], str]]:
        """Register an HTML dashboard, served at ``GET /ui`` and
        rendered sandboxed inside the catalog. Return a string."""

        def decorate(fn: Callable[[], str]):
            self._ui_fn = fn
            return fn

        return decorate

    def on_license(self) -> Callable[[Callable[[str], Any]], Callable[[str], Any]]:
        """Register the licence gate for a paid app.

        Declaring it sets ``entitlement="license_key"``: the catalog
        collects a key from the administrator and refuses to enable the
        app until this function says it is good. Return an
        :class:`~.contract.Entitlement`, or a bool for the simple case.
        OpenNVR takes no part in the transaction."""

        def decorate(fn: Callable[[str], Any]):
            self._license_fn = fn
            return fn

        return decorate

    # ── Declaration: lifecycle ─────────────────────────────────────

    def on_setup(self) -> Callable[[Callable[[Any], Any]], Callable[[Any], Any]]:
        """Run once with the parsed config before any event — open a
        database, build a client, load a denylist."""

        def decorate(fn: Callable[[Any], Any]):
            self._setup_hooks.append(fn)
            return fn

        return decorate

    def on_config(self) -> Callable[[Callable[[dict], Any]], Callable[[dict], Any]]:
        """Run when an operator changes the app's config, which core
        re-delivers without a restart. Called with the new config dict;
        make it idempotent — the first call usually restates what boot
        already applied. Zones are re-read for you either way."""

        def decorate(fn: Callable[[dict], Any]):
            self._config_hooks.append(fn)
            return fn

        return decorate

    def on_shutdown(self) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
        """Run on the way out (SIGINT / SIGTERM), after the event loop
        stops — close a database, flush a buffer. The SDK's own
        resources are closed for you."""

        def decorate(fn: Callable[[], Any]):
            self._shutdown_hooks.append(fn)
            return fn

        return decorate

    # ── The running app, from a handler ────────────────────────────

    @property
    def _detector(self) -> Any:
        active = _ACTIVE.get()
        return active if active is not None else self._last_detector

    @property
    def config(self) -> Any:
        """The parsed config of the running app. ``event.config`` is the
        same object and is what a rule should normally use."""
        detector = self._detector
        if detector is None:
            raise RuntimeError(
                f"App({self.id!r}).config is only available once the app is "
                f"running — read it in @app.on_setup(cfg) or from event.config."
            )
        return detector.cfg

    @property
    def store(self) -> dict[str, Any]:
        """A plain dict the app can keep counters and recent items in.
        Its contents are merged into ``GET /state``, so declaring
        ``app.metric("alerted")`` and doing ``app.store["alerted"] += 1``
        is a complete dashboard."""
        detector = self._detector
        if detector is None:
            raise RuntimeError(
                f"App({self.id!r}).store is only available once the app is "
                f"running — seed it in @app.on_setup()."
            )
        return detector.store

    @property
    def nvr(self):
        """The platform client, built once from this app's own config
        and credential (:class:`~.client.OpenNVR`)."""
        detector = self._detector
        if detector is None:
            raise RuntimeError(
                f"App({self.id!r}).nvr is only available once the app is "
                f"running — use it from a rule, an action or @app.on_setup()."
            )
        return detector.nvr

    @property
    def publisher(self):
        """The domain-event publisher for this app
        (:class:`~.domain_events.DomainEventPublisher`). ``event.publish``
        is the usual way in."""
        detector = self._detector
        if detector is None:
            raise RuntimeError(
                f"App({self.id!r}).publisher is only available once the app "
                f"is running."
            )
        return detector.publisher

    # ── Compilation ────────────────────────────────────────────────

    def manifest(self) -> AppManifest:
        """The :class:`~.manifest.AppManifest` this app compiles to —
        declared fields plus everything the decorators imply."""
        params: list[Param] = []
        for zone_name in self._zones:
            params.append(Param(
                zone_name, "geometry.polygon", default=None, per_camera=True,
                description=self._zones[zone_name]
                or f"The {zone_name.replace('_', ' ')} area on each camera.",
            ))
        params.extend(self._params)

        emits = list(self._emits)
        if not emits:
            seen: dict[str, str] = {}
            for rule in self._rules:
                seen.setdefault(rule.emits or _slug(rule.label) or self.id,
                                rule.severity)
            emits = [AlertType(name=n, severity=s) for n, s in seen.items()] or [
                AlertType(name=self.id, severity="medium")]

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
            state_schema=list(self._views),
            actions=[a for a, _ in self._actions],
            has_ui=self._ui_fn is not None,
            entitlement="license_key" if self._license_fn else "none",
            **kwargs,
        )

    def config_class(self) -> type:
        """The dataclass ``config.yml`` is loaded into:
        :class:`~.config.BaseAppConfig` plus one field per zone and per
        declared param."""
        fields_spec: list[tuple[str, Any, Any]] = [
            ("consume_tier0", Any, field(default=self.consume_tier0)),
        ]
        for zone_name in self._zones:
            fields_spec.append((zone_name, Any, field(default_factory=dict)))
        for param in self._params:
            default = param.default
            if isinstance(default, (list, dict, set)):
                spec: Any = field(
                    default_factory=lambda frozen=default: copy.deepcopy(frozen))
            else:
                spec = field(default=default)
            fields_spec.append((param.name, Any, spec))
        return make_dataclass(
            f"{_pascal(self.id)}Config", fields_spec, bases=(BaseAppConfig,),
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
        return _build_detector_class(owner, manifest)

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


# ── The compiled detector ───────────────────────────────────────────


def _build_detector_class(owner: App, manifest: AppManifest) -> type[Detector]:
    """Build the ``Detector`` subclass an :class:`App` compiles to.

    Kept out of :meth:`App.detector_class` so the generated class is a
    normal module-level construct with readable tracebacks.
    """

    class FacadeDetector(Detector):
        __doc__ = f"{manifest.name} — compiled from the App facade."

        # ── Lifecycle ──────────────────────────────────────────────

        def setup(self) -> None:
            owner._last_detector = self
            self.store: dict[str, Any] = {}
            self._nvr: Any = None
            self._publisher: Any = None
            self._resolved: list[_Resolved] = []
            self._facade_zones: dict[str, dict[str, Zone]] = {}
            self._clocks: dict[str, float] = {}
            self._gc_clock: float = 0.0
            self._warned_zones: set[str] = set()

            for rule in owner._rules:
                dwell = float(_resolve(rule.dwell, self.cfg, 0.0) or 0.0)
                cooldown = float(_resolve(rule.cooldown, self.cfg, 0.0) or 0.0)
                forget = _resolve(rule.forget, self.cfg, None)
                forget = (float(forget) if forget is not None
                          else max(DEFAULT_ABSENCE_S, dwell))
                floor = _resolve(rule.min_confidence, self.cfg, None)
                self._resolved.append(_Resolved(
                    rule=rule,
                    min_confidence=float(floor) if floor is not None else 0.0,
                    dwell=dwell,
                    cooldown=cooldown,
                    forget=forget,
                    severity=rule.severity,
                    # Per rule, so two rules never share a latch, and
                    # sized by what ENDS an episode plus what the
                    # cooldown must remember — never by how long an
                    # episode lasts, or a record would outlive the
                    # object by hours whenever dwell is large.
                    state=keyed_state(max(forget, cooldown) + forget,
                                      auto_gc=False),
                ))
            for hook in owner._setup_hooks:
                hook(self.cfg)

        async def run(self, *, once: bool = False) -> None:
            token = _ACTIVE.set(self)
            try:
                await super().run(once=once)
            finally:
                _ACTIVE.reset(token)
                self._facade_close()

        def _facade_close(self) -> None:
            for hook in owner._shutdown_hooks:
                try:
                    hook()
                except Exception:
                    logger.exception("%s: shutdown hook failed", owner.id)
            if self._publisher is not None:
                try:
                    self._publisher.close()
                except Exception:
                    logger.exception("%s: closing the publisher failed", owner.id)
                self._publisher = None
            if self._nvr is not None:
                try:
                    self._nvr.close()
                except Exception:
                    logger.exception("%s: closing the platform client failed",
                                     owner.id)
                self._nvr = None

        # ── Lazily-built collaborators ─────────────────────────────

        @property
        def nvr(self):
            if self._nvr is None:
                from .client import OpenNVR

                self._nvr = OpenNVR(
                    getattr(self.cfg, "opennvr_url", None) or None,
                    token=getattr(self.cfg, "opennvr_token", None) or None,
                    client_id=owner.id,
                )
            return self._nvr

        @property
        def publisher(self):
            if self._publisher is None:
                from .domain_events import DomainEventPublisher

                self._publisher = DomainEventPublisher(
                    self.cfg.nats_url,
                    token=getattr(self.cfg, "nats_token", None),
                    producer=f"app:{owner.id}",
                )
            return self._publisher

        # ── Zones ──────────────────────────────────────────────────

        def _zones_for(self, camera_id: str) -> dict[str, Zone]:
            """The zones configured for one camera.

            Wire shape, set by the catalog's geometry editor and checked
            by core: each zone is its own ``per_camera`` param, whose
            value is ``{camera_id: [[x, y], …]}`` in normalized
            coordinates. A bare list is accepted as "this polygon on
            every camera", for hand-written config."""
            cached = self._facade_zones.get(camera_id)
            if cached is not None:
                return cached
            built: dict[str, Zone] = {}
            for zone_name in owner._zones:
                raw = getattr(self.cfg, zone_name, None)
                if isinstance(raw, dict):
                    vertices = raw.get(camera_id)
                elif isinstance(raw, (list, tuple)):
                    vertices = raw
                else:
                    vertices = None
                if not isinstance(vertices, (list, tuple)) or not vertices:
                    continue
                try:
                    built[zone_name] = Zone.from_config(
                        zone_name, scale_vertices(vertices, 1, 1))
                except (ValueError, TypeError, IndexError) as exc:
                    logger.warning("%s: ignoring malformed zone %r on %s: %s",
                                   owner.id, zone_name, camera_id, exc)
            self._facade_zones[camera_id] = built
            return built

        def _warn_missing_zone(self, rule: _Rule, camera_id: str) -> None:
            """An app filtering on a zone nobody has drawn is silent
            forever. Say so once, per rule per camera."""
            token = f"{rule.key}@{camera_id}"
            if token in self._warned_zones:
                return
            self._warned_zones.add(token)
            logger.warning(
                "%s: rule %s watches zone %r, but no polygon is configured for "
                "camera %s — draw it in the App Catalog (Settings → Apps → %s) "
                "or the rule can never fire there.",
                owner.id, rule.label, rule.zone, camera_id, manifest.name,
            )

        def on_config_update(self, config: dict[str, Any]) -> None:
            """Live config: apply the new values, drop the zone cache so
            a redrawn polygon takes effect, and tell the app."""
            declared = {p.name for p in owner._params} | set(owner._zones)
            for key, value in (config or {}).items():
                if key in declared or hasattr(self.cfg, key):
                    setattr(self.cfg, key, value)
            self._facade_zones.clear()
            self._warned_zones.clear()
            token = _ACTIVE.set(self)
            try:
                for hook in owner._config_hooks:
                    try:
                        hook(dict(config or {}))
                    except Exception:
                        logger.exception("%s: on_config hook failed", owner.id)
            finally:
                _ACTIVE.reset(token)

        # ── Event time ─────────────────────────────────────────────

        def _event_time(self, camera_id: str, event: dict[str, Any]) -> float:
            """A non-decreasing clock per camera, taken from the events
            themselves.

            Per camera, because cameras are independent timelines and a
            few seconds of skew between them is normal — a global clock
            would read a slightly-behind camera's events as
            out-of-order and freeze its presence timers.

            Non-decreasing, because ``parse_event_ts`` falls back to the
            WALL clock for a missing or malformed ``completed_at``.
            Mixing that into event time let one undated event jump years
            ahead and garbage-collect every other camera's presence
            record. A value that goes backwards, or more than an hour
            forward, is treated as noise and the last good time stands."""
            raw = event.get("completed_at")
            parsed = self.parse_event_ts(raw) if isinstance(raw, str) else None
            current = self._clocks.get(camera_id)
            if current is None:
                current = parsed if parsed is not None else self._gc_clock
            elif parsed is not None and current <= parsed <= current + 3600:
                current = parsed
            self._clocks[camera_id] = current
            self._gc_clock = max(self._gc_clock, current)
            return current

        # ── Dispatch ───────────────────────────────────────────────

        def on_detections(
            self,
            camera_id: str,
            detections: list[dict[str, Any]],
            event: dict[str, Any],
        ) -> list[Alert]:
            token = _ACTIVE.set(self)
            try:
                return self._dispatch(camera_id, detections, event)
            finally:
                _ACTIVE.reset(token)

        def _dispatch(
            self,
            camera_id: str,
            detections: list[dict[str, Any]],
            event: dict[str, Any],
        ) -> list[Alert]:
            fired: list[Alert] = []
            now = self._event_time(camera_id, event)
            zones = self._zones_for(camera_id)
            if self._resolved:
                for detection in detections:
                    if not isinstance(detection, dict):
                        continue
                    fired.extend(
                        self._run_rules(detection, camera_id, event, zones, now))
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
            fired: list[Alert] = []
            for resolved in self._resolved:
                rule = resolved.rule
                ctx = DetectionEvent(
                    detection=detection, camera=camera_id, raw=event,
                    config=self.cfg, zones=zones, owner=owner,
                    severity=resolved.severity,
                )
                if not resolved.matches(ctx):
                    if rule.zone is not None and rule.zone not in zones:
                        self._warn_missing_zone(rule, camera_id)
                    continue
                # Only now does presence start accruing: dwell measures
                # time spent SATISFYING this rule, not time on camera.
                # Prune with the furthest-ahead camera's clock, never
                # with this camera's — a lagging camera must not keep
                # dead records alive across the whole deployment.
                resolved.state.gc(self._gc_clock, exclude=(key,))
                record = resolved.presence(key, now)
                ctx._bind(record, resolved.state)
                if not _gate(resolved, record, now):
                    continue
                try:
                    produced = rule.fn(ctx)
                except Exception:
                    logger.exception("%s: rule %s failed on %s",
                                     owner.id, rule.label, camera_id)
                    # Still mark the cooldown: a rule that raises on
                    # every event must not re-raise on every event.
                    _mark(resolved, record, now)
                    continue
                _mark(resolved, record, now)
                fired.extend(_merge_alerts(ctx._alerts, produced))
            return fired

        # ── The app's surfaces ─────────────────────────────────────

        def asyncapi_snapshot(self) -> dict[str, Any]:
            from .openapi import contract_asyncapi, prune

            if self.manifest is None:
                return {}
            return prune(contract_asyncapi(self.manifest,
                                           publishes=owner._publishes))

        def state_snapshot(self) -> dict[str, Any]:
            snapshot = dict(self.store)
            if owner._state_fn is not None:
                token = _ACTIVE.set(self)
                try:
                    extra = owner._state_fn()
                except Exception:
                    logger.exception("%s: @app.state failed", owner.id)
                    extra = None
                finally:
                    _ACTIVE.reset(token)
                if isinstance(extra, dict):
                    snapshot.update(extra)
            return snapshot

        def on_action(self, name: str, params: dict[str, Any]) -> Any:
            for action, fn in owner._actions:
                if action.name != name:
                    continue
                kwargs = {p.name: params.get(p.name, p.default)
                          for p in action.params}
                token = _ACTIVE.set(self)
                try:
                    return fn(**kwargs)
                finally:
                    _ACTIVE.reset(token)
            raise KeyError(name)

        def verify_license(self, license_key: str) -> Any:
            if owner._license_fn is None:
                return super().verify_license(license_key)
            token = _ACTIVE.set(self)
            try:
                verdict = owner._license_fn(license_key)
            finally:
                _ACTIVE.reset(token)
            if isinstance(verdict, bool):
                return Entitlement(valid=verdict, message="" if verdict
                                   else "This licence key was not accepted.")
            return verdict

    if owner._ui_fn is not None:
        def ui_html(self) -> str:
            token = _ACTIVE.set(self)
            try:
                return str(owner._ui_fn())
            finally:
                _ACTIVE.reset(token)

        FacadeDetector.ui_html = ui_html  # type: ignore[attr-defined]

    FacadeDetector.manifest = manifest
    FacadeDetector.__name__ = f"{_pascal(owner.id)}App"
    FacadeDetector.__qualname__ = FacadeDetector.__name__
    return FacadeDetector


# ── Helpers ─────────────────────────────────────────────────────────


def _gate(resolved: _Resolved, record: Any, now: float) -> bool:
    """Apply ``dwell`` and ``cooldown`` for one rule."""
    if resolved.dwell > 0.0:
        if record.age < resolved.dwell:
            return False
        if record.alerted:
            return False
    if resolved.cooldown > 0.0:
        last = record.data.get("_last")
        if last is not None and (now - float(last)) < resolved.cooldown:
            return False
    return True


def _mark(resolved: _Resolved, record: Any, now: float) -> None:
    if resolved.dwell > 0.0:
        record.alerted = True
    if resolved.cooldown > 0.0:
        record.data["_last"] = now


def _merge_alerts(fired: list[Alert], produced: Any) -> list[Alert]:
    """A handler may call ``event.alert()`` AND return the alert. Both
    are honoured, neither is dispatched twice."""
    out = list(fired)
    seen = {id(a) for a in out}
    for alert in _as_alerts(produced):
        if id(alert) not in seen:
            seen.add(id(alert))
            out.append(alert)
    return out


def _as_alerts(produced: Any) -> list[Alert]:
    """Normalize a handler's return value to a list of alerts."""
    if produced is None:
        return []
    if isinstance(produced, Alert):
        return [produced]
    if isinstance(produced, (list, tuple)):
        return [a for a in produced if isinstance(a, Alert)]
    return []


def _slug(name: str) -> str:
    """A handler name as a valid alert-type name (``[a-z0-9_-]+``)."""
    return _SLUG_RE.sub("-", str(name).lower()).strip("-")


def _pascal(app_id: str) -> str:
    return "".join(part.capitalize()
                   for part in app_id.replace("_", "-").split("-") if part)


__all__ = [
    "App", "DetectionEvent", "Setting", "setting",
    "DEFAULT_ABSENCE_S", "DEFAULT_MIN_CONFIDENCE",
]
