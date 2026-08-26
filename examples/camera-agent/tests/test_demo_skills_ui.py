# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Static guard on the demo Skills panel wiring (no build system / no node).

The demo (``demo/index.html``) is vanilla no-build JS, so its render path
is only covered indirectly (payload tests + ``node --check`` on the inline
script). This test parses the inline ``<script>`` statically and asserts the
skills panel's load/render handlers and the payload fields they consume are
still wired — so a future edit that drops the ``+`` button, the greyed-skill
on-ramp (``suggested_adapters`` / ``suggested_apps`` / the enable links), or
the app-skill rendering fails a test instead of silently regressing the UI.

It is deliberately whitespace-insensitive (checks for substrings/symbols,
not exact formatting) so cosmetic edits don't make it brittle.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_DEMO = Path(__file__).resolve().parent.parent / "demo" / "index.html"


def _inline_script() -> str:
    html = _DEMO.read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S | re.I)
    assert scripts, "demo/index.html has no inline <script> block"
    return "\n".join(scripts)


@pytest.fixture(scope="module")
def script() -> str:
    return _inline_script()


@pytest.fixture(scope="module")
def html() -> str:
    return _DEMO.read_text(encoding="utf-8")


def test_skill_render_handlers_present(script: str) -> None:
    # The three functions that load the panel and render the enabled list +
    # the add-list. Dropping any of them breaks the panel.
    for fn in ("function loadSkills", "function renderSkills", "function renderBrowse"):
        assert fn in script, f"demo skills handler missing: {fn!r}"


def test_skill_panel_element_ids_present(html: str) -> None:
    # The card, the '+' add button, the enabled list, and the browse/add list.
    for eid in ("skillsCard", "skillAdd", "skillsList", "skillBrowseList"):
        assert f'id="{eid}"' in html, f"demo skills element id missing: {eid!r}"


def test_greyed_skill_onramp_fields_consumed(script: str) -> None:
    # The greyed-skill install on-ramp must render both the adapter and the
    # app suggestions, each with its deep-link. If a refactor drops any of
    # these field references, the on-ramp silently disappears.
    for field in (
        "suggested_adapters",
        "suggested_apps",
        "enable_url",       # adapter deep-link
        "app_enable_url",   # app deep-link
    ):
        assert field in script, f"demo no longer consumes greyed-skill field: {field!r}"


def test_app_onramp_labeling_present(script: str) -> None:
    # The greyed-skill app on-ramp turns a suggested_apps id into a readable
    # name (APP_LABELS / appLabel) and deep-links via app_enable_url. Dropping
    # this collapses the "or install the <App> app" path back to a dead end.
    assert "APP_LABELS" in script and "appLabel" in script, (
        "demo no longer maps suggested_apps ids to readable app names"
    )


def test_enable_links_are_guide_only(script: str) -> None:
    # The install links are navigation-only anchors — assert the new-tab
    # anchor pattern is present and (governance boundary, at the UI layer)
    # the skills UI never POSTs to an app enable/disable/config route.
    assert 'target="_blank"' in script, "enable links should open in a new tab"
    assert not re.search(
        r"fetch\([^)]*apps/[^)]*/(enable|disable|config)", script
    ), "skills UI must not POST to an app enable/disable/config route"


def test_core_skill_disable_asks_first(script: str) -> None:
    # Removing a core task-shaped skill (alarm/watch/report/task) takes a
    # whole rail panel's capability away, so the ✕ must confirm before
    # calling the disable endpoint. If the confirm gate or the core map
    # goes missing, a stray click silently de-tools the agent.
    assert "CORE_SKILLS" in script, "core-skill map missing"
    for sid in ("alarm", "watch", "report", "task"):
        assert re.search(rf'CORE_SKILLS\s*=\s*{{[^}}]*\b{sid}\b', script), (
            f"core-skill map lost entry {sid!r}"
        )
    assert re.search(r"CORE_SKILLS\[s\.id\][^\n]*confirm", script), (
        "disable path no longer confirms before removing a core skill"
    )


def test_restore_defaults_wired(script: str, html: str) -> None:
    # The Skills header's "Restore defaults" is the one-click undo for an
    # over-pruned agent: the button must exist, call the restore endpoint,
    # and only show when something is actually restorable.
    assert 'id="skillRestore"' in html, "restore-defaults button missing"
    assert "function restoreSkills" in script, "restoreSkills handler missing"
    assert '"/skills/restore"' in script, "restore endpoint call missing"
    assert re.search(r'skillRestore[\s\S]{0,200}hidden\s*=\s*!_skills\.some', script), (
        "restore button visibility no longer keyed to restorable skills"
    )


