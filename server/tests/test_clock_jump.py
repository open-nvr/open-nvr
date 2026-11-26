# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Issue #1023: a step of the system clock while the stack runs.

Seen on a board with no RTC after NTP stepped it +50 min: core's cached
MediaMTX token was "expired" to MediaMTX for minutes (dashboard: media
server Down), and a stream connected before the step kept recording
segments dated 50 min in the past.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from core import short_lived_token
from core.short_lived_token import ShortLivedTokenCache
from services import clock_jump
from services.clock_jump import ClockStepDetector, watch_clock_jumps

# ---- token cache -----------------------------------------------------------


class _Clocks:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = 1_800_000_000.0


@pytest.fixture
def clocks(monkeypatch):
    c = _Clocks()
    monkeypatch.setattr(short_lived_token.time, "monotonic", lambda: c.mono)
    monkeypatch.setattr(short_lived_token.time, "time", lambda: c.wall)
    return c


def _minter():
    n = {"count": 0}

    def mint() -> str:
        n["count"] += 1
        return f"tok-{n['count']}"

    return mint, n


def test_token_is_reused_inside_the_window(clocks):
    cache = ShortLivedTokenCache(240.0)
    mint, n = _minter()
    assert cache.get(mint) == "tok-1"
    clocks.mono += 200
    clocks.wall += 200
    assert cache.get(mint) == "tok-1"
    assert n["count"] == 1


def test_token_is_reminted_when_the_window_ends(clocks):
    cache = ShortLivedTokenCache(240.0)
    mint, _ = _minter()
    cache.get(mint)
    clocks.mono += 241
    clocks.wall += 241
    assert cache.get(mint) == "tok-2"


def test_forward_wall_clock_jump_remints_at_once(clocks):
    """The incident: monotonic time says 30 s passed, the wall clock was
    stepped 50 min. The old token's exp is long gone for MediaMTX."""
    cache = ShortLivedTokenCache(240.0)
    mint, _ = _minter()
    cache.get(mint)
    clocks.mono += 30
    clocks.wall += 30 + 3000
    assert cache.get(mint) == "tok-2"


def test_backward_wall_clock_jump_remints_at_once(clocks):
    """A token issued "in the future" is not one to keep handing out."""
    cache = ShortLivedTokenCache(240.0)
    mint, _ = _minter()
    cache.get(mint)
    clocks.mono += 30
    clocks.wall -= 3000
    assert cache.get(mint) == "tok-2"


def test_a_failed_mint_propagates_and_caches_nothing(clocks):
    cache = ShortLivedTokenCache(240.0)

    def boom() -> str:
        raise RuntimeError("no signing key yet")

    with pytest.raises(RuntimeError):
        cache.get(boom)
    mint, _ = _minter()
    assert cache.get(mint) == "tok-1"


# ---- step detection ----------------------------------------------------------


def _offsets(*values: float):
    it = iter(values)

    def offset() -> float:
        try:
            return next(it)
        except StopIteration:
            raise _DoneError from None

    return offset


class _DoneError(Exception):
    pass


def test_detector_reports_steps_and_absorbs_drift():
    d = ClockStepDetector(_offsets(100.0, 100.001, 100.002, 3100.002, 3100.003))
    assert d.step() == pytest.approx(0.001)
    assert d.step() == pytest.approx(0.001)
    assert d.step() == pytest.approx(3000.0)
    assert d.step() == pytest.approx(0.001)


def _watch(offsets, **kw) -> list[float]:
    jumps: list[float] = []
    det = ClockStepDetector(_offsets(*offsets))
    with pytest.raises(_DoneError):
        asyncio.run(watch_clock_jumps(
            detector=det, interval_s=0, settle_s=0, on_jump=jumps.append, **kw))
    return jumps


def test_watch_ignores_ordinary_drift():
    assert _watch([0.0, 0.01, 0.02, 0.5, 0.6]) == []


def test_watch_reports_one_jump_for_a_burst():
    """Stepped +3000 s, then corrected another +2 s during the settle:
    one reconnect, for the whole step."""
    assert _watch([0.0, 0.0, 3000.0, 3002.0, 3002.0]) == [pytest.approx(3002.0)]


def test_watch_reports_backward_jumps():
    assert _watch([0.0, -600.0, -600.0]) == [pytest.approx(-600.0)]


def test_watch_ignores_a_step_undone_within_the_settle():
    assert _watch([0.0, 3000.0, 0.0, 0.0]) == []


# ---- reconnecting recording streams ------------------------------------------


@pytest.fixture
def mediamtx(monkeypatch):
    """A fake MediaMTX Control API holding a few paths."""
    from services import mediamtx_admin_service as mod
    from services.mediamtx_admin_service import MediaMtxAdminService

    monkeypatch.setattr(mod.settings, "mediamtx_admin_api", "http://mediamtx:9997/v3")
    monkeypatch.setattr(mod.settings, "mediamtx_stream_prefix", "cam-")
    monkeypatch.setattr(MediaMtxAdminService, "_admin_token", classmethod(lambda cls: "t"))

    state = {
        "paths": {
            "cam-1": {"name": "cam-1", "source": "rtsp://a/1", "record": True},
            "cam-1-sub": {"name": "cam-1-sub", "source": "rtsp://a/1s", "record": False},
            "cam-2": {"name": "cam-2", "source": "rtsp://a/2", "record": True,
                      "recordSegmentDuration": "1m"},
            "__startup__": {"name": "__startup__", "source": "publisher", "record": True},
        },
        "calls": [],
        "fail_add": set(),
    }

    def handle(req: httpx.Request) -> httpx.Response:
        path = req.url.path.removeprefix("/v3")
        state["calls"].append((req.method, path))
        if req.method == "GET" and path == "/config/paths/list":
            return httpx.Response(200, json={
                "pageCount": 1, "items": list(state["paths"].values())})
        name = path.rsplit("/", 1)[-1]
        if req.method == "DELETE" and path.startswith("/config/paths/delete/"):
            if state["paths"].pop(name, None) is None:
                return httpx.Response(404, json={"error": "path not found"})
            return httpx.Response(200)
        if req.method == "POST" and path.startswith("/config/paths/add/"):
            if name in state["fail_add"]:
                return httpx.Response(500, json={"error": "boom"})
            if name in state["paths"]:
                return httpx.Response(400, json={"error": "path already exists"})
            state["paths"][name] = json.loads(req.content)
            return httpx.Response(200)
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        mod.httpx, "AsyncClient",
        lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handle)}))
    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    return state


