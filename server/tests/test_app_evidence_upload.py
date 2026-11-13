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


# ── reading one back ─────────────────────────────────────────────────
#
# A photo an app stored used to be write-only to it: the doorbell that
# uploads a face crop could not show that crop again after a restart,
# and a relay could not attach the picture its own alert cited.


def _get(tc, rel: str):
    return tc.get(f"/internal/app/evidence/{rel}",
                  headers={"X-Internal-Api-Key": SITE_KEY})


def test_a_photo_an_app_stored_can_be_read_back(client):
    tc, _ = client
    rel = _post(tc, JPEG).json()["path"]

    r = _get(tc, rel)
    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-type"].startswith("image/jpeg")


def test_a_camera_structured_path_is_not_readable(client):
    """THE boundary. The same root also holds Tier-0's visit evidence
    under guessable paths, and serving those would turn this into a way
    to walk the site's cameras by construction — a different feature,
    and not one anybody asked for.
    """
    tc, root = client
    victim = root / ".evidence" / "cam1" / "2026" / "09" / "22"
    victim.mkdir(parents=True, exist_ok=True)
    (victim / "frame.jpg").write_bytes(JPEG)

    assert _get(tc, "cam1/2026/09/22/frame.jpg").status_code == 404


@pytest.mark.parametrize("rel", [
    "ab/" + "f" * 64 + ".jpg.jpg",      # a suffix past the one we write
    "AB/" + "F" * 64 + ".jpg",          # the store writes lowercase hex
    "ab/" + "f" * 63 + ".jpg",          # one short of a sha256
    "ab/notahash.jpg",
    "notahash/" + "f" * 64 + ".jpg",
])
def test_anything_that_is_not_a_content_addressed_name_is_refused(client, rel):
    """The file is PUT THERE first, so a missing shape check would serve
    it. Asserting 404 on a path that does not exist would pass against
    no check at all — which is what the first version of this test did.
    """
    tc, root = client
    victim = root / ".evidence" / rel
    victim.parent.mkdir(parents=True, exist_ok=True)
    victim.write_bytes(JPEG)
    assert victim.is_file()

    assert _get(tc, rel).status_code == 404


@pytest.mark.parametrize("rel", ["../../../etc/passwd", "ab/../../secret.jpg", ""])
def test_traversal_is_refused_twice_over(client, rel):
    """The pattern admits no ``..`` and ``resolve_evidence`` re-checks
    containment under the root anyway. Neither alone is trusted."""
    tc, _ = client
    assert _get(tc, rel).status_code == 404


def test_a_well_formed_path_that_is_gone_is_a_404(client):
    """Retention sweeps evidence, so an app that stored a crop last
    month must be told the picture has aged out rather than get a
    surprise."""
    tc, _ = client
    assert _get(tc, "ab/" + "c" * 64 + ".jpg").status_code == 404


def test_no_key_no_read(client):
    tc, _ = client
    rel = _post(tc, JPEG).json()["path"]
    assert tc.get(f"/internal/app/evidence/{rel}").status_code in (401, 403)


def test_the_sdk_offers_it_in_both_flavours():
    """The SDK ships sync and async clients that must stay in step; a
    method on one and not the other is the drift the parity check
    exists to catch."""
    import inspect
    import pathlib

    sdk = (pathlib.Path(__file__).resolve().parents[2]
           / "sdk/opennvr-app-sdk/opennvr_app_sdk")
    for name in ("client.py", "aio.py"):
        src = (sdk / name).read_text()
        assert "def read_evidence(" in src, f"{name} has no read_evidence"
        assert "/api/v1/internal/app/evidence/" in src
    del inspect
