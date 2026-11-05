# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Attendance's face gallery: enrol, check for duplicates, forget.

The faces live in the attendance adapter (``insightface_attendance``), not
in core: core keeps the person, the consent and a sample count, and the
adapter keeps the embeddings under the person's ``uuid``. KAI-C proxies
``/infer`` but not ``/faces/*``, so core calls the adapter directly, with
``internal_api_key`` as the bearer the adapter expects.

Never call these while holding a pooled DB connection: an enrolment photo
costs the adapter about half a second of CPU, more under load.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid as uuidlib
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: A photo that matches another enrolled person this closely is refused as
#: a duplicate: it is either the same person enrolled twice (two rows that
#: would share every sighting) or the wrong photo under this name.
DUPLICATE_SIMILARITY = 0.6
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

#: A person's picture: a small face crop the enrolling page cuts from an
#: accepted photo. Kept beside the recordings, but outside ``.evidence``
#: and the camera folders, which the retention sweep empties; deleted with
#: the person's faces (``drop_photo``), never by age.
_PHOTO_DIR = Path(".attendance") / "people"
MAX_PHOTO_BYTES = 256 * 1024
_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")


class FaceGalleryError(Exception):
    """The adapter refused, or could not be reached. ``status`` is the HTTP
    status the API should answer with; ``message`` says why, for a person."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _base() -> tuple[str, dict[str, str]]:
    from core.config import settings

    url = (settings.attendance_face_adapter_url or "").rstrip("/")
    headers = {"Authorization": f"Bearer {settings.internal_api_key}"}
    return url, headers


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason_phrase
    if isinstance(body, dict):
        for key in ("detail", "message"):
            if isinstance(body.get(key), str):
                return body[key]
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return err["message"]
    return str(body)[:200]


async def _call(method: str, path: str, **kw) -> httpx.Response:
    url, headers = _base()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            return await client.request(method, f"{url}{path}", headers=headers, **kw)
    except httpx.HTTPError as exc:
        logger.warning("attendance faces: adapter unreachable at %s (%s)", url, exc)
        raise FaceGalleryError(
            503, "The attendance face service is not reachable. Is the attendance "
                 "app installed and its face adapter running?") from None


def _raise_for(resp: httpx.Response) -> None:
    if resp.status_code < 400:
        return
    detail = _detail(resp)
    if resp.status_code in (400, 413, 422):
        raise FaceGalleryError(422, detail)
    if resp.status_code == 503:
        raise FaceGalleryError(503, "The attendance face service is still starting; try again shortly.")
    raise FaceGalleryError(502, f"The attendance face service failed: {detail}")


async def identify(image: bytes) -> dict[str, Any]:
    """``face_identify`` on one photo: every face, each looked up."""
    resp = await _call("POST", "/infer",
                       files={"frame": ("photo.jpg", image, "image/jpeg")},
                       data={"params": json.dumps({"task": "face_identify"})})
    _raise_for(resp)
    body = resp.json()
    result = body.get("result") if isinstance(body, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("faces"), list):
        raise FaceGalleryError(
            502, "The attendance face service does not support face_identify; "
                 "update its image (ai-adapter with face_identify).")
    return result


async def register(person_uuid: str, name: str, image: bytes) -> int:
    """Add one photo to the person's samples; returns how many they have."""
    resp = await _call("POST", "/faces/register",
                       files={"frame": ("photo.jpg", image, "image/jpeg")},
                       data={"person_id": person_uuid, "name": name,
                             "category": "attendance", "append": "true"})
    _raise_for(resp)
    face = (resp.json() or {}).get("face") or {}
    return int(face.get("samples") or 0)


async def forget_sample(person_uuid: str, index: int) -> int:
    """Delete one of the person's samples (0 = the oldest); returns how
    many are left. The adapter keeps at least one: removing the last is
    a 409, and forgetting the person is how all of them go."""
    resp = await _call("DELETE", f"/faces/{person_uuid}/samples/{index}")
    if resp.status_code == 404:
        raise FaceGalleryError(404, "That photo is no longer enrolled.")
    if resp.status_code == 409:
        raise FaceGalleryError(409, (
            "This is their only enrolled photo. To remove it, delete all of "
            "their face photos."))
    _raise_for(resp)
    face = (resp.json() or {}).get("face") or {}
    return int(face.get("samples") or 0)


async def forget(person_uuid: str) -> bool:
    """Delete every sample of the person. True if there were any; a person
    the adapter does not know is already forgotten, not an error."""
    resp = await _call("DELETE", f"/faces/{person_uuid}")
    if resp.status_code == 404:
        return False
    _raise_for(resp)
    return True


def duplicate_of(result: dict[str, Any], person_uuid: str) -> dict[str, Any] | None:
    """The face in a one-face identify result, if it is somebody else
    already enrolled at DUPLICATE_SIMILARITY or closer."""
    faces = result.get("faces") or []
    if len(faces) != 1:
        return None
    face = faces[0]
    other = face.get("person_id")
    sim = face.get("similarity")
    if (other and other != person_uuid and isinstance(sim, (int, float))
            and sim >= DUPLICATE_SIMILARITY):
        return {"person_uuid": other, "similarity": float(sim)}
    return None


def photo_path(person_uuid: str) -> Path | None:
    if not _UUID.match(person_uuid or ""):
        return None
    from core.config import settings

    return Path(settings.recordings_base_path) / _PHOTO_DIR / f"{person_uuid}.jpg"


def has_photo(person_uuid: str) -> bool:
    path = photo_path(person_uuid)
    return path is not None and path.is_file()


def save_photo(person_uuid: str, data: bytes) -> None:
    """Replace the person's picture. Only a JPEG, and only a small one."""
    if not data or len(data) > MAX_PHOTO_BYTES:
        raise ValueError(f"the picture must be 1..{MAX_PHOTO_BYTES // 1024} KB")
    if not data.startswith(b"\xff\xd8"):
        raise ValueError("the picture is not a JPEG")
    path = photo_path(person_uuid)
    if path is None:
        raise ValueError("bad person id")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".partial")
    tmp.write_bytes(data)
    tmp.replace(path)


def drop_photo(person_uuid: str) -> None:
    """Delete the person's picture and every photo kept of them."""
    path = photo_path(person_uuid)
    if path is not None:
        path.unlink(missing_ok=True)
        shutil.rmtree(path.with_suffix(""), ignore_errors=True)


# ── The enrolled photos, kept so a person can see what was enrolled ──
#
# The adapter keeps one embedding per enrolled photo, in order, oldest
# first, and deletes one by its position. Core keeps the photos beside
# them in ``<uuid>/photos.json``: one entry per sample, in the same
# order, ``{"file", "at"}`` — or ``null`` for a sample enrolled before
# photos were kept, or a photo too large to keep. The list is fitted to
# the adapter's count on every change: padded at the front for samples
# it never saw, trimmed at the front when the adapter dropped its oldest.

#: An enrolled photo larger than this is enrolled but not kept (the page
#: sends photos scaled to 1600 px, a few hundred KB).
MAX_KEPT_PHOTO_BYTES = 2 * 1024 * 1024


def _photos_dir(person_uuid: str) -> Path | None:
    path = photo_path(person_uuid)
    return path.with_suffix("") if path is not None else None


def _read_manifest(folder: Path) -> list[dict[str, Any] | None]:
    try:
        data = json.loads((folder / "photos.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [e if isinstance(e, dict) else None for e in data] if isinstance(data, list) else []


def _write_manifest(folder: Path, entries: list[dict[str, Any] | None]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / "photos.json.partial"
    tmp.write_text(json.dumps(entries), encoding="utf-8")
    tmp.replace(folder / "photos.json")


def _fit(folder: Path, entries: list[dict[str, Any] | None], count: int) -> list[dict[str, Any] | None]:
    """The list at ``count`` entries: older samples it never saw padded
    in front, samples the adapter has dropped (its oldest) cut off the
    front with their files."""
    while len(entries) > count:
        gone = entries.pop(0)
        if gone:
            (folder / gone["file"]).unlink(missing_ok=True)
    return [None] * (count - len(entries)) + entries


def kept_photos(person_uuid: str, count: int) -> list[dict[str, Any] | None]:
    """One entry per enrolled sample (``count``, the adapter's number):
    ``{"file", "at"}``, or None where no photo was kept."""
    folder = _photos_dir(person_uuid)
    if folder is None:
        return [None] * count
    entries = _read_manifest(folder)
    return [None] * (count - len(entries)) + entries[-count:] if count else []


def kept_photo_file(person_uuid: str, count: int, index: int) -> Path | None:
    entries = kept_photos(person_uuid, count)
    folder = _photos_dir(person_uuid)
    if folder is None or not 0 <= index < len(entries) or entries[index] is None:
        return None
    path = folder / entries[index]["file"]
    return path if path.is_file() else None


def keep_photo(person_uuid: str, count: int, image: bytes) -> None:
    """Record the photo just enrolled as the newest of ``count`` samples.
    Never fails an enrolment: a photo that cannot be kept is a gap."""
    folder = _photos_dir(person_uuid)
    if folder is None:
        return
    try:
        entries = _read_manifest(folder)
        entry = None
        if image.startswith(b"\xff\xd8") and len(image) <= MAX_KEPT_PHOTO_BYTES:
            folder.mkdir(parents=True, exist_ok=True)
            name = f"{uuidlib.uuid4().hex}.jpg"
            (folder / name).write_bytes(image)
            entry = {"file": name, "at": time.time()}
        entries = _fit(folder, entries + [entry], count)
        _write_manifest(folder, entries)
    except OSError:
        logger.warning("attendance: could not keep an enrolled photo of %s", person_uuid, exc_info=True)


def drop_kept_photo(person_uuid: str, count_before: int, index: int) -> None:
    """The sample at ``index`` (of ``count_before``) was deleted."""
    folder = _photos_dir(person_uuid)
    if folder is None:
        return
    try:
        entries = _fit(folder, _read_manifest(folder), count_before)
        if 0 <= index < len(entries):
            gone = entries.pop(index)
            if gone:
                (folder / gone["file"]).unlink(missing_ok=True)
        _write_manifest(folder, entries)
    except OSError:
        logger.warning("attendance: could not drop a kept photo of %s", person_uuid, exc_info=True)
