"""Face enrolment and recognition on OpenNVR.

The seven calls the AWS version made against Rekognition, with the same
names and shapes, on the platform's InsightFace adapter:

* detection and recognition go through KAI-C (``POST /api/v1/infer/
  insightface``, tasks ``face_detection`` / ``face_recognition``) — the
  same door every app uses, audited and budgeted by core;
* enrolment, listing and deletion go to the adapter's ``/faces/*`` routes
  directly, which KAI-C does not proxy (the Smart Doorbell does the same).

Student codes are the adapter's ``person_id``; every entry attendly writes
carries ``category=attendly_student`` so it can be told apart from the
doorbell's household in the deployment's one shared face database. A
"face id" here is ``<person_id>#<sample index>``.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)


class FaceServiceError(RuntimeError):
    """KAI-C or the adapter could not be used (transport, 5xx, not ready)."""


class FaceRejected(ValueError):
    """The adapter refused this image for enrolment (no face / several)."""


def _image_size(image_bytes: bytes) -> Tuple[int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        return int(im.width), int(im.height)


def normalised_bbox(bbox: List[int] | Tuple[int, int, int, int], width: int, height: int) -> Dict[str, float]:
    """Pixel ``[x1, y1, x2, y2]`` → Rekognition-style normalised box."""
    x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
    w = max(1.0, float(width))
    h = max(1.0, float(height))
    return {
        "Left": max(0.0, min(1.0, x1 / w)),
        "Top": max(0.0, min(1.0, y1 / h)),
        "Width": max(0.0, min(1.0, (x2 - x1) / w)),
        "Height": max(0.0, min(1.0, (y2 - y1) / h)),
    }


def face_id(person_id: str, index: int) -> str:
    return f"{person_id}#{int(index)}"


def split_face_id(fid: str) -> Tuple[str, Optional[int]]:
    if "#" not in fid:
        return fid, None
    pid, _, idx = fid.rpartition("#")
    try:
        return pid, int(idx)
    except ValueError:
        return fid, None


class FaceEngine:
    _shared: "FaceEngine | None" = None
    _shared_lock = threading.Lock()

    def __init__(
        self, *, kaic_url: str, kaic_api_key: str, adapter_name: str, faces_url: str, faces_token: str,
        category: str, timeout_seconds: float = 15.0, http_client: httpx.Client | None = None,
    ) -> None:
        from opennvr_app_sdk.frame_app import KaiCClient

        self.adapter_name = adapter_name
        self.category = category
        self._faces_base = faces_url.rstrip("/")
        self._faces_headers = {"Authorization": f"Bearer {faces_token}"} if faces_token else {}
        self._timeout = timeout_seconds
        self._http = http_client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        self._kaic = KaiCClient(kaic_url, adapter_name, api_key=kaic_api_key or None,
                                timeout_seconds=timeout_seconds, http_client=self._http)
        # Does the adapter answer face_identify? Unknown until asked; a
        # refusal is remembered for a while so every frame does not pay
        # for a round trip that fails.
        self._identify_ok: Optional[bool] = None
        self._identify_checked_at = 0.0

    @classmethod
    def get_shared(cls) -> "FaceEngine":
        with cls._shared_lock:
            if cls._shared is None:
                s = get_settings()
                cls._shared = FaceEngine(
                    kaic_url=s.kaic_url, kaic_api_key=s.internal_api_key, adapter_name=s.recognition_adapter,
                    faces_url=s.faces_url, faces_token=s.effective_faces_token, category=s.face_category,
                    timeout_seconds=s.platform_timeout_seconds,
                )
            return cls._shared

    @classmethod
    def set_shared(cls, engine: "FaceEngine | None") -> None:
        with cls._shared_lock:
            cls._shared = engine

    # ── KAI-C: detect / recognise ───────────────────────────────────

    def _infer(self, image_bytes: bytes, task: str, params: Optional[dict] = None) -> Dict[str, Any]:
        from opennvr_app_sdk.frame_app import KaiCError

        try:
            body = self._kaic.infer(image_bytes, task=task, params=params or None)
        except KaiCError as exc:
            raise FaceServiceError(str(exc)) from exc
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict):
            raise FaceServiceError(f"KAI-C {task}: no result in response")
        return result

    def detect_faces(self, image_bytes: bytes) -> List[dict]:
        """Every face with a normalised ``BoundingBox`` (plus ``bbox_px`` and
        ``Confidence``), so callers crop exactly as before."""
        result = self._infer(image_bytes, "face_detection")
        faces = result.get("faces") or []
        if not faces:
            return []
        try:
            w, h = _image_size(image_bytes)
        except Exception as exc:
            raise FaceServiceError(f"could not read image size: {exc}") from exc
        out: List[dict] = []
        for f in faces:
            bbox = (f or {}).get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            out.append({
                "BoundingBox": normalised_bbox(bbox, w, h),
                "bbox_px": [int(v) for v in bbox[:4]],
                "Confidence": float((f or {}).get("confidence") or 0.0) * 100.0,
            })
        return out

    IDENTIFY_RECHECK_SECONDS = 600.0

    @property
    def identify_supported(self) -> Optional[bool]:
        return self._identify_ok

    def identify_faces(self, image_bytes: bytes, *, threshold: Optional[float] = None, match: bool = True,
                       min_face_px: Optional[int] = None, max_faces: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """``face_identify``: every face in the frame, each looked up (or,
        with ``match=False``, only where they are and how they are turned).
        ``None`` when this adapter does not answer the task — the caller
        falls back to detect-then-crop. Raises :class:`FaceServiceError`
        when the adapter is reachable but failed."""
        from opennvr_app_sdk.frame_app import KaiCError

        now = time.monotonic()
        if self._identify_ok is False and now - self._identify_checked_at < self.IDENTIFY_RECHECK_SECONDS:
            return None
        s = get_settings()
        params: Dict[str, Any] = {
            "threshold": min(1.0, max(0.01, float(threshold if threshold is not None else s.recognition_threshold))),
            "min_face_px": int(min_face_px if min_face_px is not None else s.min_face_px),
            "max_faces": int(max_faces if max_faces is not None else s.max_faces),
        }
        if not match:
            params["match"] = False
        try:
            body = self._kaic.infer(image_bytes, task="face_identify", params=params)
        except KaiCError as exc:
            text = str(exc)
            # KAI-C relays the adapter's refusal of an unknown task as a
            # 4xx; anything else is an outage, which is not "unsupported".
            if "HTTP 4" in text and ("task" in text.lower() or "unknown" in text.lower() or "unsupported" in text.lower()):
                self._identify_ok, self._identify_checked_at = False, now
                logger.info("adapter %s has no face_identify; using detect-then-crop", self.adapter_name)
                return None
            raise FaceServiceError(text) from exc
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or not isinstance(result.get("faces"), list):
            raise FaceServiceError("KAI-C face_identify: no faces list in response")
        self._identify_ok, self._identify_checked_at = True, now
        return result

    def search_faces(self, image_bytes: bytes, threshold: Optional[float] = None,
                     max_faces: int = 1) -> List[Tuple[str, float]]:
        """Who is the best face in this image? ``[(person_id, similarity)]``
        — one entry when recognised above ``threshold``, else empty. The
        adapter answers for one face per request; callers crop first."""
        thr = float(threshold if threshold is not None else get_settings().recognition_threshold)
        thr = min(1.0, max(0.01, thr))
        result = self._infer(image_bytes, "face_recognition", {"threshold": thr})
        if not result.get("recognized"):
            return []
        pid = result.get("person_id")
        if not pid:
            return []
        return [(str(pid), float(result.get("similarity") or 0.0))]

    # ── Adapter: enrol / list / delete ──────────────────────────────

    def _faces_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            return self._http.request(method, f"{self._faces_base}{path}", headers=self._faces_headers,
                                      timeout=self._timeout, **kwargs)
        except httpx.HTTPError as exc:
            raise FaceServiceError(f"face adapter unreachable at {self._faces_base}: {exc}") from exc

    def person(self, person_id: str) -> Optional[Dict[str, Any]]:
        resp = self._faces_request("GET", f"/faces/{person_id}")
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise FaceServiceError(f"face adapter GET /faces/{person_id}: HTTP {resp.status_code}")
        return (resp.json() or {}).get("face")

    def index_face(self, image_bytes: bytes, external_image_id: str, *, name: str = "",
                   metadata: Optional[dict] = None, category: Optional[str] = None) -> Optional[str]:
        """Add one photo to a person (creating them on the first). Returns
        the new sample's face id. Raises :class:`FaceRejected` when the
        adapter refuses the image, :class:`FaceServiceError` otherwise.
        ``category`` defaults to the students'; staff enrol under their own.
        The adapter is given the code only, never the name: another app
        asking it who is in a frame learns a code, and names stay here."""
        del name  # kept in the signature for callers; deliberately not sent
        meta = {"app": "attendly", **(metadata or {})}
        data = {
            "person_id": external_image_id, "name": "", "category": category or self.category,
            "metadata": json.dumps(meta), "append": "true",
        }
        files = {"frame": ("face.jpg", image_bytes, "image/jpeg")}
        resp = self._faces_request("POST", "/faces/register", data=data, files=files)
        if resp.status_code in (400, 413, 422):
            detail = ""
            try:
                detail = str(resp.json().get("detail", ""))
            except Exception:
                detail = resp.text[:200]
            raise FaceRejected(detail or f"HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise FaceServiceError(f"face adapter register: HTTP {resp.status_code} {resp.text[:200]}")
        face = (resp.json() or {}).get("face") or {}
        samples = int(face.get("samples") or 1)
        return face_id(external_image_id, samples - 1)

    def list_persons(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        resp = self._faces_request("GET", "/faces", params={"category": category or self.category})
        if resp.status_code >= 400:
            raise FaceServiceError(f"face adapter GET /faces: HTTP {resp.status_code}")
        return list((resp.json() or {}).get("faces") or [])

    def list_faces(self, max_results: int = 1000, category: Optional[str] = None) -> List[dict]:
        """One entry per enrolled sample, Rekognition-shaped."""
        out: List[dict] = []
        for p in self.list_persons(category):
            pid = str(p.get("person_id") or "")
            if not pid:
                continue
            for i in range(int(p.get("samples") or 0)):
                out.append({"FaceId": face_id(pid, i), "ExternalImageId": pid, "Confidence": None,
                            "Name": p.get("name"), "ImageId": None})
        return out

    def describe_collection(self, category: Optional[str] = None) -> dict:
        persons = self.list_persons(category)
        return {
            "CollectionId": self.adapter_name,
            "FaceCount": sum(int(p.get("samples") or 0) for p in persons),
            "PersonCount": len(persons),
            "FaceModelVersion": "insightface",
            "CreationTimestamp": None,
        }

    def get_faces_by_external_id(self, external_image_id: str, category: Optional[str] = None) -> List[dict]:
        p = self.person(external_image_id)
        if not p or str(p.get("category") or "") != (category or self.category):
            return []
        return [{"FaceId": face_id(external_image_id, i), "ExternalImageId": external_image_id,
                 "Confidence": None, "ImageId": None} for i in range(int(p.get("samples") or 0))]

    def delete_person(self, person_id: str) -> bool:
        resp = self._faces_request("DELETE", f"/faces/{person_id}")
        if resp.status_code == 404:
            return False
        if resp.status_code >= 400:
            raise FaceServiceError(f"face adapter DELETE /faces/{person_id}: HTTP {resp.status_code}")
        return True

    def delete_faces(self, face_ids: List[str]) -> dict:
        """Samples by id; when every sample of a person is named, the person
        goes. Sample indexes shift after each removal, so they are removed
        highest first."""
        by_person: Dict[str, List[Optional[int]]] = {}
        for fid in face_ids:
            pid, idx = split_face_id(fid)
            by_person.setdefault(pid, []).append(idx)
        deleted: List[dict] = []
        for pid, idxs in by_person.items():
            p = self.person(pid)
            total = int((p or {}).get("samples") or 0)
            wanted = sorted({i for i in idxs if i is not None}, reverse=True)
            if p is None:
                continue
            if None in idxs or (total and len(wanted) >= total):
                if self.delete_person(pid):
                    deleted.extend({"FaceId": face_id(pid, i)} for i in range(total))
                continue
            for i in wanted:
                resp = self._faces_request("DELETE", f"/faces/{pid}/samples/{i}")
                if resp.status_code < 400:
                    deleted.append({"FaceId": face_id(pid, i)})
        return {"DeletedFaces": deleted}
