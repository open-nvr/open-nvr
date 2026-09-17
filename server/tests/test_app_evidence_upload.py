# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""An app stores a photo, then cites it from an alert.

The reason this path exists at all: an alert travels over NATS, whose
default payload ceiling is 1 MB, so an app cannot put a crop inside the
alert — past the ceiling the broker drops the publish and the operator
never sees the alarm at all. The photo goes to the evidence store first
and the alert carries a path.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("INTERNAL_API_KEY", "site_" + secrets.token_hex(16))
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-evidence-upload")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from core.config import settings  # noqa: E402
from core.database import Base, get_db  # noqa: E402
from routers import app_platform  # noqa: E402

#: Whatever key this process ended up configured with. Read at import
#: rather than set here: another test module may have imported settings
#: first, and a key we merely WISH were configured authenticates nothing.
SITE_KEY = settings.internal_api_key

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 900
NOT_JPEG = b"GIF89a" + b"\x00" * 100


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)

    from services import evidence_store

    monkeypatch.setattr(evidence_store.settings, "recordings_base_path",
                        str(tmp_path))

    app = FastAPI()
    app.include_router(app_platform.router)

    def _db():
        s = SessionLocal()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as tc:
        yield tc, tmp_path


def _post(tc, body, content_type="image/jpeg"):
    return tc.post("/internal/app/evidence", content=body,
                   headers={"X-Internal-Api-Key": SITE_KEY,
                            "Content-Type": content_type})


def test_a_jpeg_comes_back_as_a_path_and_lands_on_disk(client):
    tc, root = client
    r = _post(tc, JPEG)
    assert r.status_code == 200
    rel = r.json()["path"]
    assert rel.endswith(".jpg")
    assert (root / ".evidence" / rel).read_bytes() == JPEG


def test_the_same_photo_twice_is_the_same_path(client):
    """Content-addressed: a retry after a failed alert costs nothing."""
    tc, _ = client
    assert _post(tc, JPEG).json()["path"] == _post(tc, JPEG).json()["path"]


def test_something_that_is_not_a_jpeg_is_refused(client):
    tc, _ = client
    assert _post(tc, NOT_JPEG).status_code == 400
    assert _post(tc, b"").status_code == 400


def test_an_oversized_upload_is_refused_before_it_is_read(client):
    """Declared length over the cap is rejected on the header, so a
    misbehaving app cannot make core hold the body in memory first."""
    tc, _ = client
    from services.evidence_store import MAX_EVIDENCE_BYTES

    r = tc.post("/internal/app/evidence", content=b"\xff\xd8" + b"\x00" * 10,
                headers={"X-Internal-Api-Key": SITE_KEY,
                         "Content-Type": "image/jpeg",
                         "Content-Length": str(MAX_EVIDENCE_BYTES + 1)})
    assert r.status_code == 413


def test_no_key_no_upload(client):
    tc, _ = client
    r = tc.post("/internal/app/evidence", content=JPEG,
                headers={"Content-Type": "image/jpeg"})
    assert r.status_code in (401, 403)
