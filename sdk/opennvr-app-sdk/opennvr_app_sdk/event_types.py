# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Typed payloads for the contracted domain events.

``docs/EVENT_CONTRACTS.md`` names every event the platform and its apps
put on the bus — ``plate.recognized.v1``, ``access.decided.v1``, the
occupancy trio, … — and their required payload fields. Until now an
app read them as ``event.payload["plate_text"]`` and wrote them as a
hand-built dict. These classes are the same contracts as Python:

    from opennvr_app_sdk import PlateRecognized, AccessDecided

    class Gate(DomainEventSubscriber):
        subscriptions = [PlateRecognized.SCHEMA]

        def on_event(self, event):
            plate = event.typed()             # PlateRecognized, or None if off-contract
            if plate and plate.confidence and plate.confidence > 0.8:
                self.publisher.publish_typed(
                    AccessDecided(plate_text=plate.plate_text, decision="allow",
                                  reason="registered"),
                    camera_id=event.camera_id)

Rules, all from the contract:

* **Additive-only.** Fields a producer adds later ride in ``extra`` and
  come back out of ``to_payload()`` untouched; a consumer on an older
  SDK never loses them, a consumer on a newer SDK never breaks on
  their absence.
* **Required is required.** A payload missing a required field does
  not parse (``ValueError``); ``DomainEvent.typed()`` turns that into
  ``None`` and a log line, so a long-lived subscriber never dies to
  one bad message.
* **Unknown values are tolerated where the contract says so**
  (``level``, ``reason``, ``decision`` are strings, not enums — a
  gate must fail closed on a decision it does not recognise, and it
  can only do that if parsing did not already reject it).
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field, fields
from typing import Any, ClassVar, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T", bound="TypedPayload")


@dataclass(frozen=True)
class TypedPayload:
    """Base of every typed payload: ``SCHEMA``, ``from_payload``,
    ``to_payload``. Subclasses list the contract's fields; ``extra``
    keeps whatever else the producer sent."""

    SCHEMA: ClassVar[str] = ""
    #: Field names a payload MUST carry (the contract's non-optional rows).
    REQUIRED: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def from_payload(cls: type[T], payload: dict[str, Any]) -> T:
        if not isinstance(payload, dict):
            raise ValueError(f"{cls.SCHEMA}: payload must be an object")
        names = {f.name for f in fields(cls)} - {"extra"}
        missing = [name for name in cls.REQUIRED if payload.get(name) is None]
        if missing:
            raise ValueError(f"{cls.SCHEMA}: missing required field(s) {missing}")
        known = {k: v for k, v in payload.items() if k in names}
        extra = {k: v for k, v in payload.items() if k not in names}
        try:
            return cls(**known, extra=extra)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ValueError(f"{cls.SCHEMA}: {exc}") from exc

    def to_payload(self) -> dict[str, Any]:
        """The wire payload: every contract field (nullable ones as
        ``null``), then ``extra`` — never overriding a contract field."""
        out: dict[str, Any] = {}
        for f in fields(self):
            if f.name == "extra":
                continue
            value = getattr(self, f.name)
            out[f.name] = list(value) if isinstance(value, tuple) else value
        for k, v in self.extra.items():
            out.setdefault(k, v)
        return out

    # Keyword-only so ``PlateRecognized("R197GB")`` fills plate_text, not extra.
    extra: dict[str, Any] = field(default_factory=dict, compare=False, kw_only=True)


# ── the v1 contracts ────────────────────────────────────────────────


@dataclass(frozen=True)
class DetectionObserved(TypedPayload):
    """``detection.observed.v1`` — one Tier-0 frame result with tracks."""

    SCHEMA: ClassVar[str] = "detection.observed.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("frame", "tracks")
    frame: dict[str, Any] = field(default_factory=dict)      # {"w", "h"}
    tracks: list[dict[str, Any]] = field(default_factory=list)
    calibrating: bool = False