def test_watch_add_form_wired(script: str, html: str) -> None:
    # The Watching panel's + form (notify/count watches via POST /monitors).
    # Crossing is deliberately absent — placing the line is a conversation,
    # not a text field — so the form must offer exactly the two typed kinds.
    for eid in ("watchAdd", "watchForm", "watchKind", "watchTarget", "watchStart"):
        assert f'id="{eid}"' in html, f"watch form element missing: {eid!r}"
    for kind in ('value="notify"', 'value="count"'):
        assert kind in html, f"watch kind option missing: {kind}"
    assert 'value="crossing"' not in html, (
        "crossing must stay chat-only (needs the agent to place the line)"
    )
    assert '"/monitors"' in script and "cameraParam()" in script, (
        "watch form no longer POSTs /monitors with the camera selection"
    )


def test_report_add_form_wired(script: str, html: str) -> None:
    # The Scheduled-reports panel's + form (POST /reports). One schedule per
    # report: every-N-minutes wins over the daily time; neither → the
    # server's 08:00-daily default.
    for eid in ("reportAdd", "reportForm", "reportName", "reportQuery",
                "reportAt", "reportEvery", "reportCreate"):
        assert f'id="{eid}"' in html, f"report form element missing: {eid!r}"
    assert '"/reports"' in script, "report form no longer POSTs /reports"
    assert re.search(r"every_minutes\s*=\s*every[\s\S]{0,80}reportAt", script), (
        "interval-beats-daily precedence lost in the report form"
    )


def test_hardware_panel_wired(script: str, html: str) -> None:
    # The Hardware card: collapsed summary + expandable per-skill breakdown,
    # fed by GET /hardware, recomputed whenever the toolset changes (both
    # the single-skill toggle and restore-defaults paths).
    for eid in ("hwCard", "hwToggle", "hwSummary", "hwDetail"):
        assert f'id="{eid}"' in html, f"hardware panel element missing: {eid!r}"
    assert "function loadHardware" in script, "loadHardware handler missing"
    assert '"/hardware"' in script, "hardware endpoint call missing"
    assert script.count("loadHardware();") >= 3, (
        "hardware panel no longer recomputes on initial load + skill changes"
    )


def test_camera_strip_peek_and_camera_screen_wired(script: str, html: str) -> None:
    # tap = peek (agent bubble with the live frame + Open/Pin actions),
    # ⤤/name = the camera's own screen at /demo/camera/{id} with the
    # review player. The pin stays one-shot on the next /ask.
    for eid in ("camstrip", "pinChip", "pinClear", "mainScreen", "camScreen",
                "camPlayer", "camImg", "camScrub", "camMarks", "camLive",
                # camAsk was retired: the chat bar is docked below the camera
                # screen and cameraParam() scopes typed questions already.
                "camPlay", "camTalk", "camWatch", "camAlarm",
                "popWatch", "popAlarm", "camRecordings", "camBack"):
        assert f'id="{eid}"' in html, f"camera UI element missing: {eid!r}"
    for fn in ("function buildCamStrip", "async function peekCam",
               "function openCamScreen", "function closeCamScreen",
               "async function loadTimeline", "async function reviewAt",
               "function goLive"):
        assert fn in script, f"camera screen handler missing: {fn!r}"
    assert '"/timeline/"' in script, "player no longer loads /timeline"
    assert "?at=" in script, "review scrub no longer fetches /frame?at="
    assert "history.pushState" in script and "/demo/camera/" in script, (
        "camera screen lost its URL (pushState /demo/camera/{id})"
    )
    assert "pinned_jpeg_b64" in script, "ask() no longer sends the pinned frame"
    assert re.search(r"if\(_pinnedFrame\)[\s\S]{0,120}clearPin\(\)", script), (
        "pin is no longer one-shot (must clear after the send)"
    )
    # scoping: on a camera screen every ask/talk goes to that camera
    assert "_camScreenCam" in script and "function cameraParam" in script


def test_all_css_variables_are_defined(html: str) -> None:
    # The camera-screen popover shipped transparent because its CSS used
    # the MAIN app's token names (--bg-1/--text-dim), which this page
    # never defines — var() then resolves to nothing. Guard: every
    # var(--x) used without a fallback must be defined in :root.
    defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", html))
    used_no_fallback = set(re.findall(r"var\((--[a-z0-9-]+)\)", html))
    missing = sorted(used_no_fallback - defined)
    assert not missing, f"CSS variables used but never defined: {missing}"


