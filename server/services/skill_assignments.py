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
* **Live claims only**: the projection is the COMPUTE gate, and it
  carries skills, not claimants. A claim whose consumer buys nothing —
  an app switched off in the catalog, or one that is gone — is left out
  of it, so the inference behind it stops; the row itself stays, so
  switching the app back on restores the camera without re-picking.
  Nothing else rewrites the column, so the catalog routes and a boot
  backstop re-project (``reproject_app_cameras``,
  ``reconcile_projections``).
* **Additive narrowing**: a claim's ``params`` may carry
  ``{"labels": [...]}``. The projection merges labels as the union of
  every claim's set; any claim WITHOUT labels means "no restriction"
  and wins (a restriction other consumers didn't ask for must never
  hide their detections).
* **App widening**: an app pick (consumer ``app:<id>``) carries the
  app's manifest ``tier0_labels`` as its ``labels``, filled in here at
  declare time and refreshed on re-registration. Tier-0 reads labels
  on a NON-``object_detection`` entry as classes to ADD to the camera's
  set, never as a replacement — see detect-pipeline's
  ``_assignment_view`` and docs/tier0-consumption.md.

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


def inactive_consumers(db: Session) -> set[str]:
    """Consumers whose claims buy no compute: installed apps that are
    switched OFF in the catalog.

    A pick is stored under the PLATFORM skill it turns on
    (``app_pick_skill``: ANPR picks are ``license_plate_recognition``),
    which is the whole point — it is what makes plate OCR run. But that
    also means the projection cannot tell "the operator tuned this
    camera" from "an app asked for this", and the compute gates read the
    projection, not the table. So the catalog's switch stopped at the
    app's own door: the app went quiet while core kept reading plates
    for it, at about a core an hour, for nobody.

    Resolved here, where the consumer is still known — the entry never
    reaches the projection, so every reader (core's gates, Tier-0, the
    SDK, an app of someone else's making) sees the claim gone rather
    than having to learn a new "but is it live?" flag it might not
    know about. A reader that has not been upgraded fails CLOSED.

    An INSTALLED app only. A consumer this deployment knows nothing
    about — ``app:something-not-installed`` — is left alone, because the
    vocabulary is open by design (see this module's header): a consumer
    is a free string, annotated and never gated on. An uninstall
    releases that app's claims itself (:func:`release_app_picks`).
    """
    from models import InstalledApp

    return {
        app_consumer(row[0]) for row in
        db.query(InstalledApp.id).filter(InstalledApp.enabled.is_(False)).all()
    }


def project_camera(db: Session, camera: Camera,
                   inactive: set[str] | None = None) -> None:
    """Recompute ``camera.assignments`` from the table (no commit).

    Projection shape is exactly what the editor wrote historically:
    ``[{"skill": s} | {"skill": s, "labels": [...]}]`` — so every
    reader (Tier-0, the SDK, the internal endpoint) is untouched.

    ACTIVE claims only: a switched-off app's pick is left out, because
    this column is the compute gate (see :func:`inactive_consumers`).
    The row stays in the table, so enabling the app brings the camera
    back without the operator picking it again.
    """
    # Two small queries; a caller projecting many cameras resolves the
    # set once and passes it in rather than paying them per camera.
    if inactive is None:
        inactive = inactive_consumers(db)
    rows = [
        row for row in (
            db.query(SkillAssignment)
            .filter(SkillAssignment.camera_id == camera.id)
            .order_by(SkillAssignment.skill)
            .all()
        )
        if row.consumer not in inactive
    ]
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
    if consumer.startswith(APP_CONSUMER_PREFIX) and _labels_of(params) is None:
        params = _with_app_tier0_labels(db, consumer[len(APP_CONSUMER_PREFIX):], params)
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


def _manifest_tier0_labels(manifest: Any) -> list[str]:
    """The ``tier0_labels`` an app's manifest declares, cleaned the way
    the projection cleans labels (lowercased, deduplicated, sorted) so a
    pick row carries exactly what Tier-0 will compare against."""
    if not isinstance(manifest, dict):
        return []
    labels = manifest.get("tier0_labels")
    if not isinstance(labels, list):
        return []
    return sorted({str(s).strip().lower() for s in labels if str(s).strip()})


