# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0002 Phase 0: core consumes ``plate.recognized.v1``.

``apply_plate_event`` is the decision core: envelope in, one status
token out, with the timeline row as the only side effect. These tests
pin the contract behaviours that make the two writers (bus consumer +
enrichment's synchronous fallback) safe to run together: never
overwrite an enriched row, ignore events that carry no timeline
reference, and treat malformed input as a no-op — never an exception.

Also pinned here: the enrichment fallback now threads ``event_id``
into its infer payload, because that reference is what lets the domain
event join back to the row at all.
"""

from __future__ import annotations

import os
import secrets
import sys
import types as _types
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_pec_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import core.database as cdb  # noqa: E402
import models  # noqa: E402
from services.plate_event_consumer import apply_plate_event  # noqa: E402


def _envelope(event_id, plate="ABC1234", **overrides):
    env = {
        "id": "evt_0123456789ab",
        "schema": "plate.recognized.v1",
        "correlation_id": "corr-1",
        "camera_id": "3",
        "ts": "2026-08-29T10:00:00+00:00",
        "producer": "kai-c",
        "payload": {
            "plate_text": plate,
            "confidence": 0.9,
            "vehicle_label": None,
            "event_id": event_id,
        },
    }
    env.update(overrides)
    return env


@pytest.fixture()
def db(monkeypatch):
    # Some suites pop core.* / models from sys.modules and leave docker
    # hostnames in the env; apply_plate_event's lazy imports would then
    # re-execute core.config's Settings() and trip its trust-zone
    # validator. Pin the modules this file already imported so the lazy
    # imports resolve to THESE objects (and this fixture's patches).
    monkeypatch.setitem(sys.modules, "core.database", cdb)
    monkeypatch.setitem(sys.modules, "models", models)
    eng = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(eng)
    SessionLocal = sessionmaker(bind=eng)
    monkeypatch.setattr(cdb, "SessionLocal", SessionLocal)
    s = SessionLocal()
    role = models.Role(name="admin")
    s.add(role)
    s.commit()
    user = models.User(username="u", email="u@x", hashed_password="x",
                       role_id=role.id)
    s.add(user)
    s.commit()
    cam = models.Camera(name="c", ip_address="10.0.0.5", owner_id=user.id)
    s.add(cam)
    s.commit()
    row = models.TimelineEvent(
        camera_id=cam.id, source="tier0", event_type="track", label="car",
        started_at=datetime(2026, 8, 29, 9, 59, tzinfo=timezone.utc),
    )
    s.add(row)
    s.commit()
    row_id = row.id
    s.close()
    yield SessionLocal, row_id


def _plate_of(SessionLocal, row_id):
    s = SessionLocal()
    try:
        return s.get(models.TimelineEvent, row_id).plate_text
    finally:
        s.close()


def test_applies_plate_to_referenced_row(db):
    SessionLocal, row_id = db
    assert apply_plate_event(_envelope(row_id)) == "applied"
    assert _plate_of(SessionLocal, row_id) == "ABC1234"


def test_never_overwrites_an_enriched_row(db):
    SessionLocal, row_id = db
    assert apply_plate_event(_envelope(row_id)) == "applied"
    # Redelivery / fallback race with a DIFFERENT read: first write wins.
    assert apply_plate_event(_envelope(row_id, plate="ZZZ999")) == "already-set"
    assert _plate_of(SessionLocal, row_id) == "ABC1234"


def test_missing_row_is_a_noop(db):
    _, row_id = db
    assert apply_plate_event(_envelope(row_id + 999)) == "not-found"


def test_dispatch_initiated_events_without_event_id_are_deferred(db):
    # Tier-1 dispatch doesn't reference a timeline row yet — joining
    # those is Phase 4. Until then the consumer must leave them alone.
    for missing in (None, "42", 3.5, True):
        assert apply_plate_event(_envelope(missing)) == "no-event-id"


def test_malformed_envelopes_never_raise(db):
    _, row_id = db
    assert apply_plate_event(None) == "malformed"
    assert apply_plate_event("junk") == "malformed"
    assert apply_plate_event({"payload": "junk"}) == "malformed"
    assert apply_plate_event(_envelope(row_id, plate="")) == "no-plate"
    assert apply_plate_event(_envelope(row_id, plate=None)) == "no-plate"


def test_plate_is_trimmed_and_capped(db):
    SessionLocal, row_id = db
    assert apply_plate_event(_envelope(row_id, plate="  " + "A" * 40)) == "applied"
    assert _plate_of(SessionLocal, row_id) == "A" * 32


def test_enrichment_fallback_threads_event_id():
    # The join key: enrichment's infer payload must carry the row id so
    # KAI-C's normaliser can put it in the domain event. String-level on
    # the source (the function does live HTTP; its request-building isn't
    # separable without refactoring it — deliberately out of scope here).
    src = (_HERE / "services" / "plate_enrichment.py").read_text()
    assert 'event_id=int(row.id)' in src, (
        "plate_enrichment no longer sends event_id with its OCR call — "
        "the plate.recognized.v1 it triggers can't be joined back to the "
        "visit row, so the bus consumer becomes a no-op for the fallback "
        "path (RFC-0002 Phase 0 convergence)")


def test_enrichment_sends_the_camera_handle_not_the_numeric_id():
    # Both plate producers must put the platform HANDLE ("cam{N}") in
    # camera_id: Tier-1 dispatch already speaks handles, and a consumer
    # scoping to assigned cameras (the LPR app) compares against
    # handles — "3" != "cam3" would silently drop every
    # enrichment-produced event.
    src = (_HERE / "services" / "plate_enrichment.py").read_text()
    assert 'camera_handle = f"cam{row.camera_id}"' in src, (
        "plate_enrichment no longer sends the camera handle — "
        "enrichment-produced plate.recognized.v1 events become "
        "invisible to camera-scoped consumers")
