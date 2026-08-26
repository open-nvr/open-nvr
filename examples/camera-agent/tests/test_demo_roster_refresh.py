# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The demo page's camera roster must be a live view, not a boot snapshot.

``loadCameras()`` used to run exactly once, at page load, and the /updates
socket carries tasks, monitors, reports, events and alarms but NOT cameras.
So:

* a camera ADDED in OpenNVR never appeared until someone reloaded the page;
* a camera DELETED in OpenNVR kept its strip tile forever (stuck on "no
  frame") and stayed selectable in the history, alarm, watch and task
  pickers — you could arm an alarm on a camera that no longer exists.

The agent already reconciles its own roster against OpenNVR on a ~30s
cadence (``_sync_camera_roster``); these guards pin the page to the same
cadence, and pin the four things a re-runnable ``loadCameras()`` has to get
right that a run-once one never had to: rebuild only on a real change,
release the frames it drops, keep the operator's selections, and never
stack a second listener or timer.

Same house style as the other demo-UI guards: ``demo/index.html`` is
vanilla no-build JS with no node/jsdom in the test env, so these are
whitespace-tolerant static assertions on the inline script.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEMO = Path(__file__).resolve().parent.parent / "demo" / "index.html"


@pytest.fixture(scope="module")
def script() -> str:
    html = _DEMO.read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)
    assert scripts, "demo/index.html has no inline <script> block"
    return "\n".join(scripts)


def _fn(script: str, name: str) -> str:
    """The source of one top-level ``function name(``, to its dedented close."""
    start = script.index(f"function {name}(")
    depth, i = 0, script.index("{", start)
    for j in range(i, len(script)):
        if script[j] == "{":
            depth += 1
        elif script[j] == "}":
            depth -= 1
            if depth == 0:
                return script[start:j + 1]
    raise AssertionError(f"unbalanced braces in {name}")


# ── the roster is polled at all ────────────────────────────────────────

def test_roster_is_refetched_on_the_agents_reconcile_cadence(script: str) -> None:
    wire = _fn(script, "wireRosterOnce")
    assert re.search(r"setInterval\(.*loadCameras\(\).*?,\s*30000\)", wire, re.S), \
        "no periodic /cameras poll — the roster is a boot snapshot again"
    assert 'document.visibilityState==="visible"' in wire, \
        "a hidden tab should not poll the roster"


def test_returning_to_the_tab_refreshes_the_roster(script: str) -> None:
    # 30s of staleness is fine while nobody is looking; it is not fine the
    # moment the operator comes back to the tab.
    wire = _fn(script, "wireRosterOnce")
    assert 'addEventListener("visibilitychange"' in wire
    assert "loadCameras()" in wire[wire.index("visibilitychange"):]


def test_a_failed_first_load_still_leaves_a_retry_timer(script: str) -> None:
    # Wiring after the fetch meant a /cameras that 401s or 503s at boot
    # (agent still starting) cost the page its retry timer for good.
    fn = _fn(script, "loadCameras")
    assert fn.index("wireRosterOnce()") < fn.index('fetch("/cameras")'), \
        "the poll must be armed before the first fetch can fail"


def test_the_roster_poll_is_armed_exactly_once(script: str) -> None:
    fn = _fn(script, "loadCameras")
    assert "if(!_rosterWired){ _rosterWired=true; wireRosterOnce(); }" in fn, \
        "every loadCameras() call would add another 30s timer"


# ── rebuild only when something actually moved ─────────────────────────

def test_an_unchanged_roster_does_not_rebuild_the_ui(script: str) -> None:
    """Rebuilding recreates every strip <img>. Doing that on each poll would
    flash all tiles and throw away frames fetched seconds earlier."""
    fn = _fn(script, "loadCameras")
    assert "const sig=JSON.stringify(" in fn
    assert "if(sig===_rosterSig) return;" in fn
    # The signature must cover the role too — the pickers render it, so a
    # rename that keeps the id still has to redraw.
    sig = fn[fn.index("const sig="):fn.index("if(sig===_rosterSig)")]
    assert "camera_id" in sig and "role" in sig


# ── what a rebuild must preserve ───────────────────────────────────────

def test_rebuild_keeps_the_operators_camera_scope(script: str) -> None:
    fn = _fn(script, "renderCamChecks")
    assert "filter(c=>c.checked)" in fn, "ticked boxes are not read before the wipe"
    assert "cb.checked=ticked.has(cam.camera_id)" in fn, \
        "a rebuild would silently widen a scoped question to every camera"


def test_rebuild_keeps_a_still_present_picker_choice(script: str) -> None:
    fn = _fn(script, "renderCamSelects")
    assert "const was=sel.value;" in fn
    assert "sel.value=[...sel.options].some(o=>o.value===was)?was:" in fn, \
        "the history/alarm/watch/task pickers reset themselves on every rebuild"


def test_a_deleted_camera_leaves_the_pickers(script: str) -> None:
    """The bug with teeth: cam1/cam2/cam3 deleted in OpenNVR stayed armable."""
    fn = _fn(script, "renderCamSelects")
    # Options are replaced, but the static first one ("all cameras" / "Any
    # camera") is markup, not roster — it must survive.
    assert "while(sel.options.length>1) sel.remove(1);" in fn
    # And a vanished selection falls back to that static option rather than
    # sitting on a stale id.
    assert "sel.options[0].value" in fn