def _with_app_tier0_labels(
    db: Session, app_id: str, params: Optional[dict],
) -> Optional[dict]:
    """A pick's params with the app's ``tier0_labels`` filled in as
    ``labels`` — only when the caller passed none, so a caller that named
    labels itself keeps them.

    Tier-0 tracks only the deployment's global label set, so an app that
    rides it for other classes (a bag, a parcel) sees nothing on a stock
    install. Its manifest says which classes it needs; the pick is where
    the platform learns WHICH cameras, so this is where the two meet.
    Tier-0 reads the labels off the pick's projection entry (skill = the
    app id) and widens that camera's set with them. An unknown app id or
    a manifest without the field leaves the params untouched.
    """
    from models import InstalledApp

    app = db.query(InstalledApp).filter(InstalledApp.id == app_id).first()
    labels = _manifest_tier0_labels(app.manifest_json) if app is not None else []
    if not labels:
        return params
    out = dict(params) if isinstance(params, dict) else {}
    out["labels"] = labels
    return out


def sync_app_pick_labels(db: Session, app_id: str) -> int:
    """Refresh ``labels`` on every pick the app holds from its CURRENT
    manifest and re-project those cameras (no commit). Returns how many
    pick rows changed.

    Registration calls this so a manifest that GAINS ``tier0_labels``
    widens the cameras picked before the upgrade — otherwise an operator
    would have to unpick and re-pick every camera to see the new classes,
    with nothing telling them so. The manifest is the source of truth for
    a pick's labels both ways: a manifest that drops the field takes the
    labels off the picks again.
    """
    from models import InstalledApp

    app = db.query(InstalledApp).filter(InstalledApp.id == app_id).first()
    labels = _manifest_tier0_labels(app.manifest_json) if app is not None else []
    rows = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.consumer == app_consumer(app_id))
        .all()
    )
    changed = 0
    cameras: set[int] = set()
    for row in rows:
        params = dict(row.params) if isinstance(row.params, dict) else {}
        if labels:
            if _labels_of(params) == labels:
                continue
            params["labels"] = labels
        else:
            if "labels" not in params:
                continue
            params.pop("labels")
        row.params = params or None
        changed += 1
        cameras.add(row.camera_id)
    if not changed:
        return 0
    db.flush()
    inactive = inactive_consumers(db)
    for camera in db.query(Camera).filter(Camera.id.in_(cameras)).all():
        project_camera(db, camera, inactive)
    logger.info(
        "refreshed tier0 labels on %d camera pick(s) for app %s: %s",
        changed, app_id, ", ".join(labels) or "-",
    )
    return changed


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
    named_apps = operator_rows_naming_apps(db, entries)
    if named_apps:
        raise ValueError(
            "An app can't be assigned here — select cameras for "
            + ", ".join(named_apps)
            + " in that app's own configuration. Assignments only tune "
            "platform detection (e.g. object_detection narrowed to labels, "
            "or license_plate_recognition)."
        )
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
    per-consumer claims visible so a release is never a surprise.

    Live cameras only (#372) — the operator view must show the same
    union the consumers act on, or a binned camera's stale claim looks
    like a working assignment."""
    rows = (
        db.query(SkillAssignment)
        .join(Camera, Camera.id == SkillAssignment.camera_id)
        .filter(SkillAssignment.skill == (skill or "").strip(),
                Camera.deleted_at.is_(None))
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


def release_camera_claims(db: Session, camera_id: int) -> int:
    """Camera deletion cleanup (#372): drop every skill claim on the
    camera, returning how many rows went. Called by BOTH delete paths —
    soft delete (the bin) and hard delete (the purge; also prevents the
    FK from failing the camera row delete). No commit here: the caller
    owns the transaction, so the cleanup lands atomically with the
    tombstone/purge it belongs to.

    The query-side ``deleted_at`` filter above already makes stale rows
    inert, so this is hygiene + belt: rows from installs deleted before
    this fix are handled by the filter (and swept by migration
    ff77bb88cc99) even if this cleanup never ran for them."""
    removed = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.camera_id == camera_id)
        .delete(synchronize_session=False)
    )
    if removed:
        logger.info(
            "released %d skill claim(s) for deleted camera %s",
            removed, camera_id,
        )
    return removed


def assignments_by_skill(db: Session) -> dict[str, list[int]]:
    """skill -> sorted union of camera ids. The registry's Phase 2
    source: an empty list never appears (no rows = no key), so
    ``skill not in map`` IS 'dormant' for the status derivation.

    Only LIVE cameras count (issue #372). Camera deletion is a soft
    delete, and a stale assignment row pointing at a binned camera used
    to keep the restriction ARMED while scoping consumers to a camera
    that no longer exists — the LPR app would ignore every live camera
    because one deleted one still 'claimed' the skill. The join also
    drops orphan rows whose camera was hard-deleted. When the last live
    assignment goes, the key disappears and the restriction correctly
    lifts (CAMERA_ASSIGNMENTS.md: 'the assignment list for that skill
    is the whole truth' — the truth must not include tombstones)."""
    out: dict[str, set[int]] = {}
    rows = (
        db.query(SkillAssignment)
        .join(Camera, Camera.id == SkillAssignment.camera_id)
        .filter(Camera.deleted_at.is_(None))
        .all()
    )
    for row in rows:
        out.setdefault(row.skill, set()).add(row.camera_id)
    return {k: sorted(v) for k, v in out.items()}


# ── The eligibility rule, in one place ──────────────────────────────
#
# Two different questions get asked about a camera and a skill, and
# conflating them is what made assignments advisory:
#
#   ELIGIBLE — may this skill be offered this camera? Open by default:
#     a camera nobody has claimed can be picked by anyone. This is what
#     a configuration picker asks.
#   ADOPTED  — is this camera actually claimed by that skill? This is
#     what COMPUTE asks, and it is never true by default. An unassigned
#     camera is eligible everywhere and computes nowhere.
#
# Both read the denormalised ``Camera.assignments`` projection rather
# than the claim table, because the hot path (plate ingest) already has
# the camera row in hand and must not pay a second query per visit.


def camera_skills(camera) -> set[str]:
    """Skills claimed on a camera, from the JSON projection.

    The projection is NULL when nothing is claimed and a list of
    ``{"skill": ..., "labels"?: [...]}`` otherwise (see
    :func:`project_camera`). Malformed entries are ignored rather than
    raised on: this is read on the ingest path, and a bad row in the
    column must not cost a visit.
    """
    entries = getattr(camera, "assignments", None)
    if not isinstance(entries, list):
        return set()
    out: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            skill = entry.get("skill")
            if isinstance(skill, str) and skill.strip():
                out.add(skill.strip().lower())
    return out


def camera_adopted(camera, skill: str) -> bool:
    """Does this camera actually carry that skill? The COMPUTE gate.

    False for an unassigned camera — that is the point. Inference for a
    skill runs on the cameras an operator pointed it at, not on every
    camera that happens to exist.
    """
    return skill.strip().lower() in camera_skills(camera)


def camera_eligible(camera, skill: str) -> bool:
    """May this skill be OFFERED this camera? The picker gate.

    Always yes. Every camera is available to every app, and any number
    of apps may pick the same one — one inference stream feeding many
    apps is what the platform is built for.

    It used to answer "only if nothing else claimed it", which made
    every claim exclusive: a camera narrowed to ``object_detection``
    labels vanished from every app's picker, and one app's pick would
    have hidden the camera from all the others. Kept as a function so
    the rule stays named in one place rather than inlined as ``True``.
    """
    return True


# ── App picks ───────────────────────────────────────────────────────
#
# An app uses a camera because the app was POINTED at it, in the app's
# own configuration — not because the camera page named it. A pick is an
# ordinary claim with ``consumer="app:<app id>"`` and ``skill`` = the app
# id with underscores, written through the same declare/release as every
# other claim (the Vehicles page has always written ANPR's picks this
# way). So:
#
#   * an app's roster is exactly its picks on live cameras — nothing
#     picked, nothing read and nothing computed;
#   * several apps may pick one camera (no exclusivity);
#   * compute is unchanged: picks project into ``Camera.assignments``
#     like any claim, so ANPR's picks (skill ``license_plate_recognition``)
#     still switch plate OCR on through the existing gates.

APP_CONSUMER_PREFIX = "app:"


def app_consumer(app_id: str) -> str:
    """The consumer a pick is stored under: ``app:<app id>``."""
    return f"{APP_CONSUMER_PREFIX}{str(app_id).strip()}"


def app_pick_skill(app_id: str) -> str:
    """The skill a pick is stored under: the app id with underscores.

    For ANPR that is ``license_plate_recognition`` — deliberately the
    platform plate skill, so its picks keep turning plate OCR on."""
    return str(app_id).strip().replace("-", "_")


def picked_camera_ids(db: Session, app_id: str) -> set[int]:
    """Live cameras this app picked. Empty = the app uses nothing."""
    rows = (
        db.query(SkillAssignment.camera_id)
        .join(Camera, Camera.id == SkillAssignment.camera_id)
        .filter(SkillAssignment.consumer == app_consumer(app_id),
                Camera.deleted_at.is_(None))
        .all()
    )
    return {int(r[0]) for r in rows}


def apps_using_camera(db: Session, camera_id: int) -> list[str]:
    """App ids that picked this camera, for the camera page's "Used by"."""
    rows = (
        db.query(SkillAssignment.consumer)
        .filter(SkillAssignment.camera_id == camera_id,
                SkillAssignment.consumer.like(f"{APP_CONSUMER_PREFIX}%"))
        .all()
    )
    return sorted({r[0][len(APP_CONSUMER_PREFIX):] for r in rows})


def reconcile_projections(db: Session, *, commit: bool = True) -> int:
    """Boot backstop: recompute the projection of every camera holding a
    claim that buys no compute. Returns how many cameras were rewritten.

    Nothing rewrites the column on its own. A deployment upgrading into
    the rule that a switched-off app's pick is not projected would keep
    the old projection — and go on paying for plate OCR nobody reads —
    until somebody happened to edit a claim on that camera. Databases
    bootstrapped by ``create_all`` skip migrations entirely, which is why
    this is a boot backstop rather than one.

    Idempotent and cheap: two small queries, and ``project_camera``
    writes only when the value actually changes.
    """
    inactive = inactive_consumers(db)
    if not inactive:
        return 0
    camera_ids = {
        int(row[0]) for row in
        db.query(SkillAssignment.camera_id)
        .filter(SkillAssignment.consumer.in_(inactive))
        .distinct().all()
    }
    if not camera_ids:
        return 0
    changed = 0
    for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all():
        before = camera.assignments
        project_camera(db, camera, inactive)
        if camera.assignments != before:
            changed += 1
    if changed and commit:
        db.commit()
    if changed:
        logger.info("reconciled %d camera projection(s) holding claims of "
                    "switched-off apps", changed)
    return changed


def reproject_app_cameras(db: Session, app_id: str) -> int:
    """Recompute the projection of every camera this app picked (no commit).

    Switching an app on or off edits no claim row, so nothing would
    otherwise recompute the column the compute gates read — the app
    would go quiet while core carried on reading plates for it. Called
    from both catalog routes; returns how many cameras were touched.
    """
    camera_ids = picked_camera_ids(db, app_id)
    if not camera_ids:
        return 0
    inactive = inactive_consumers(db)
    for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all():
        project_camera(db, camera, inactive)
    return len(camera_ids)


def claimed_skills_by_camera(db: Session, camera_ids: set[int]) -> dict[int, list[str]]:
    """Every skill CLAIMED on each camera, live or not — one query.

    The projection carries what is live; this says whether anything
    asked at all. Tier-0's opt-in ``DETECT_SKIP_UNASSIGNED`` needs the
    difference: an empty projection means "no restriction declared" to
    it (so: analyze with the global labels), and without this a camera
    whose only claim came from an app that was then switched off would
    go from "skipped, nobody wants it" to "analyzed by default" — the
    switch would cost MORE compute than leaving the app on.
    """
    if not camera_ids:
        return {}
    out: dict[int, set[str]] = {}
    rows = (
        db.query(SkillAssignment.camera_id, SkillAssignment.skill)
        .filter(SkillAssignment.camera_id.in_(camera_ids))
        .all()
    )
    for camera_id, skill in rows:
        out.setdefault(int(camera_id), set()).add(str(skill))
    return {cid: sorted(skills) for cid, skills in out.items()}


def operator_assignments(db: Session, camera_id: int) -> list[dict[str, Any]]:
    """The OPERATOR's own claims on this camera, in editor shape.

    What the camera-settings form must prefill from. It used to prefill
    from the projection, which also carries app picks — so opening a
    camera an app had picked and pressing Save copied that app's skill
    into an operator claim (``license_plate_recognition`` is a platform
    task, so it is accepted there by design). The app's switch then
    stopped working on that camera for ever, with nothing on screen to
    say why.
    """
    rows = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.camera_id == camera_id,
                SkillAssignment.consumer == OPERATOR_CONSUMER)
        .order_by(SkillAssignment.skill)
        .all()
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {"skill": row.skill}
        labels = _labels_of(row.params)
        if labels:
            entry["labels"] = labels
        out.append(entry)
    return out


def release_app_picks(db: Session, app_id: str) -> int:
    """Drop every pick an app holds and re-project those cameras (no
    commit). Uninstall calls this: nothing should keep running for an
    app that is gone."""
    rows = (
        db.query(SkillAssignment)
        .filter(SkillAssignment.consumer == app_consumer(app_id))
        .all()
    )
    camera_ids = {row.camera_id for row in rows}
    for row in rows:
        db.delete(row)
    db.flush()
    if camera_ids:
        inactive = inactive_consumers(db)
        for camera in db.query(Camera).filter(Camera.id.in_(camera_ids)).all():
            project_camera(db, camera, inactive)
    if rows:
        logger.info("released %d camera pick(s) for app %s", len(rows), app_id)
    return len(rows)


_PLATFORM_TASKS: Optional[frozenset[str]] = None


def platform_task_names() -> frozenset[str]:
    """Every task name and alias in ``config/tasks.yml`` — the skills an
    operator row on the camera page may legitimately carry, even when an
    installed app happens to share the name (ANPR's id spelling is
    ``license_plate_recognition``, which is also the plate OCR task)."""
    global _PLATFORM_TASKS
    if _PLATFORM_TASKS is None:
        from pathlib import Path

        import yaml

        names: set[str] = set()
        try:
            path = Path(__file__).resolve().parents[1] / "config" / "tasks.yml"
            for entry in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
                if isinstance(entry, dict) and entry.get("task"):
                    names.add(str(entry["task"]).strip().lower())
                    names.update(
                        str(a).strip().lower() for a in entry.get("aliases") or [])
        except Exception:  # noqa: BLE001
            logger.warning("tasks.yml unreadable; no platform task names known",
                           exc_info=True)
        _PLATFORM_TASKS = frozenset(names)
    return _PLATFORM_TASKS


def operator_rows_naming_apps(db: Session, entries: list[dict]) -> list[str]:
    """Installed apps that an operator assignment list names — which is
    no longer allowed. Returns the apps' display names, sorted.

    The camera page used to be how an app was pointed at a camera, which
    is what made "which cameras does this app use" impossible to answer
    from the app itself. A name that is also a platform task
    (``license_plate_recognition``) is accepted: there it tunes compute.
    """
    wanted = {
        str(e.get("skill") or "").strip().lower()
        for e in entries or [] if isinstance(e, dict)
    } - platform_task_names() - {""}
    if not wanted:
        return []
    from models import InstalledApp
    from services.app_keys import app_skills

    named: set[str] = set()
    for row in db.query(InstalledApp).all():
        tokens = {str(t).strip().lower() for t in app_skills(row)}
        if wanted & tokens:
            named.add(str(row.name or row.id))
    return sorted(named)