async def _no_sleep(_s: float) -> None:
    return None


def _restart():
    from services.mediamtx_admin_service import MediaMtxAdminService

    return asyncio.run(MediaMtxAdminService.restart_recording_paths())


def test_restart_reconnects_recording_camera_paths_with_their_own_config(mediamtx):
    before = {k: dict(v) for k, v in mediamtx["paths"].items()}
    result = _restart()

    assert result == {"restarted": ["cam-1", "cam-2"], "failed": []}
    # Each recording camera path removed and put back unchanged.
    assert mediamtx["paths"] == before
    touched = [c for c in mediamtx["calls"] if c[0] != "GET"]
    assert touched == [
        ("DELETE", "/config/paths/delete/cam-1"), ("POST", "/config/paths/add/cam-1"),
        ("DELETE", "/config/paths/delete/cam-2"), ("POST", "/config/paths/add/cam-2"),
    ]


def test_restart_leaves_paused_and_non_camera_paths_alone(mediamtx):
    """record: false (live-only sub, paused camera) and paths outside the
    camera prefix are not reconnected."""
    mediamtx["paths"]["cam-2"]["record"] = False
    result = _restart()
    assert result["restarted"] == ["cam-1"]
    assert mediamtx["paths"]["cam-2"]["record"] is False


def test_restart_reports_a_path_it_could_not_put_back(mediamtx):
    mediamtx["fail_add"].add("cam-2")
    result = _restart()
    assert result == {"restarted": ["cam-1"], "failed": ["cam-2"]}
    adds = [c for c in mediamtx["calls"] if c == ("POST", "/config/paths/add/cam-2")]
    assert len(adds) == 3


def test_handle_clock_jump_restarts_and_audits(monkeypatch, mediamtx):
    audited = []
    monkeypatch.setattr(clock_jump, "_audit", lambda d, r: audited.append((d, r)))
    result = asyncio.run(clock_jump.handle_clock_jump(3000.0))
    assert result["restarted"] == ["cam-1", "cam-2"]
    assert audited == [(3000.0, result)]


# ---- what the dashboard's Clock chip reads ------------------------------------


def test_sync_status_is_unknown_off_linux(monkeypatch):
    monkeypatch.setattr(clock_jump.sys, "platform", "win32")
    assert clock_jump.system_clock_synchronized() is None


@pytest.mark.parametrize("release", ["6.10.14-linuxkit", "5.15.167.4-microsoft-standard-WSL2"])
def test_sync_status_is_unknown_where_the_host_os_sets_the_clock(monkeypatch, release):
    """Docker Desktop and WSL2 VMs never get STA_UNSYNC cleared: a warning
    there would be permanent and wrong."""
    monkeypatch.setattr(clock_jump.sys, "platform", "linux")
    monkeypatch.setattr(clock_jump.platform, "release", lambda: release)
    assert clock_jump.system_clock_synchronized() is None


@pytest.mark.parametrize(("status", "synced"), [(0x2001, True), (0x0041, False)])
def test_sync_status_reads_the_kernel_unsync_flag(monkeypatch, status, synced):
    monkeypatch.setattr(clock_jump.sys, "platform", "linux")
    monkeypatch.setattr(clock_jump.platform, "release", lambda: "6.8.0-1015-qcom")

    class _Libc:
        def adjtimex(self, ref):
            ref._obj.status = status
            return 0

    monkeypatch.setattr(clock_jump.ctypes, "CDLL", lambda *a, **kw: _Libc())
    assert clock_jump.system_clock_synchronized() is synced


def test_a_failing_adjtimex_is_unknown(monkeypatch):
    monkeypatch.setattr(clock_jump.sys, "platform", "linux")
    monkeypatch.setattr(clock_jump.platform, "release", lambda: "6.8.0")

    class _Libc:
        def adjtimex(self, ref):
            return -1

    monkeypatch.setattr(clock_jump.ctypes, "CDLL", lambda *a, **kw: _Libc())
    assert clock_jump.system_clock_synchronized() is None


def test_clock_status_carries_the_last_jump(monkeypatch, mediamtx):
    monkeypatch.setattr(clock_jump, "_audit", lambda d, r: None)
    monkeypatch.setattr(clock_jump, "_last_jump", None)
    monkeypatch.setattr(clock_jump, "system_clock_synchronized", lambda: True)
    assert clock_jump.clock_status() == {"synchronized": True, "last_jump": None}

    asyncio.run(clock_jump.handle_clock_jump(-600.0))
    jump = clock_jump.clock_status()["last_jump"]
    assert jump["delta_seconds"] == -600.0
    assert jump["streams_restarted"] == 2 and jump["streams_failed"] == 0
    assert jump["at"]
