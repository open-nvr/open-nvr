"""Integration secrets: encrypted at rest, masked on the API."""
from __future__ import annotations

import json
import os
import secrets
import sys
import types as _types
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_intsec_test.db")
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

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from core.database import Base  # noqa: E402
from core.sealed_json import MASK, merge_masked, needs_sealing, redact, seal, unseal  # noqa: E402
from models import Integration, IntegrationType, Role, User  # noqa: E402

PASSWORD = "s3cret-pa55"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, expire_on_commit=False)()
    s.add(Role(id=1, name="admin", description="t")); s.commit()
    s.add(User(id=1, username="o", email="o@x.t", hashed_password="x", role_id=1,
               is_superuser=True, is_active=True)); s.commit()
    yield s
    s.close()


def test_the_column_stores_ciphertext_and_reads_plaintext(db):
    row = Integration(name="broker", type=IntegrationType.MQTT, enabled=True,
                      config={"broker_url": "mqtt://b", "username": "u", "password": PASSWORD})
    db.add(row); db.commit()
    raw = db.execute(text("SELECT config FROM integrations WHERE id = :i"), {"i": row.id}).scalar()
    stored = json.loads(raw) if isinstance(raw, str) else raw
    assert stored["password"].startswith("enc:") and PASSWORD not in str(raw)
    assert stored["username"] == "u"                       # only secrets are sealed
    db.expire_all()
    assert db.get(Integration, row.id).config["password"] == PASSWORD


def test_seal_unseal_redact_and_merge_round_trip():
    sealed = seal({"password": PASSWORD, "url": "http://x"})
    assert sealed["password"].startswith("enc:") and sealed["url"] == "http://x"
    assert seal(sealed) == sealed                          # idempotent
    assert unseal(sealed)["password"] == PASSWORD
    assert needs_sealing({"password": PASSWORD}) and not needs_sealing(sealed)
    assert redact({"password": PASSWORD, "secret": "", "url": "u"}) == {
        "password": MASK, "secret": "", "url": "u"}
    # The edit form echoes the mask: the stored secret survives; a new
    # value replaces it; a mask for a secret never stored is dropped.
    stored = {"password": "old", "url": "u"}
    assert merge_masked(stored, {"password": MASK, "url": "v"}) == {"password": "old", "url": "v"}
    assert merge_masked(stored, {"password": "new", "url": "v"}) == {"password": "new", "url": "v"}
    assert merge_masked({}, {"password": MASK}) == {}


def test_a_rotated_key_leaves_the_row_loadable(monkeypatch):
    sealed = seal({"password": PASSWORD})
    from core.config import settings
    monkeypatch.setattr(settings, "credential_encryption_key", Fernet.generate_key().decode())
    out = unseal(sealed)
    assert out["password"].startswith("enc:")              # unusable, not a crash


@pytest.fixture()
def client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from core.auth import get_current_superuser
    from core.database import get_db
    from routers import integrations as router_mod

    app = FastAPI()
    app.include_router(router_mod.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_superuser] = lambda: db.get(User, 1)
    with TestClient(app) as c:
        yield c


def test_the_api_never_returns_the_secret_and_keeps_it_on_masked_updates(client, db, monkeypatch):
    from routers import integrations as router_mod
    monkeypatch.setattr(router_mod, "_reload_mqtt", lambda: None)
    body = {"name": "mail", "type": "email", "enabled": True,
            "config": {"smtp_host": "m", "username": "u", "password": PASSWORD}}
    created = client.post("/api/v1/integrations", json=body).json()
    assert created["config"]["password"] == MASK
    iid = created["id"]
    listed = client.get("/api/v1/integrations").json()
    assert all(i["config"].get("password") in (None, MASK) for i in listed)
    assert client.get(f"/api/v1/integrations/{iid}").json()["config"]["password"] == MASK
    # The edit form sends the mask back with a changed host: stored secret kept.
    updated = client.put(f"/api/v1/integrations/{iid}", json={
        "config": {"smtp_host": "m2", "username": "u", "password": MASK}}).json()
    assert updated["config"] == {"smtp_host": "m2", "username": "u", "password": MASK}
    db.expire_all()
    assert db.get(Integration, iid).config["password"] == PASSWORD
    # A new secret replaces it.
    client.put(f"/api/v1/integrations/{iid}", json={"config": {"smtp_host": "m2", "password": "new"}})
    db.expire_all()
    assert db.get(Integration, iid).config["password"] == "new"
