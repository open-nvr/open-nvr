"""Every test runs against a fresh SQLite file, with the face engine and
the camera source replaced by in-memory fakes, and the auth gate in
``none`` mode unless a test installs its own gate."""
from __future__ import annotations

import os
from typing import Dict, List, Optional, Tuple

import numpy as np
import pytest

os.environ.setdefault("ATTENDLY_AUTH_MODE", "none")
os.environ.setdefault("ATTENDLY_LISTEN_HOST", "127.0.0.1")
os.environ.setdefault("OPENNVR_URL", "http://core.test")
os.environ.setdefault("KAIC_URL", "http://kaic.test")
os.environ.setdefault("OPENNVR_INTERNAL_API_KEY", "site-key")
os.environ.setdefault("ATTENDLY_FACES_URL", "http://faces.test")


def jpeg_bytes(width: int = 64, height: int = 48, seed: int = 1) -> bytes:
    """A small valid JPEG whose pixels depend on ``seed``."""
    import cv2

    rng = np.random.default_rng(seed)
    img = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


class FakeFaceEngine:
    """The FaceEngine surface, scripted: ``enrolled`` maps person_id →
    samples; ``match`` says who a given image recognises as; ``faces_in``
    says how many faces detection reports for an image."""

    adapter_name = "insightface"
    category = "attendly_student"

    def __init__(self) -> None:
        self.enrolled: Dict[str, Dict] = {}
        self.match: Dict[bytes, Tuple[str, float]] = {}
        self.faces_in: Dict[bytes, List[List[int]]] = {}
        self.default_faces: List[List[int]] = []
        self.reject: set[bytes] = set()
        # 1-based index of index_face calls to refuse (the routers enhance
        # images before enrolling, so bytes-keyed rejection cannot match)
        self.reject_calls: set[int] = set()
        self.index_calls = 0
        self.calls: List[str] = []

    # KAI-C side
    def detect_faces(self, image_bytes: bytes) -> List[dict]:
        self.calls.append("detect")
        boxes = self.faces_in.get(image_bytes, self.default_faces)
        return [{"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.5, "Height": 0.5}, "bbox_px": list(b), "Confidence": 90.0}
                for b in boxes]

    def search_faces(self, image_bytes: bytes, threshold: Optional[float] = None, max_faces: int = 1):
        self.calls.append("search")
        hit = self.match.get(image_bytes)
        if hit is None:
            return []
        pid, sim = hit
        if threshold is not None and sim < threshold:
            return []
        return [(pid, sim)]

    # adapter side
    def person(self, person_id: str):
        return self.enrolled.get(person_id)

    def index_face(self, image_bytes: bytes, external_image_id: str, *, name: str = "", metadata=None,
                   category: Optional[str] = None) -> Optional[str]:
        from attendly.face.engine import FaceRejected

        self.calls.append("index")
        self.index_calls += 1
        if image_bytes in self.reject or self.index_calls in self.reject_calls:
            raise FaceRejected("no face detected in the supplied image")
        rec = self.enrolled.setdefault(external_image_id, {"person_id": external_image_id, "name": name,
                                                           "category": category or self.category, "samples": 0})
        if name:
            rec["name"] = name
        rec["samples"] += 1
        return f"{external_image_id}#{rec['samples'] - 1}"

    def list_persons(self, category: Optional[str] = None):
        cat = category or self.category
        return [dict(r) for r in self.enrolled.values() if r["category"] == cat]

    def list_faces(self, max_results: int = 1000, category: Optional[str] = None):
        return [{"FaceId": f"{pid}#{i}", "ExternalImageId": pid, "Confidence": None, "Name": r["name"], "ImageId": None}
                for pid, r in self.enrolled.items() if r["category"] == (category or self.category)
                for i in range(r["samples"])]

    def describe_collection(self, category: Optional[str] = None):
        persons = self.list_persons(category)
        return {"CollectionId": self.adapter_name, "FaceCount": sum(r["samples"] for r in persons),
                "PersonCount": len(persons), "FaceModelVersion": "insightface", "CreationTimestamp": None}

    def get_faces_by_external_id(self, external_image_id: str, category: Optional[str] = None):
        r = self.enrolled.get(external_image_id)
        if not r or r["category"] != (category or self.category):
            return []
        return [{"FaceId": f"{external_image_id}#{i}", "ExternalImageId": external_image_id, "Confidence": None, "ImageId": None}
                for i in range(r["samples"])]

    def delete_person(self, person_id: str) -> bool:
        self.calls.append("delete_person")
        return self.enrolled.pop(person_id, None) is not None

    def delete_faces(self, face_ids):
        return {"DeletedFaces": [{"FaceId": f} for f in face_ids]}

    def _faces_request(self, *a, **k):
        class _R:
            status_code = 200

            def json(self):
                return {}
        return _R()


class FakeCameraSource:
    def __init__(self) -> None:
        self.cameras = [{"id": 7, "handle": "cam7", "name": "Lab 1 camera", "role": "classroom"},
                        {"id": 9, "handle": "cam9", "name": "Hall camera", "role": ""}]
        self.frames: Dict[int, Optional[np.ndarray]] = {}
        self.snapshots = 0

    def list_cameras(self):
        return [dict(c) for c in self.cameras]

    def camera_name(self, camera_id: int):
        for c in self.cameras:
            if c["id"] == int(camera_id):
                return c["name"]
        return None

    def snapshot_jpeg(self, camera_id: int):
        frame = self.snapshot(camera_id)
        if frame is None:
            return None
        import cv2

        return cv2.imencode(".jpg", frame)[1].tobytes()

    def snapshot(self, camera_id: int):
        self.snapshots += 1
        return self.frames.get(int(camera_id))


class FakePublisher:
    """The AccessPublisher surface: remembers every decision it was handed."""

    nats_url = "nats://fake"

    def __init__(self) -> None:
        self.events: List[tuple] = []
        self.alerts_sent: List[object] = []
        self.published = 0
        self.alerts = 0
        self.last_error = None
        self.fail = False

    def publish(self, gate, decision) -> bool:
        from attendly.services.access import AccessPublisher

        self.events.append((gate, decision, AccessPublisher.payload(decision)))
        if self.fail:
            return False
        self.published += 1
        return True

    def alert(self, gate, decision, movement, *, ts) -> bool:
        from attendly.services.access import build_alert

        a = build_alert(gate, decision, movement, ts=ts)
        if a is None:
            return False
        self.alerts_sent.append(a)
        self.alerts += 1
        return True


@pytest.fixture
def fake_engine():
    return FakeFaceEngine()


@pytest.fixture
def fake_cameras():
    return FakeCameraSource()


@pytest.fixture
def env(tmp_path, monkeypatch, fake_engine, fake_cameras):
    """Fresh settings + database + fakes for one test."""
    from attendly.cameras import CameraSource
    from attendly.config import reset_settings_cache
    from attendly.face.engine import FaceEngine
    from attendly.gate import set_gate
    from attendly.services.access import AccessPublisher, GateEngine, set_publisher
    from attendly.services.engine import StreamEngine
    from attendly.services.staff_attendance import StaffEngine
    from attendly.storage.db import init_db, reset_db

    monkeypatch.setenv("ATTENDLY_DATABASE_URL", f"sqlite:///{tmp_path / 'attendly.db'}")
    monkeypatch.setenv("ATTENDLY_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ATTENDLY_AUTH_MODE", "none")
    reset_settings_cache()
    reset_db()
    init_db()
    FaceEngine.set_shared(fake_engine)  # type: ignore[arg-type]
    CameraSource.set_shared(fake_cameras)  # type: ignore[arg-type]
    StreamEngine.set_shared(StreamEngine(recognizer=fake_engine, frames=fake_cameras))
    publisher = FakePublisher()
    set_publisher(publisher)
    GateEngine.set_shared(GateEngine(recognizer=fake_engine, frames=fake_cameras, publisher=publisher))
    StaffEngine.set_shared(StaffEngine(recognizer=fake_engine, frames=fake_cameras))
    set_gate(None)
    yield {"engine": fake_engine, "cameras": fake_cameras, "tmp": tmp_path, "publisher": publisher}
    for eng in (GateEngine, StaffEngine):
        try:
            eng.get_shared().shutdown()
        except Exception:
            pass
    GateEngine.set_shared(None)
    StaffEngine.set_shared(None)
    set_publisher(None)
    StreamEngine.set_shared(None)
    FaceEngine.set_shared(None)
    CameraSource.set_shared(None)
    set_gate(None)
    reset_db()
    reset_settings_cache()


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient

    import attendly_app

    app = attendly_app.build_app(start_engine=False, register=False)
    with TestClient(app) as c:
        yield c
