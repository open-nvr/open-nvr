# Copyright (c) 2026 OpenNVR
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""RFC-0002 Phase 2: the assignment table's union semantics + projection.

One table (``SkillAssignment``: skill × camera × consumer, decision 8),
three rules, all here so no caller can get them wrong:

* **Union**: a skill runs on the union of its consumers' cameras.
  Releasing one consumer's claim shrinks the union; releasing the last
  makes the skill dormant on that camera (and dormant overall when no
  camera remains — gap 7 closes in the registry's status derivation).
* **Projection**: ``Camera.assignments`` — the JSON column Tier-0
  reconcile, the SDK's ``cameras_for_skill`` and the internal
  camera-agent endpoint already read — is recomputed from the table on
  every write. Existing consumers keep working without a line changed.
* **Additive narrowing**: a claim's ``params`` may carry
  ``{"labels": [...]}``. The projection merges labels as the union of
  every claim's set; any claim WITHOUT labels means "no restriction"
  and wins (a restriction other consumers didn't ask for must never
  hide their detections).

Vocabulary stays open (annotate, never gate): ``skill`` and
``consumer`` are free strings; validation is shape and bounds only.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.orm import Session

from models import Camera, SkillAssignment

logger = logging.getLogger(__name__)

#: The camera-settings editor's identity. Its PUT keeps full-replace
#: semantics, but only over rows carrying this consumer — other
#: consumers' claims survive an operator edit (union semantics).
OPERATOR_CONSUMER = "operator"

MAX_SKILL_LEN = 100
MAX_CONSUMER_LEN = 100


def _clean(value: str, what: str, limit: int) -> str:
    v = (value or "").strip()
    if not v or len(v) > limit:
        raise ValueError(f"{what} must be 1..{limit} characters")
    return v


def _labels_of(params: Optional[dict]) -> Optional[list[str]]:
    if not isinstance(params, dict):
        return None
    labels = params.get("labels")
    if not isinstance(labels, list):
        return None
    cleaned = sorted({str(s).strip().lower() for s in labels if str(s).strip()})
    return cleaned or None


def project_camera(db: Session, camera: Camera) -> None:
    """Recompute ``camera.assignments`` from the table (no commit).

    Projection shape is exactly what the editor wrote historically:
    ``[{"skill": s} | {"skill": s, "labels": [...]}]`` — so every
    reader (Tier-0, SDK, internal endpoint, the editor's own prefill)
    is untouched.
    """
    rows = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.camera_id == camera.id)
        .order_by(SkillAssignment.skill)
        .all()
    )
    merged: dict[str, Optional[set[str]]] = {}
    for row in rows:
        labels = _labels_of(row.params)
        if row.skill not in merged:
            merged[row.skill] = set(labels) if labels is not None else None
        else:
            current = merged[row.skill]
            # None = unrestricted; unrestricted wins over any label set.
            if current is not None:
                merged[row.skill] = (
                    None if labels is None else current | set(labels)
                )
    projection: list[dict[str, Any]] = []
    for skill in sorted(merged):
        entry: dict[str, Any] = {"skill": skill}
        if merged[skill] is not None:
            entry["labels"] = sorted(merged[skill])
        projection.append(entry)
    new_value = projection or None
    if (camera.assignments or None) != new_value:
        camera.assignments = new_value


def declare(
    db: Session, *, skill: str, camera_id: int, consumer: str,
    params: Optional[dict] = None,
) -> SkillAssignment:
    """Upsert one claim and refresh the camera's projection (no commit)."""
    skill = _clean(skill, "skill", MAX_SKILL_LEN)
    consumer = _clean(consumer, "consumer", MAX_CONSUMER_LEN)
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    if camera is None:
        raise LookupError(f"camera {camera_id} not found")
    row = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.skill == skill,
                SkillAssignment.camera_id == camera_id,
                SkillAssignment.consumer == consumer)
        .first()
    )
    if row is None:
        row = SkillAssignment(
            skill=skill, camera_id=camera_id, consumer=consumer,
            params=params or None,
        )
        db.add(row)
    else:
        row.params = params or None
    db.flush()
    project_camera(db, camera)
    return row


def release(
    db: Session, *, skill: str, camera_id: int, consumer: str,
) -> bool:
    """Delete one claim and refresh the projection (no commit).

    Returns False when no such claim existed. The union shrinks by
    exactly this consumer's contribution — other claims stay.
    """
    row = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.skill == (skill or "").strip(),
                SkillAssignment.camera_id == camera_id,
                SkillAssignment.consumer == (consumer or "").strip())
        .first()
    )
    if row is None:
        return False
    camera = db.query(Camera).filter(Camera.id == camera_id).first()
    db.delete(row)
    db.flush()
    if camera is not None:
        project_camera(db, camera)
    return True


def set_operator_assignments(
    db: Session, camera: Camera, entries: list[dict],
) -> None:
    """The camera-settings editor's write path (no commit).

    Full-replace — but only of the OPERATOR's claims on this camera,
    which preserves the editor's documented contract ("send the FULL
    list each time") while other consumers' claims survive (decision
    8). Entries are the validated CameraAssignment dicts
    (``{"skill", "labels"?}``).
    """
    db.query(SkillAssignment).filter(
        SkillAssignment.camera_id == camera.id,
        SkillAssignment.consumer == OPERATOR_CONSUMER,
    ).delete(synchronize_session=False)
    db.flush()
    for entry in entries or []:
        skill = str(entry.get("skill") or "").strip()
        if not skill:
            continue
        labels = entry.get("labels")
        params = (
            {"labels": list(labels)}
            if isinstance(labels, list) and labels else None
        )
        db.add(SkillAssignment(
            skill=skill, camera_id=camera.id,
            consumer=OPERATOR_CONSUMER, params=params,
        ))
    db.flush()
    project_camera(db, camera)


def skill_view(db: Session, skill: str) -> dict[str, Any]:
    """``GET /api/v1/skills/{id}/cameras``: the skill's union, with the
    per-consumer claims visible so a release is never a surprise."""
    rows = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.skill == (skill or "").strip())
        .order_by(SkillAssignment.camera_id, SkillAssignment.consumer)
        .all()
    )
    cameras: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        cameras.setdefault(row.camera_id, []).append({
            "consumer": row.consumer,
            "params": row.params,
        })
    return {
        "skill": (skill or "").strip(),
        "cameras": [
            {"camera_id": cid, "consumers": claims}
            for cid, claims in sorted(cameras.items())
        ],
        "union": sorted(cameras),
    }


def assignments_by_skill(db: Session) -> dict[str, list[int]]:
    """skill -> sorted union of camera ids. The registry's Phase 2
    source: an empty list never appears (no rows = no key), so
    ``skill not in map`` IS 'dormant' for the status derivation."""
    out: dict[str, set[int]] = {}
    for row in db.query(SkillAssignment).all():
        out.setdefault(row.skill, set()).add(row.camera_id)
    return {k: sorted(v) for k, v in out.items()}