@dataclass(frozen=True)
class VisitRecorded(TypedPayload):
    """``visit.recorded.v1`` — a finished timeline visit persisted by core."""

    SCHEMA: ClassVar[str] = "visit.recorded.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("event_id", "label", "started_at", "ended_at")
    event_id: int = 0
    label: str = ""
    started_at: str = ""
    ended_at: str = ""
    evidence_path: str | None = None


@dataclass(frozen=True)
class PlateRecognized(TypedPayload):
    """``plate.recognized.v1`` — one accepted OCR read."""

    SCHEMA: ClassVar[str] = "plate.recognized.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("plate_text",)
    plate_text: str = ""
    confidence: float | None = None
    vehicle_label: str | None = None
    event_id: int | None = None
    plate_box: list[float] | None = None
    plate_box_confidence: float | None = None
    plate_box_image: list[int] | None = None

    def to_payload(self) -> dict[str, Any]:
        out = super().to_payload()
        # The optional geometry fields are "absent", not null, when unknown.
        for name in ("plate_box", "plate_box_confidence", "plate_box_image"):
            if out.get(name) is None:
                out.pop(name, None)
        return out


@dataclass(frozen=True)
class AccessDecided(TypedPayload):
    """``access.decided.v1`` — an admission decision for a plate at a gate.
    Consumers actuate only on ``decision == "allow"``; anything else,
    including values this SDK does not know, is "do not actuate"."""

    SCHEMA: ClassVar[str] = "access.decided.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("plate_text", "decision", "reason")
    plate_text: str = ""
    decision: str = "deny"
    reason: str = "unknown"
    owner: str | None = None
    unit: str | None = None
    confidence: float | None = None

    @property
    def allow(self) -> bool:
        return self.decision == "allow"


@dataclass(frozen=True)
class OccupancyChanged(TypedPayload):
    """``occupancy.changed.v1`` — a zone's head-count moved."""

    SCHEMA: ClassVar[str] = "occupancy.changed.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("count", "level")
    count: int = 0
    level: str = "normal"
    max_occupancy: int | None = None
    min_occupancy: int | None = None


@dataclass(frozen=True)
class OccupancyHeatmap(TypedPayload):
    """``occupancy.heatmap.v1`` — a sparse delta of a per-camera heat grid."""

    SCHEMA: ClassVar[str] = "occupancy.heatmap.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("cols", "rows", "cells", "frames", "period_seconds")
    cols: int = 0
    rows: int = 0
    cells: list[list[int]] = field(default_factory=list)     # [[index, hits], …]
    frames: int = 0
    period_seconds: int = 60
    labels: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OccupancyFootfall(TypedPayload):
    """``occupancy.footfall.v1`` — entries, exits and finished stays since the last publish."""

    SCHEMA: ClassVar[str] = "occupancy.footfall.v1"
    REQUIRED: ClassVar[tuple[str, ...]] = ("entries", "exits", "dwell_count", "dwell_seconds",
                                           "dwell_max_seconds", "period_seconds")
    entries: int = 0
    exits: int = 0
    dwell_count: int = 0
    dwell_seconds: float = 0.0
    dwell_max_seconds: float = 0.0
    period_seconds: int = 60
    labels: list[str] = field(default_factory=list)


#: schema → typed payload class, for every contract this SDK knows.
EVENT_TYPES: dict[str, type[TypedPayload]] = {
    cls.SCHEMA: cls for cls in (
        DetectionObserved, VisitRecorded, PlateRecognized, AccessDecided,
        OccupancyChanged, OccupancyHeatmap, OccupancyFootfall,
    )
}


def typed_payload(schema: str, payload: dict[str, Any]) -> TypedPayload | None:
    """The typed payload for ``schema``, ``None`` when the schema is not
    one this SDK types (a newer contract, or an app's own) or the
    payload is off-contract (logged)."""
    cls = EVENT_TYPES.get(schema)
    if cls is None:
        return None
    try:
        return cls.from_payload(payload)
    except ValueError as exc:
        logger.warning("domain event %s: payload off-contract: %s", schema, exc)
        return None


def is_typed_payload(obj: Any) -> bool:
    return dataclasses.is_dataclass(obj) and isinstance(obj, TypedPayload) and bool(type(obj).SCHEMA)
