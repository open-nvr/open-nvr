# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The History card's backend + the demo page's new honesty surfaces.

/history is the browsable face of search_history: remembered visits with
their kept photos. Same honesty contract as the tool — "history isn't
wired" (available:false), "store unreachable" (ok:false), and "no visits"
(empty events) are three DIFFERENT answers. /history/{id}/evidence serves
the kept JPEG. /health now carries vision_error + history for the header
health dot.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from camera_agent import AppConfig, CameraAgentRuntime, build_app
from context import CameraSpec


def _runtime():
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True,
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                                        role="front door", opennvr_camera_id=7),
                             CameraSpec(camera_id="local0", frame_url="device:0",
                                        role="laptop cam")])
    return CameraAgentRuntime(cfg)


class _FakeEvents:
    def __init__(self, rows=None, jpeg=b"\xff\xd8jpeg"):
        self.rows = rows
        self.jpeg = jpeg
        self.calls = []

    async def search(self, **kw):
        self.calls.append(kw)
        return self.rows

    async def evidence(self, event_id):
        return self.jpeg if event_id == 12 else None


def _visit(**over):
    base = dict(id=12, camera_id=7, label="person", score=0.9,
                started_at="2026-08-24T14:02:00+05:30",
                ended_at="2026-08-24T14:05:00+05:30",
                stationary=False, plate_text=None, has_evidence=True)
    base.update(over)
    return SimpleNamespace(**base)


# ── /history ────────────────────────────────────────────────────────────

def test_history_unwired_reports_unavailable():
    rt = _runtime()
    rt.events_client = None
    body = TestClient(build_app(rt)).get("/history").json()
    assert body == {"available": False, "ok": False, "events": []}


def test_history_rows_mapped_to_agent_cameras():
    rt = _runtime()
    rt.events_client = _FakeEvents(rows=[_visit()])
    body = TestClient(build_app(rt)).get("/history?label=person").json()
    assert body["available"] and body["ok"]
    (row,) = body["events"]
    assert row["camera_id"] == "cam1"          # server id 7 → agent id
    assert row["role"] == "front door"
    assert row["has_evidence"] is True
    assert row["id"] == 12


def test_history_store_failure_is_not_an_empty_window():
    rt = _runtime()
    rt.events_client = _FakeEvents(rows=None)   # search() → None = failure
    body = TestClient(build_app(rt)).get("/history").json()
    assert body["available"] is True
    assert body["ok"] is False                  # ≠ "no visits"
    assert body["events"] == []


def test_history_camera_filter_resolves_server_id():
    rt = _runtime()
    fake = _FakeEvents(rows=[])
    rt.events_client = fake
    body = TestClient(build_app(rt)).get("/history?camera_id=cam1").json()
    assert body["ok"] and body["events"] == []
    assert fake.calls[0]["camera_id"] == 7


def test_history_unknown_camera_404s():
    rt = _runtime()
    rt.events_client = _FakeEvents(rows=[])
    r = TestClient(build_app(rt)).get("/history?camera_id=nope")
    assert r.status_code == 404


def test_history_local_camera_truthfully_empty():
    # A camera the NVR doesn't record has no history — empty, not an error,
    # and the store is never queried.
    rt = _runtime()
    fake = _FakeEvents(rows=[_visit()])
    rt.events_client = fake
    body = TestClient(build_app(rt)).get("/history?camera_id=local0").json()
    assert body["ok"] and body["events"] == []
    assert fake.calls == []


# ── /history/{id}/evidence ──────────────────────────────────────────────

def test_evidence_served_as_jpeg():
    rt = _runtime()
    rt.events_client = _FakeEvents()
    r = TestClient(build_app(rt)).get("/history/12/evidence")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")


def test_evidence_missing_404s():
    rt = _runtime()
    rt.events_client = _FakeEvents()
    assert TestClient(build_app(rt)).get("/history/99/evidence").status_code == 404
    rt.events_client = None
    assert TestClient(build_app(rt)).get("/history/99/evidence").status_code == 404


# ── /health: header health dot payload ──────────────────────────────────

def test_health_carries_vision_error_and_history_flag():
    rt = _runtime()
    rt.events_client = _FakeEvents()
    rt.tools.last_vision_error = "adapter unreachable"
    h = TestClient(build_app(rt)).get("/health").json()
    assert h["vision_error"] == "adapter unreachable"
    assert h["history"] is True
    rt.tools.last_vision_error = None
    rt.events_client = None
    h = TestClient(build_app(rt)).get("/health").json()
    assert h["vision_error"] is None
    assert h["history"] is False


# ── demo page: the new UI surfaces exist and are wired safely ───────────

_HTML = (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()


def test_demo_has_history_card_and_filters():
    for needle in ('id="historyCard"', 'id="histList"', 'id="histLabel"',
                   'id="histCam"', 'id="histHours"', 'id="histRefresh"'):
        assert needle in _HTML, needle


def test_demo_history_thumbs_use_authed_fetch_not_bare_src():
    # <img src="/history/..."> would arrive without the bearer header and
    # 401 — thumbnails must go through the patched fetch() into a blob URL.
    assert 'fetch("/history/"' in _HTML
    assert 'src="/history/' not in _HTML


def test_demo_has_suggestion_chips_and_health_dot():
    assert 'id="suggestRow"' in _HTML
    assert "renderSuggestions" in _HTML
    assert 'id="healthDot"' in _HTML
    assert "pollHealth" in _HTML
    assert "vision_error" in _HTML


# ── History collapse ───────────────────────────────────────────────────

def test_history_card_can_be_collapsed_like_the_others():
    # Activity and Hardware both fold; History — the card that grows most,
    # up to 30 visits each with a photo — was the only one that could not.
    assert 'id="histToggle"' in _HTML
    assert "applyHistCollapse" in _HTML
    # Same session-scoped persistence + glyph convention as Activity.
    assert 'sessionStorage.setItem("histCollapsed"' in _HTML
    assert 'sessionStorage.getItem("histCollapsed")' in _HTML


def test_collapsed_history_skips_the_evidence_photo_fetches():
    """Collapsing must stop the work, not just hide it.

    Each visible visit fires its own authenticated evidence fetch, so a
    30-row card is 30 requests per refresh. The availability call above the
    guard still runs (it decides whether the card and its phone tab exist),
    but nothing below it should build rows nobody can see.
    """
    body = _HTML[_HTML.index("async function loadHistory"):]
    guard = body.index("if(_histCollapsed()) return;")
    first_thumb_fetch = body.index('fetch("/history/"')
    assert guard < first_thumb_fetch, (
        "the collapsed-return must come BEFORE the per-visit evidence fetches"
    )


def test_expanding_history_reloads_so_thumbs_are_not_blank():
    # Because the photos are skipped while collapsed, unfolding without a
    # reload would show rows with permanently empty thumbnails.
    toggle = _HTML[_HTML.index('const b=$("histToggle")'):]
    toggle = toggle[:toggle.index("async function loadHistory")]
    assert "loadHistory()" in toggle