def test_auth_login_overlay_wired(script: str, html: str) -> None:
    # auth_mode="opennvr": one fetch wrapper attaches the bearer token to
    # every same-origin call, tries ONE silent refresh on a 401, then
    # raises the login overlay (which proxies OpenNVR's login).
    for eid in ("loginOverlay", "loginUser", "loginPass", "loginTotp",
                "loginErr", "loginGo"):
        assert f'id="{eid}"' in html, f"login element missing: {eid!r}"
    assert "window.fetch=" in script.replace(" ", ""), "fetch wrapper missing"
    assert '"/auth/login"' in script and '"/auth/refresh"' in script
    assert "function showLogin" in script
    assert "sessionStorage" in script, "tokens must survive reload (mobile-parity contract)"


def test_recorded_row_wired(script: str, html: str) -> None:
    # The camera screen's Recorded row: server-side segments, direct-URL
    # playback in the player's <video>, ● LIVE returns. The old ctop
    # Recordings link is DEMOTED into this row.
    for eid in ("recRow", "recDay", "recSegs", "camVideo", "camRecordings"):
        assert f'id="{eid}"' in html, f"recorded-row element missing: {eid!r}"
    for fn in ("async function loadRecordings", "function renderSegs",
               "async function playRecording", "function stopPlayback"):
        assert fn in script, f"recorded-row handler missing: {fn!r}"
    assert '"/recordings/"' in script, "row no longer calls the agent proxy"
    assert '"PLAYBACK"' in script, "player lost its PLAYBACK mode label"
    # goLive must tear playback down — a lingering <video> under a LIVE
    # pill is the same class of bug as the frozen-pause finding.
    assert re.search(r"function goLive\(\)\{[^\n]*stopPlayback\(\)", script)


def test_events_feed_wired(script: str, html: str) -> None:
    # The Events card leads the rail (operational truth before Skills),
    # a per-camera filter rides the camera screen, and clicking an event
    # jumps to THE MOMENT: review ring first, covering recorded segment
    # second, live as the fallback.
    assert 'id="eventsCard"' in html and 'id="eventsList"' in html
    assert html.index('id="eventsCard"') < html.index('id="skillsCard"'), (
        "Events must lead the rail — what happened beats configuration"
    )
    assert 'id="evToggle"' in html and "evCollapsed" in script, (
        "Events card must be collapsible (it was eating the rail)"
    )
    # one home for events: the rail card scopes to the focused camera
    # (no left-column list — that was reported as noise)
    assert 'id="camEvents"' not in html
    assert 'id="evScope"' in html
    for fn in ("async function loadEvents", "function renderEvents",
               "function renderEventsCard", "async function openCamAtMoment"):
        assert fn in script, f"events handler missing: {fn!r}"
    assert '"/events"' in script
    assert re.search(r"openCamAtMoment[\s\S]{0,400}loadTimeline", script), (
        "click-to-moment no longer tries the review ring first"
    )


def test_alarm_ring_levels_wired(script: str, html: str) -> None:
    # Annunciation levels: chime dings once (amber flash, no latch),
    # siren latches until silenced, silent pushes only. Both alarm forms
    # offer the choice; fire-grade targets preselect the siren.
    assert 'id="alarmRing"' in html and 'id="paRing"' in html
    for v in ('value="chime"', 'value="pulse"', 'value="siren"', 'value="silent"'):
        assert html.count(v) >= 2, f"ring option missing from a form: {v}"
    assert "function startPulse" in script, "urgent level lost its own sound"
    assert "ringing_kind" in script, "page no longer picks the sound by kind"
    assert "_ringDefaults" in script, "preselect no longer follows the site map"
    assert "function wireRingPreselect" in script
    assert "flashChime" in script and 'playChime("bell")' in script
    assert ".alarmbar.chime" in html, "chime banner style missing"
    # Presets are server-rendered now (GET /alarm-presets, honest
    # availability); the siren grade rides in each preset's payload and
    # the click handler forwards it into the arm body.
    assert "/alarm-presets" in script, "presets no longer server-driven"
    assert "body.ring=p.ring" in script.replace(" ", ""), (
        "preset click no longer forwards the ring grade")


