# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Per-camera detection settings: overrides of the pipeline's global knobs.

The detect-pipeline's ``DETECT_*`` env knobs are one value for every
camera. A road camera and an indoor camera want different analysis rates,
motion thresholds and object sizes, so each camera may carry its own
overrides here (``Camera.detection_settings``). Every field is optional
and absent means "inherit the global value", so a camera with nothing set
behaves exactly as before.

Object filters are per label (Frigate's ``objects.filters``): a detection
is dropped when its box is smaller than ``min_area`` or larger than
``max_area`` (fractions of the frame area, so the same numbers hold on the
substream and the main stream), its width/height ratio is outside
``min_ratio``..``max_ratio``, or its score is below ``min_score``.

The pipeline reads the record as ``detection`` in the camera-agent sources
listing and restarts the camera's worker on its next reconcile tick when
it changes.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_LABEL_RE = re.compile(r"^[a-z0-9][a-z0-9 _.-]{0,59}$")
MAX_OBJECT_FILTERS = 40


class ObjectFilterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_area: float | None = Field(None, ge=0.0, le=1.0)
    max_area: float | None = Field(None, ge=0.0, le=1.0)
    min_ratio: float | None = Field(None, ge=0.01, le=100.0)
    max_ratio: float | None = Field(None, ge=0.01, le=100.0)
    min_score: float | None = Field(None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> ObjectFilterIn:
        if self.min_area is not None and self.max_area is not None \
                and self.min_area > self.max_area:
            raise ValueError("min_area is larger than max_area")
        if self.min_ratio is not None and self.max_ratio is not None \
                and self.min_ratio > self.max_ratio:
            raise ValueError("min_ratio is larger than max_ratio")
        return self

    @property
    def empty(self) -> bool:
        return all(v is None for v in self.model_dump().values())


class DetectionSettingsIn(BaseModel):
    """What the UI sends; ``None`` everywhere = inherit."""

    model_config = ConfigDict(extra="forbid")

    fps: int | None = Field(None, ge=1, le=30)
    motion_enabled: bool | None = None
    motion_threshold: int | None = Field(None, ge=1, le=255)
    motion_contour_area: int | None = Field(None, ge=1, le=100_000)
    min_spawn_score: float | None = Field(None, ge=0.0, le=1.0)
    stationary_interval: int | None = Field(None, ge=0, le=600)
    # Which stream Tier-0 detects on for this camera. ``auto`` (the
    # default) takes the sub stream when it is tall enough for the
    # detector and the main stream otherwise; ``sub`` pins the sub even
    # when small; ``main`` detects on the full-resolution stream (costlier,
    # full-resolution evidence from the same frame) — the per-camera
    # switch that lets the two be compared on the same scene.
    detect_stream: Literal["auto", "sub", "main"] | None = None
    object_filters: dict[str, ObjectFilterIn] | None = None

    @field_validator("object_filters")
    @classmethod
    def _filters(cls, v: dict[str, ObjectFilterIn] | None) -> dict[str, ObjectFilterIn] | None:
        if v is None:
            return None
        out: dict[str, ObjectFilterIn] = {}
        for label, f in v.items():
            lbl = label.strip().lower()
            if not _LABEL_RE.fullmatch(lbl):
                raise ValueError(f"bad label {label!r}")
            if lbl in out:
                raise ValueError(f"label {lbl!r} listed twice")
            if not f.empty:
                out[lbl] = f
        if len(out) > MAX_OBJECT_FILTERS:
            raise ValueError(f"at most {MAX_OBJECT_FILTERS} object filters")
        return out or None

    def stored(self) -> dict[str, Any] | None:
        """The compact form kept on the camera: only the fields that are
        set, ``None`` when nothing is."""
        data = {k: v for k, v in self.model_dump(exclude={"object_filters"}).items()
                if v is not None}
        if self.object_filters:
            data["object_filters"] = {
                lbl: {k: v for k, v in f.model_dump().items() if v is not None}
                for lbl, f in self.object_filters.items()
            }
        return data or None


def normalize(raw: Any) -> dict[str, Any]:
    """A stored record (anything, including junk from an older write) as
    the compact dict the pipeline and the UI read. Never raises: a record
    that fails validation reads as "no overrides"."""
    if not isinstance(raw, dict) or not raw:
        return {}
    try:
        return DetectionSettingsIn.model_validate(raw).stored() or {}
    except Exception:
        return {}


def serialize(camera) -> dict[str, Any]:
    return {"camera_id": camera.id, "detection": normalize(camera.detection_settings)}
