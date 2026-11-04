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
from typing import Any

import httpx

logger = logging.getLogger(__name__)

#: A photo that matches another enrolled person this closely is refused as
#: a duplicate: it is either the same person enrolled twice (two rows that
#: would share every sighting) or the wrong photo under this name.
DUPLICATE_SIMILARITY = 0.6
_TIMEOUT = httpx.Timeout(30.0, connect=5.0)


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
