# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""#221 P0 hotfixes: disk usage reporting + off-loop MediaMTX calls."""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import types as _types
from pathlib import Path

from cryptography.fernet import Fernet

# Portability shim: some source modules do `from datetime import UTC`
# (Python 3.11+). Make the suite runnable on 3.10 too; no-op on 3.11.
import datetime as _dt  # noqa: E402
if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc

_HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HERE))
os.environ.setdefault("DATABASE_URL", "sqlite:///./_pb_test.db")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
# MediaMTX URLs on loopback so the trust-zone validator (V-015) passes
# whenever Settings() is rebuilt mid-suite (module-pollution safe).
os.environ.setdefault("MEDIAMTX_BASE_URL", "http://127.0.0.1:8889")
os.environ.setdefault("MEDIAMTX_ADMIN_API", "http://127.0.0.1:9997/v3")
os.environ.setdefault("MEDIAMTX_HLS_URL", "http://127.0.0.1:8888")
os.environ.setdefault("MEDIAMTX_PLAYBACK_URL", "http://127.0.0.1:9996")

_lm = _types.ModuleType("core.logging_config")


class _L:
    def __getattr__(self, _n):
        return lambda *a, **k: None


_lm.__getattr__ = lambda _n: _L()
_lm.setup_logging = lambda *a, **k: None
sys.modules.setdefault("core.logging_config", _lm)

import routers.recordings as rec  # noqa: E402
import services.storage_service as ss  # noqa: E402


# ── disk usage reporting (the statvfs unpack bug) ───────────────────

def test_get_storage_info_reports_disk_usage(tmp_path, monkeypatch):
    """Regression for #221: os.statvfs() unpack raised on Linux and was
    swallowed, so disk_total/used/free were never populated. shutil.disk_usage
    must now return real numbers."""
    monkeypatch.setattr(ss, "_effective_root", lambda store: tmp_path)

    class _Store:
        segment_seconds = 60

    monkeypatch.setattr(ss, "_load_storage_config", lambda db: _Store())

    info = ss.StorageService().get_storage_info(db=None)
    assert info["disk_total"] > 0
    assert info["disk_free"] > 0
    assert info["disk_free"] <= info["disk_total"]
    # used + free need not equal total exactly (reserved blocks), but must be sane
    assert info["disk_used"] >= 0


# ── blocking MediaMTX calls run off the event loop ──────────────────

def test_mediamtx_get_runs_off_the_event_loop():
    """_mediamtx_get must run the blocking GET in a WORKER thread, not on the
    event-loop thread. Deterministic (no timing): capture the thread the fake
    GET executes on and assert it differs from the loop thread."""
    import threading

    seen = {}

    def _fake_get(url, timeout=10):
        seen["thread"] = threading.get_ident()

        class _R:
            status_code = 200

            def json(self):
                return {"ok": True}

        return _R()

    async def _scenario():
        seen["loop_thread"] = threading.get_ident()
        return await rec._mediamtx_get("http://x/list")

    orig = rec.http_client.get
    rec.http_client.get = _fake_get
    try:
        resp = asyncio.run(_scenario())
    finally:
        rec.http_client.get = orig

    assert resp.json() == {"ok": True}
    # ran off the event-loop thread → cannot freeze the loop
    assert seen["thread"] != seen["loop_thread"]


# ── regression guard: no blocking MediaMTX GET left in async handlers ──

def test_no_bare_blocking_mediamtx_get_in_recordings():
    """#221 said 'same pattern at 8 call sites'. Guard against a new one
    sneaking in: recordings.py must not call http_client.get(...) directly —
    every MediaMTX metadata fetch goes through _mediamtx_get (off-loop) or
    asyncio.to_thread. (StreamingResponse byte-range proxying is a separate
    concern and lives in hls_playback_service, not here.)"""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "routers" / "recordings.py"
    text = src.read_text()
    # allow the definition of the helper itself; forbid bare call sites
    # exclude the one legitimate use: inside _mediamtx_get, wrapped in to_thread
    call_sites = [
        m for m in re.finditer(r"http_client\.get\(", text)
        if "asyncio.to_thread" not in text[max(0, m.start() - 40):m.start()]
    ]
    assert call_sites == [], (
        f"bare blocking http_client.get found: {len(call_sites)} site(s) — "
        "route MediaMTX GETs through _mediamtx_get / asyncio.to_thread"
    )
