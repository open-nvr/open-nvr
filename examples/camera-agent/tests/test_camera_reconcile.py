# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Camera deletions in OpenNVR must reach the agent — and roster changes
must re-bake the LLM's tool enums.

Field bug: cam1-3 were deleted in OpenNVR but the agent's demo kept
listing them ("no frame" tiles, still offered in tool enums). The server
endpoint was already correct (it only serves is_active cameras); the
agent's reconcile loop was ADD-ONLY and never removed anything. A second
latent bug rode along: cameras ADDED by reconcile after boot never
rebuilt _camera_ids, so the tool schemas' camera enums went stale in the
other direction too (the LLM literally couldn't name a late-added
camera).

Safety invariants pinned here:
- only NVR-managed cameras (seen in a successful fetch, or boot-loaded
  from the NVR) are ever removed — config-file cameras never;
- a FAILED fetch changes nothing (failure ≠ empty roster);
- removal keeps the camera's event history (bounded ring) — its past is
  still history;
- add AND remove both re-bake tool enums + the roster id list.
"""
from __future__ import annotations

import time

import camera_agent as ca
from camera_agent import AppConfig, CameraAgentRuntime
from context import CameraContext, CameraSpec, EventRecord


def _spec(cid: str) -> CameraSpec:
    return CameraSpec(camera_id=cid, frame_url=f"http://x/{cid}.jpg", role=cid)


def _runtime(cameras=(), source="config"):
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    text_mode=True, cameras=[_spec(c) for c in cameras],
                    cameras_source=source,
                    opennvr_cameras_url="http://core:8000/api/v1/internal/camera-agent/cameras")
    return CameraAgentRuntime(cfg)


def _enum_ids(rt) -> set[str]:
    for t in rt.tool_definitions:
        if t["function"]["name"] == "describe_camera":
            props = t["function"]["parameters"]["properties"]
            return set(props["camera_id"]["enum"]) - {"all"}
    raise AssertionError("describe_camera not advertised")


# ── CameraContext.remove_camera ─────────────────────────────────────────

def test_context_remove_camera_forgets_spec_and_frames():
    ctx = CameraContext(cameras=[_spec("cam1"), _spec("cam2")])
    assert ctx.remove_camera("cam1") is True
    assert not ctx.known_camera("cam1")
    assert ctx.known_camera("cam2")
    assert ctx.remove_camera("cam1") is False   # already gone — no error


def test_context_remove_camera_keeps_event_history():
    ctx = CameraContext(cameras=[_spec("cam1")])
    ctx.record_event(EventRecord(received_at=time.time(), camera_id="cam1",
                                 adapter="tier0", summary="person"))
    ctx.remove_camera("cam1")
    events = ctx.recent_events(camera_id="cam1", window_seconds=60)
    assert len(events) == 1 and events[0].summary == "person"


# ── provenance seeding ──────────────────────────────────────────────────

def test_opennvr_boot_roster_is_managed():
    rt = _runtime(["cam1", "cam2"], source="opennvr")
    assert rt._opennvr_managed == {"cam1", "cam2"}


def test_config_boot_roster_is_not_managed():
    rt = _runtime(["cam1"], source="config")
    assert rt._opennvr_managed == set()


# ── removal safety rails ────────────────────────────────────────────────

def test_config_camera_never_removed_even_if_passed():
    rt = _runtime(["cfgcam"], source="config")
    removed = rt._remove_opennvr_cameras({"cfgcam"})
    assert removed == []
    assert rt.context.known_camera("cfgcam")


def test_deleted_nvr_camera_is_removed_everywhere():
    rt = _runtime([], source="config")
    rt._register_opennvr_cameras([_spec("cam1"), _spec("cam4")])
    rt._opennvr_managed |= {"cam1", "cam4"}
    rt._sync_camera_roster()
    assert _enum_ids(rt) == {"cam1", "cam4"}

    # OpenNVR now only lists cam4 → cam1 was deleted there.
    removed = rt._remove_opennvr_cameras({"cam1"})
    rt._sync_camera_roster()
    assert removed == ["cam1"]
    assert not rt.context.known_camera("cam1")
    assert [c.camera_id for c in rt.cfg.cameras] == ["cam4"]
    assert _enum_ids(rt) == {"cam4"}
    assert "cam1" not in rt._opennvr_managed


def test_added_camera_enters_tool_enums_after_sync():
    # The latent add-side bug: reconcile added cameras but never re-baked
    # the enums, so the LLM couldn't name them.
    rt = _runtime(["cam1"], source="opennvr")
    assert _enum_ids(rt) == {"cam1"}
    rt._register_opennvr_cameras([_spec("cam5")])
    rt._sync_camera_roster()
    assert _enum_ids(rt) == {"cam1", "cam5"}


def test_mixed_sources_only_nvr_cameras_removed():
    rt = _runtime(["cfgcam"], source="config")
    rt._register_opennvr_cameras([_spec("cam2")])
    rt._opennvr_managed |= {"cam2"}
    # NVR fetch now empty: cam2 goes, the config camera stays.
    removed = rt._remove_opennvr_cameras(rt._opennvr_managed - set())
    assert removed == ["cam2"]
    assert rt.context.known_camera("cfgcam")


# ── reconcile loop: failure ≠ empty ─────────────────────────────────────

def test_reconcile_failed_fetch_removes_nothing(monkeypatch):
    import asyncio

    rt = _runtime(["cam1", "cam2"], source="opennvr")

    calls = {"n": 0}

    def _boom(**kwargs):
        calls["n"] += 1
        rt._stop_event.set()          # one failing cycle, then stop
        raise RuntimeError("core unreachable")

    monkeypatch.setattr(ca, "_load_opennvr_cameras", _boom)
    asyncio.run(rt._reconcile_opennvr_cameras())
    assert calls["n"] == 1
    assert rt.context.known_camera("cam1") and rt.context.known_camera("cam2")
    assert rt._opennvr_managed == {"cam1", "cam2"}


def test_reconcile_successful_empty_fetch_removes_managed(monkeypatch):
    import asyncio

    rt = _runtime(["cam1"], source="opennvr")

    def _empty(**kwargs):
        rt._stop_event.set()
        return []                     # SUCCESSFUL fetch: NVR has no cameras

    monkeypatch.setattr(ca, "_load_opennvr_cameras", _empty)
    asyncio.run(rt._reconcile_opennvr_cameras())
    assert not rt.context.known_camera("cam1")
    assert rt.cfg.cameras == []


def test_reconcile_successful_fetch_adds_and_removes(monkeypatch):
    import asyncio

    rt = _runtime(["cam1", "cam2"], source="opennvr")

    def _fetch(**kwargs):
        rt._stop_event.set()
        return [_spec("cam2"), _spec("cam9")]   # cam1 deleted, cam9 new

    monkeypatch.setattr(ca, "_load_opennvr_cameras", _fetch)
    asyncio.run(rt._reconcile_opennvr_cameras())
    assert not rt.context.known_camera("cam1")
    assert rt.context.known_camera("cam9")
    assert _enum_ids(rt) == {"cam2", "cam9"}
    assert rt._opennvr_managed == {"cam2", "cam9"}


# ── the camera-id map must not freeze at boot either ────────────────
#
# Same class as the tool-enum staleness above, one layer down. The agent
# resolved "cam1" -> the OpenNVR camera id through a dict comprehension
# captured in a closure at startup. The reconcile loop exists BECAUSE the
# agent usually boots before the core has cameras ready — so on a normal cold
# start that snapshot was empty and stayed empty for the life of the process,
# even while reconcile logged "loaded 1 camera(s)" every 30s.
#
# Field symptom: every past-tense question returned "The camera appears to be
# offline." search_history got ERROR: camera 'cam1' has no server-side id, and
# the LLM turned that into an offline claim about a camera that was online the
# whole time. camera_snapshot and describe_camera's best-frame fetch resolve
# through the same function and broke identically.


def _nvr_spec(cid: str, oid: int) -> CameraSpec:
    return CameraSpec(camera_id=cid, frame_url=f"http://x/{cid}.jpg", role=cid,
                      opennvr_camera_id=oid)


def test_camera_added_after_boot_resolves_to_its_server_side_id():
    rt = _runtime()                              # boots with NO cameras
    assert rt.tools._resolve_camera("cam1") == "cam1"   # nothing to resolve yet

    rt._register_opennvr_cameras([_nvr_spec("cam1", 1)])
    rt._sync_camera_roster()

    assert rt.tools._resolve_camera("cam1") == "1", (
        "resolver froze the boot-time roster; history/snapshot stay broken "
        "for the life of the process"
    )


def test_resolver_follows_a_removed_camera_too():
    rt = _runtime()
    rt._register_opennvr_cameras([_nvr_spec("cam1", 1), _nvr_spec("cam2", 2)])
    rt._opennvr_managed |= {"cam1", "cam2"}      # what the reconcile loop adopts
    rt._sync_camera_roster()
    assert rt.tools._resolve_camera("cam2") == "2"

    rt._remove_opennvr_cameras({"cam2"})
    rt._sync_camera_roster()
    assert rt.tools._resolve_camera("cam2") == "cam2"   # unknown again
    assert rt.tools._resolve_camera("cam1") == "1"      # survivor unaffected