def test_ring_defaults_editor_wired(script: str, html: str) -> None:
    # ⚙ in the Alarms header opens the site alert-defaults editor:
    # override rows (target + level), inherited entries shown read-only,
    # save PUTs /alarm-defaults and refreshes the live preselect map.
    for eid in ("ringCfg", "ringCfgForm", "ringRows", "ringAddRow",
                "ringSave", "ringInherited"):
        assert f'id="{eid}"' in html, f"ring editor element missing: {eid!r}"
    assert '"/alarm-defaults"' in script
    assert re.search(r'method:"PUT"[\s\S]{0,120}overrides', script)
    assert "window._ringDefaults=d.defaults" in script.replace(" ", ""), (
        "saving must refresh the live preselect map"
    )


def test_webrtc_live_player_wired(script: str, html: str) -> None:
    # The laggy-stream fix: the camera screen plays the SAME WebRTC
    # (WHEP) stream the OpenNVR Live view does; snapshot polling is the
    # labelled fallback, and every exit path tears the session down.
    for fn in ("async function tryWebrtcLive", "function stopWebrtc"):
        assert fn in script, f"webrtc handler missing: {fn!r}"
    assert "RTCPeerConnection" in script and '"application/sdp"' in script
    assert '"/streams/"' in script, "player no longer asks the agent proxy"
    assert "Live preview — up to 1 frame/s" in script, (
        "the stills fallback lost its honest label"
    )
    # review, recorded playback, and leaving the screen all stop the stream
    assert script.count("stopWebrtc()") >= 4


def test_rail_pollers_gate_on_auth(script: str) -> None:
    # With auth on, the rail must not spam 401s behind the login overlay:
    # every gated poller short-circuits until a token is held.
    assert "function authReady()" in script, "authReady gate missing"
    assert '_authMode!=="opennvr"||!!_token' in script.replace(" ", ""), (
        "authReady must pass when auth is off OR a token is held"
    )
    for fn in ("pollAlarms", "pollTasks", "pollMonitors", "pollReports",
               "loadEvents", "refreshCamStrip"):
        body = script.split(f"function {fn}(", 1)[1][:120]
        assert "authReady()" in body, f"{fn} is not gated on authReady"


def test_login_error_shows_message_not_object(script: str) -> None:
    # OpenNVR nests the error as {detail:{error,message,...}}; rendering
    # d.detail directly printed "[object Object]". errText pulls the message.
    assert "function errText" in script, "login error helper missing"
    assert "errText(d,r.status)" in script, "login no longer uses errText"
    assert 'typeof dt==="object"' in script, "errText must handle a nested detail object"


def test_live_video_pauses_when_tab_hidden(script: str) -> None:
    # A live WebRTC decode competes with the on-box STT/LLM/TTS. When the tab
    # is backgrounded the stream is torn down, and re-established on return to
    # a live camera screen — so no decode burns CPU while nobody's watching.
    assert 'addEventListener("visibilitychange"' in script, (
        "no visibilitychange handler — live decode keeps running in a hidden tab"
    )
    assert re.search(r"document\.hidden[\s\S]{0,120}stopWebrtc\(\)", script), (
        "hidden tab should stop the WebRTC stream"
    )
    assert re.search(r"document\.hidden[\s\S]{0,200}goLive\(\)", script), (
        "returning to a live camera screen should re-establish the stream"
    )


# ── camera widget: strip concurrency + honest WebRTC drop ──────────────

def _demo_html() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "demo" / "index.html").read_text()


def test_camera_strip_refreshes_cameras_concurrently():
    """The strip must not serialise cameras.

    Every /frame miss is a fresh ffmpeg waiting for the camera's next
    KEYFRAME (1-4s on long-GOP OEM cameras). Awaiting them one at a time
    made the last tile land ~N x that behind the first, and the server was
    built for the opposite: context.get_frame takes a PER-CAMERA lock so
    different cameras proceed in parallel while callers on the SAME camera
    coalesce. Measured in-browser at 4 cameras x 1.2s: 4809ms -> 1211ms.
    """
    html = _demo_html()
    assert "_STRIP_LANES" in html
    assert "Promise.all(" in html[html.index("async function refreshCamStrip"):]
    # The old shape: a for-loop awaiting each tile inline.
    body = html[html.index("async function refreshCamStrip"):]
    body = body[:body.index("async function _refreshTile")]
    assert "for(const t of document.querySelectorAll" not in body


