"""Two AI endpoints that answered a bare 500 on every call.

Both were NameErrors, found by the UI route smoke test (tests/e2e/tests/ui/
test_route_smoke.py) rather than by any API test, because nothing exercised
them outside a page load:

* ``get_inference_manager()`` declared ``global _inference_manager`` but the
  module never defined it, so the first call raised NameError. Every
  endpoint using the manager -- ``GET /ai-model-management/inference/running``
  on each AI Models page load -- answered 500.
* ``GET /ai-models/adapters-metrics`` used ``kai_c_service`` without binding
  it, so the AI Adapters page's fleet strip never loaded.

Run with:

    cd server && pytest tests/test_ai_endpoints_boot.py -v
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types as _types
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_ai_endpoints_boot_test.db")
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

from fastapi import HTTPException  # noqa: E402


def test_the_inference_manager_singleton_can_be_created():
    from services import inference_manager as im

    first = im.get_inference_manager()
    assert first is im.get_inference_manager()
    assert isinstance(first.get_running_models(), list)


def test_adapters_metrics_reports_kaic_down_as_502_not_500(monkeypatch):
    from routers import ai_models

    class _Down:
        async def get_fleet_metrics(self):
            raise httpx.ConnectError("KAI-C is not running")

    monkeypatch.setattr(ai_models, "get_kai_c_service", lambda: _Down())

    with pytest.raises(HTTPException) as err:
        asyncio.run(ai_models.get_fleet_metrics(current_user=object()))
    assert err.value.status_code == 502


def test_adapters_metrics_returns_the_rollup(monkeypatch):
    from routers import ai_models

    class _Up:
        async def get_fleet_metrics(self):
            return {"adapters": {"a": {"ok": True}}}

    monkeypatch.setattr(ai_models, "get_kai_c_service", lambda: _Up())

    body = asyncio.run(ai_models.get_fleet_metrics(current_user=object()))
    assert body == {"adapters": {"a": {"ok": True}}}