def test_the_checkbox_row_replaces_only_its_own_labels(script: str) -> None:
    # "All cameras" is static markup inside #cambox; wiping the container
    # would delete it.
    fn = _fn(script, "renderCamChecks")
    assert 'camboxEl.innerHTML=""' not in fn
    assert 'camboxEl.querySelectorAll(".cam-cb")' in fn
    # useLocalCamera() appends its own .cam-cb label outside this function;
    # it has to be swept too, or it survives as a duplicate once /cameras
    # starts reporting that device.
    assert 'cb.closest("label")' in fn


# ── what a rebuild must release / must not stack ───────────────────────

def test_strip_rebuild_revokes_the_frames_it_drops(script: str) -> None:
    """A blob URL outlives the <img> that held it until it is revoked, so a
    site that adds and removes cameras would leak a JPEG per tile per
    rebuild. Unreachable before this change (the strip was never rebuilt)."""
    fn = _fn(script, "buildCamStrip")
    revoke = fn.index("URL.revokeObjectURL")
    assert revoke < fn.index('strip.innerHTML=""'), \
        "the tiles are dropped before their object URLs are released"
    assert "img[data-blob-url]" in fn[:revoke + 200]


def test_an_empty_roster_clears_the_strip(script: str) -> None:
    # The old guard bailed out before the wipe, so deleting the last camera
    # left its tile standing.
    fn = _fn(script, "buildCamStrip")
    assert 'if(!strip) return;' in fn
    assert fn.index('strip.innerHTML=""') < fn.index("if(!cams.length) return;"), \
        "an empty roster must clear the strip, not leave stale tiles"


def test_the_frame_poll_timer_is_not_restarted_per_rebuild(script: str) -> None:
    fn = _fn(script, "buildCamStrip")
    assert "setInterval" not in fn, \
        "each rebuild would add another 6s frame poll, multiplying ffmpegs"
    assert re.search(r"setInterval\(.*refreshCamStrip\(\).*?,\s*6000\)",
                     _fn(script, "wireRosterOnce"), re.S)


def test_a_rebuild_behind_the_camera_screen_fetches_no_frames(script: str) -> None:
    """The CPU guard. The strip is hidden while a camera's own screen is up,
    and every /frame miss is a fresh ffmpeg waiting for that camera's next
    keyframe. Priming the tiles on a roster change would spawn one per
    camera in the fleet for a strip nobody can see — on top of the live
    stream the screen is already pulling. closeCamScreen() refreshes on the
    way back, so the wait costs nothing."""
    fn = _fn(script, "buildCamStrip")
    assert "if(!window._camScreenCam) refreshCamStrip();" in fn, \
        "a roster change behind the camera screen would fetch the whole fleet"
    assert "refreshCamStrip()" in _fn(script, "closeCamScreen"), \
        "nothing refills the strip when the operator comes back"


def test_overlapping_roster_requests_collapse_to_one(script: str) -> None:
    # The 30s timer, visibilitychange and useLocalCamera() can all fire at
    # once; a hidden/shown tab should not queue a burst of /cameras.
    fn = _fn(script, "loadCameras")
    assert "if(_rosterBusy) return;" in fn
    assert "finally{ _rosterBusy=false; }" in fn


def test_a_forced_history_filter_reload_follows_the_picker(script: str) -> None:
    """Assigning .value fires no change event, so resetting the history
    filter to "all cameras" left the card showing the deleted camera's rows
    under a picker that claimed otherwise."""
    fn = _fn(script, "renderCamSelects")
    assert "if(sel===hist && sel.value!==was) loadHistory();" in fn
    # Only the history picker drives a fetch — the alarm/watch/task ones are
    # form fields, and reloading on those would be pointless work.
    assert fn.count("loadHistory()") == 1


def test_roster_listeners_are_not_stacked_per_rebuild(script: str) -> None:
    """The cam-all / cambox change handlers used to be attached inside
    loadCameras(); re-running it would fire them N times per click."""
    for name in ("loadCameras", "renderCamChecks", "renderCamSelects"):
        assert "addEventListener" not in _fn(script, name), name
    wire = _fn(script, "wireRosterOnce")
    assert 'all.addEventListener("change"' in wire
    assert 'camboxEl.addEventListener("change"' in wire


def test_a_tile_detached_mid_fetch_is_not_painted(script: str) -> None:
    """A rebuild can land while a /frame is in flight; painting the old tile
    mints a blob URL nothing can ever revoke."""
    fn = _fn(script, "_refreshTile")
    assert "if(!t.isConnected) return;" in fn
    assert fn.index("if(!t.isConnected) return;") < fn.index("setBlobSrc(img,blob)")


# ── the local-camera path joins the roster ─────────────────────────────

def test_adding_this_machines_camera_refreshes_the_roster(script: str) -> None:
    fn = _fn(script, "useLocalCamera")
    assert "loadCameras();" in fn, \
        "a local camera would wait up to 30s for its tile and picker entries"