def test_camera_strip_cycles_cannot_overlap():
    """A refresh slower than the 6s interval used to stack a second cycle on
    top of the first — two loops racing, double the ffmpeg processes.
    Measured in-browser: 11 fetches for 4 cameras, now 4."""
    html = _demo_html()
    assert "_stripBusy" in html
    body = html[html.index("async function refreshCamStrip"):]
    assert "if(!authReady() || _stripBusy) return;" in body
    assert "finally { _stripBusy = false; }" in body


def test_a_dropped_webrtc_stream_stops_claiming_to_be_live():
    """A WHEP session dying after connect was invisible: _pc stayed non-null
    (gating the still-poller OFF) and the caption still said 'real-time
    stream', so the player showed a FROZEN frame under a green LIVE pill.
    """
    html = _demo_html()
    assert "function handleWebrtcState" in html
    assert "pc.onconnectionstatechange=()=>handleWebrtcState(pc);" in html
    fn = html[html.index("function handleWebrtcState"):]
    fn = fn[:fn.index("function stopWebrtc")]
    # 'failed' and 'closed' are terminal and act at once. 'disconnected' is
    # NOT — see test_a_transient_ice_blip_does_not_demote_to_stills.
    assert 'st==="failed"||st==="closed"' in fn
    # It must clear the session (stills resume) AND correct the caption.
    assert "stopWebrtc();" in fn
    assert "real-time stream dropped" in fn
    # And it must ignore a peer that has already been superseded.
    assert "if(_pc!==pc) return;" in fn


def test_a_transient_ice_blip_does_not_demote_to_stills():
    """'disconnected' is not 'failed'.

    ICE recovers from 'disconnected' on its own — a Wi-Fi roam or a brief
    reroute trips it for a second or two. Treating it as terminal tore down
    the WHEP session (DELETEing the resource), and because the agent never
    retries, a two-second blip left the operator on 1 fps stills until they
    left the camera screen and came back. The core player (app/src/
    components/VideoPlayer/VideoPlayer.tsx) gives ICE a 5s grace; the agent
    now matches it.
    """
    html = _demo_html()
    fn = html[html.index("function handleWebrtcState"):html.index("function whepDropped")]
    assert 'st==="disconnected"' in fn
    # The load-bearing assertion: everything the blip branch does BEFORE it
    # arms the timer must be non-destructive. Slicing to the timer (rather
    # than checking the whole function) is what makes this fail when someone
    # reintroduces an inline teardown — the timer below it would still be
    # present, just unreachable, so a bare "is setTimeout there?" check would
    # happily pass. The condition is matched loosely so an added clause
    # doesn't slip past the guard.
    m = re.search(r'st==="disconnected"[^{]*\{(.*?)if\(!_whepGrace\)', fn, re.S)
    assert m, "the disconnected branch no longer arms the grace timer"
    inline = m.group(1)
    assert "whepDropped" not in inline, "a blip tears the session down inline again"
    assert "stopWebrtc" not in inline, "a blip tears the session down inline again"
    # One clock per outage: repeated 'disconnected' must not extend the wait.
    assert "if(!_whepGrace) _whepGrace=setTimeout(" in fn
    # The timer only gives up if the peer is STILL not connected, and is
    # still the current one.
    assert '_pc===pc && pc.connectionState!=="connected"' in fn
    # Recovery cancels the clock and restores the honest caption.
    assert 'st==="connected"' in fn
    assert "_clearWhepGrace();" in fn
    assert "real-time stream · marks" in fn


def test_the_caption_tells_the_truth_during_a_blip():
    """Media really has stopped during the grace window, so the player must
    not keep claiming 'real-time stream' — that was the original bug, just
    for 5s instead of forever."""
    html = _demo_html()
    fn = html[html.index("function handleWebrtcState"):html.index("function whepDropped")]
    assert "reconnecting…" in fn
    # And the note is only painted when the LIVE view actually owns the
    # caption — not over a scrub into review or a recorded segment.
    helper = html[html.index("function _setLiveNote"):html.index("function handleWebrtcState")]
    assert "if(!window._camScreenCam || _revTs || _playback) return;" in helper


def test_leaving_the_camera_screen_cancels_a_pending_grace_timer():
    """Otherwise the timer wakes 5s later and stomps the caption of whatever
    the operator is looking at by then."""
    html = _demo_html()
    fn = html[html.index("function stopWebrtc"):]
    fn = fn[:fn.index("// The same WebRTC")]
    assert "_clearWhepGrace();" in fn
