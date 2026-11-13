"""router.py — decide simple camera questions before the LLM sees them.

Tier 0 fires only when every slot resolves and the question is one
question; anything with a side effect, or about WHO, goes to the model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import router

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 9, 30, 0, tzinfo=IST)
ALL = {"describe_camera", "detect_objects", "search_history", "recent_events",
       "create_monitor", "create_alarm", "stop_alarm"}
CAMS = ["cam1", "cam2"]


ROLES = {"cam1": "front door", "cam2": "back yard"}


def _t0(text, *, cameras=CAMS, advertised=ALL, preferred=None, roles=ROLES):
    return router.decide_tier0(text, cameras=cameras, advertised=advertised,
                               preferred=preferred, roles=roles, now=NOW)


# ── Tier 0: every slot resolves ───────────────────────────────────────

@pytest.mark.parametrize("q, tool", [
    ("what do you see on cam1", "describe_camera"),
    ("is anyone at camera 2", "detect_objects"),
    ("how many cars on cam1", "detect_objects"),
    ("what is the person on cam2 wearing", "describe_camera"),
    ("Is anyone on Camera 2", "detect_objects"),          # STT capitalisation is not a name
    ("is the front door clear", "describe_camera"),        # the roster's role names the camera
])
def test_a_live_question_naming_a_camera_routes_to_the_live_tool(q, tool):
    d = _t0(q)
    assert d is not None and d.routed, (q, d)
    assert d.tool == tool
    assert d.args["camera_id"] in ("cam1", "cam2")


def test_a_describe_question_keeps_its_question():
    d = _t0("what is the person on cam2 wearing")
    assert d.routed and d.args.get("question") == "what is the person on cam2 wearing"
    d = _t0("what do you see on cam1")
    assert d.routed and "question" not in d.args, "a generic look gets the default caption"


def test_a_number_that_is_not_a_camera_is_not_a_camera():
    d = _t0("is anyone there in the last 2 hours", preferred="cam1")
    assert d.routed and d.args["camera_id"] == "cam1", "'2 hours' is not cam2"
    d = _t0("did a car come by between 2 and 4pm")
    assert d.routed and d.tool == "search_history" and "camera_id" not in d.args, (
        "no camera named: the history over every camera, and '2' is not cam2")


def test_a_past_question_with_a_window_routes_to_history_with_iso_bounds():
    d = _t0("did a car come by cam1 between 2pm and 4pm")
    assert d.routed and d.tool == "search_history"
    assert d.args["camera_id"] == "cam1" and d.args["label"] == "car"
    assert d.args["start_time"].startswith("2026-09-28T14:00:00")
    assert d.args["end_time"].startswith("2026-09-28T16:00:00")


def test_a_relative_window_and_an_attribute_ride_along():
    d = _t0("did you see a blue truck on camera 1 in the last 2 hours")
    assert d.routed and d.tool == "search_history"
    assert d.args["label"] == "truck" and d.args["attr"] == ["blue"]
    assert d.args["start_time"].startswith("2026-09-28T07:30:00")


def test_all_cameras_is_a_camera():
    d = _t0("is anyone on any camera")
    assert d.routed and d.args["camera_id"] == "all"
    h = _t0("did anyone come to any of the cameras today")
    assert h.routed and h.tool == "search_history" and "camera_id" not in h.args
    r = _t0("did anyone come by on any camera in the last hour",
            advertised={"recent_events", "detect_objects"})
    assert r.routed and r.tool == "recent_events" and r.args["camera_id"] == "__any__", (
        "recent_events takes '__any__', not 'all' — 'all' was an ERROR string to compose from")


# ── which camera the words name ───────────────────────────────────────

def test_a_camera_id_is_matched_as_a_word_anywhere_in_the_sentence():
    cams = ["garage", "front_door", "cam1", "cam2"]
    assert router._names_camera("is anyone in the garage", cams) == "garage"
    assert router._names_camera("what do you see at the front door", cams) == "front_door"
    assert router._names_camera("is anyone on cam 2 right now", cams) == "cam2"
    assert router._names_camera("the second camera please", cams) == "cam2"
    assert router._names_camera("are the garages full", cams) is None, "not inside a word"
    assert router._names_camera("did anyone come in the last 2 hours", cams) is None, (
        "a bare digit is not a camera")


def test_the_longest_role_wins():
    roles = {"cam1": "Door", "cam2": "Back door"}
    assert router._names_camera("is anyone at the back door", CAMS, roles) == "cam2"
    assert router._names_camera("is anyone at the door", CAMS, roles) == "cam1"


def test_forced_grounding_picks_cameras_the_same_way():
    """_pick_camera is the always-answers version of _names_camera: the
    same words, then the UI's camera, then the first — never a digit."""
    assert router._pick_camera("did anyone stop by in the last 2 hours", CAMS, "cam1") == "cam1"
    assert router._pick_camera("did anyone stop by in the last 2 hours", CAMS) == "all"
    assert router._pick_camera("is anyone on cam2", CAMS, "cam1") == "cam2"


def test_a_noun_after_an_article_is_not_a_name():
    d = _t0("Is there a Car on cam1")
    assert d.routed and d.tool == "detect_objects", d
    d = _t0("is anyone at the Front door on cam1", cameras=["cam1"])
    assert d.routed, d
    assert not _t0("was Priya at camera 1 this morning").routed


def test_router_owns_the_question_shapes_and_camera_agent_borrows_them():
    """camera_agent.py is __main__ in the container; a router that imported
    it would execute the whole file again on the first routed turn."""
    import camera_agent
    assert "camera_agent" not in open(router.__file__).read().split("logger = ")[0].replace(
        "camera_agent.py", ""), "router.py must not import camera_agent"
    assert camera_agent._pick_forced_call is router._pick_forced_call
    assert camera_agent._pick_camera is router._pick_camera


def test_the_camera_the_ui_is_on_or_the_only_camera_counts_as_named():
    d = _t0("what do you see", preferred="cam2")
    assert d.routed and d.args["camera_id"] == "cam2"
    d = _t0("what do you see", cameras=["cam1"])
    assert d.routed and d.args["camera_id"] == "cam1"


# ── Tier 0 refuses ────────────────────────────────────────────────────

def test_two_cameras_and_no_name_looks_at_both():
    """The first camera used to answer for the fleet; now none named means all."""
    d = _t0("what do you see")
    assert d.routed and d.tool == "describe_camera" and d.args["camera_id"] == "all"


@pytest.mark.parametrize("q", [
    "alarm me if someone is at cam1 after 10pm",
    "watch cam1 and tell me if a car shows up",
    "notify me when you see a person on cam2",
    "every morning summarize cam1",
    "let me know if anyone comes to cam1",
])
def test_side_effects_and_standing_requests_go_to_the_model(q):
    d = _t0(q)
    assert d is not None and not d.routed, q
    assert "side effect" in d.reason


def test_stop_by_is_history_not_a_side_effect():
    d = _t0("did anyone stop by camera 1 today")
    assert d.routed and d.tool == "search_history"


@pytest.mark.parametrize("q", [
    "any plates on camera 1 today",
    "take a snapshot of cam1",
    "show me a picture of cam2",
])
def test_questions_for_tools_the_picker_does_not_know_go_to_the_model(q):
    d = _t0(q)
    assert d is not None and not d.routed and "another tool" in d.reason, q


@pytest.mark.parametrize("q", [
    "who came to cam1 today",
    "was Priya at camera 1 this morning",
    "do you recognise the person on cam1",
])
def test_who_questions_go_to_the_model(q):
    d = _t0(q)
    assert d is not None and not d.routed and "who" in d.reason


def test_a_config_question_is_not_the_routers_business():
    """The roster in the prompt answers it — and no Tier-1 hint either:
    "how many cameras do you have" resembles "how many people" word for
    word, and a detect_objects hint sent the model to count people."""
    d = _t0("how many cameras do you have")
    assert d is not None and not d.routed and not d.hintable and "roster" in d.reason
    full = router.decide("how many cameras do you have", cameras=CAMS, advertised=ALL,
                         roles=ROLES, now=NOW)
    assert full.tier == 2 and full.hint is None


def test_two_questions_or_a_story_go_to_the_model():
    d = _t0("what do you see on cam1? and is anyone on cam2?")
    assert not d.routed and "two questions" in d.reason
    long = "so yesterday my neighbour said that around noon there was a van and " \
           "then later a bike and I wonder whether cam1 caught any of that at all"
    assert not _t0(long).routed


def test_a_past_question_with_no_history_tool_is_not_sent_to_a_live_detector():
    d = _t0("did a car come by cam1 today", advertised={"describe_camera", "detect_objects"})
    assert d is not None and not d.routed and "history tool" in d.reason


# ── Tier 1: a lexical hint, never a call ──────────────────────────────

def test_a_rephrasing_gets_a_hint_for_the_tool_it_resembles():
    h = router.hint_tier1("did anybody come by earlier", advertised=ALL)
    assert h is not None and h.tier == 1 and h.hint == "search_history"
    h = router.hint_tier1("is anybody at the door", advertised=ALL)
    assert h is not None and h.hint == "detect_objects"


def test_short_exemplars_still_count():
    h = router.hint_tier1("is anyone there", advertised=ALL)
    assert h is not None and h.hint == "detect_objects"
    assert all(" " not in v for v in router._SYNONYMS.values()), "a synonym must be one token"
    assert all(k != v for k, v in router._SYNONYMS.items()), "no identity mappings"


def test_no_resemblance_or_a_tie_means_no_hint():
    assert router.hint_tier1("tell me a joke about penguins", advertised=ALL) is None
    assert router.hint_tier1("", advertised=ALL) is None
    assert router.hint_tier1("is anybody at the door", advertised={"create_alarm"}) is None


def test_contractions_fold_onto_the_exemplars():
    h = router.hint_tier1("what's happening", advertised=ALL)
    assert h is not None and h.hint == "describe_camera"


def test_a_past_question_is_never_hinted_to_a_live_tool():
    h = router.hint_tier1("was anyone at the door earlier", advertised=ALL)
    assert h is None or h.hint in ("search_history", "recent_events"), h


def test_no_hint_where_tier0_said_the_model_must_decide():
    """A standing request or a WHO question resembles a live phrasing
    word for word; a detector hint there IS the wrong-tool iteration."""
    d = router.decide("watch the door and tell me if anyone comes", cameras=CAMS,
                      advertised=ALL, now=NOW)
    # A standing request gets the standing-verb hint, never a detector's.
    assert d.tier == 1 and d.hint == "create_monitor"
    d = router.decide("every morning summarize cam1", cameras=CAMS,
                      advertised=ALL, now=NOW)
    assert d.tier == 2 and d.hint is None and "side effect" in d.reason   # no report tool advertised
    d = router.decide("who is at the door", cameras=CAMS, advertised=ALL, now=NOW)
    assert d.tier == 2 and d.hint is None and "who" in d.reason


# ── the one call the turn makes ───────────────────────────────────────

def test_decide_prefers_tier0_then_hint_then_nothing():
    d = router.decide("what do you see on cam1", cameras=CAMS, advertised=ALL, now=NOW)
    assert d.tier == 0 and d.tool == "describe_camera"
    d = router.decide("is anybody at the door", cameras=CAMS, advertised=ALL, now=NOW)
    assert d.tier == 0 and d.tool == "detect_objects" and d.args["camera_id"] == "all", (
        "two cameras, none named: the detector over both")
    d = router.decide("tell me a joke", cameras=CAMS, advertised=ALL, now=NOW)
    assert d.tier == 2
    d = router.decide("what do you see on cam1", cameras=CAMS, advertised=ALL, tier0=False,
                      hints=False, now=NOW)
    assert d.tier == 2 and "off" in d.reason


def test_the_compose_prompt_is_short_and_ends_with_the_clock():
    p = router.compose_prompt("I am the agent.", "Always answer in Hindi.", "- cam1: front door",
                              "The current date and time is X.")
    assert p.startswith("I am the agent.")
    assert "Always answer in Hindi." in p, "the operator's prompt applies to routed turns too"
    assert p.rstrip().endswith("The current date and time is X.")
    assert "tool" not in p.lower(), "no schemas, no routing guidance: the decision is made"
    assert len(p.split()) < 140, "a compose prompt re-prefills every turn; it must stay small"


# ── small talk is tier 0 with no tool ──────────────────────────────

@pytest.mark.parametrize("q", [
    "thanks, that's all", "hi, what can you do?", "hello there", "ok bye",
    "good morning", "thank you so much",
])
def test_small_talk_is_tier_0_with_no_tool(q):
    """A greeting or a closing never reaches the model with tools: with
    them, a model told to always look first runs a tool sweep on "thanks"
    (granite4:micro, measured) and answers "I can't see right now"."""
    d = _t0(q)
    assert d is not None and d.routed and d.tool is None and d.reason == "small talk"
    assert d.hintable is False
    # Through decide() too, and whatever the camera list: a goodbye needs
    # no camera, and an install with none still gets a civil answer.
    full = router.decide(q, cameras=CAMS, advertised=ALL)
    assert full.routed and full.tool is None
    assert _t0(q, cameras=[]).routed


@pytest.mark.parametrize("q", [
    "thanks, and is anyone at the door?", "hi, what's on cam1?",
    "ok, how many cars on cam2", "thanks — did anyone come by today?",
])
def test_one_real_word_makes_it_not_small_talk(q):
    d = _t0(q)
    assert d is None or not (d.routed and d.tool is None), q


# ── standing requests: alarm or monitor, by the words ────────────────

@pytest.mark.parametrize("q,tool", [
    ("let me know when a car arrives at cam1", "create_monitor"),
    ("notify me when a person shows up at cam1", "create_monitor"),
    ("notify me when more than 3 people gather on cam1", "create_monitor"),
    ("keep an eye on cam1 for a truck", "create_monitor"),
    ("sound the siren if anyone is at cam1 before 6am", "create_alarm"),
    ("set a silent alarm for a person at cam1 after 11pm", "create_alarm"),
    ("ring an alarm if a person shows up there after 10pm", "create_alarm"),
    ("wake me if someone comes to cam1", "create_alarm"),
])
def test_standing_requests_are_hinted_by_their_words(q, tool):
    """Measured on the NPU: 'let me know when a car arrives' went to
    create_alarm with an invented midnight window, six times of six."""
    d = router.decide(q, cameras=CAMS, advertised=ALL | {"stop_monitor"}, now=NOW)
    assert d.tier == 1 and d.hint == tool, (q, d)


@pytest.mark.parametrize("q,tool,args", [
    ("stop watching for that", "stop_monitor", {}),
    ("actually, cancel that alarm", "stop_alarm", {"action": "disarm"}),
    ("turn off the siren", "stop_alarm", {"action": "disarm"}),
    ("silence the alarm", "stop_alarm", {"action": "silence"}),
    ("stop the alarm on cam1", "stop_alarm", {"action": "disarm"}),
    ("cancel alarm 2", "stop_alarm", {"alarm_id": 2}),
    ("delete report 3", "stop_report", {"report_id": 3}),
    ("stop monitor number 4", "stop_monitor", {"monitor_id": 4}),
])
def test_a_bare_stop_request_runs_the_stop_tool_without_the_model(q, tool, args):
    """The stop tools pick the item themselves (the number said, the last
    one set up, the only one, or they ask), so there is nothing for the
    model to fill — and hinted, it still skipped the call 1 time in 3
    (Ventuno Q eval, 2026-11-09)."""
    d = router.decide(q, cameras=CAMS, advertised=ALL | {"stop_monitor", "stop_report"}, now=NOW)
    assert d.tier == 0 and d.tool == tool, (q, d)
    for k, v in args.items():
        assert d.args.get(k) == v, (q, d.args)


@pytest.mark.parametrize("q", ["stop talking", "stop listening", "stop the recording",
                               "please stop the conversation"])
def test_a_stop_about_the_conversation_or_recording_is_not_a_stop_tool(q):
    d = router.decide(q, cameras=CAMS, advertised=ALL | {"stop_monitor", "stop_report"}, now=NOW)
    assert d.tool is None, (q, d)


def test_standing_hint_only_names_advertised_tools():
    d = router.decide("let me know when a car arrives at cam1", cameras=CAMS,
                      advertised={"describe_camera", "detect_objects"}, now=NOW)
    assert d.tier == 2 and d.hint is None


@pytest.mark.parametrize("q,window", [
    ("alarm if a person is seen after 6pm", (1080, None, True)),
    ("sound the siren if anyone is at cam1 before 6am", (None, 360, True)),
    ("ring an alarm if a person shows up there after 10pm", (1320, None, True)),
    ("set a silent alarm for a person after 11 pm", (1380, None, True)),
    ("alarm between 10pm and 6am if anyone comes", (1320, 360, True)),
    ("alarm from 22:30 to 05:00 for a person", (1350, 300, True)),
    ("after 12:10 pm ring if a car comes", (730, None, True)),
    ("alarm if a person comes after midnight", (None, None, True)),
    ("let me know when a car arrives at cam1", (None, None, False)),
    ("notify me when a person shows up tonight", (None, None, False)),
    ("alarm if 3 people gather", (None, None, False)),
])
def test_clock_window_from_text(q, window):
    assert router.clock_window_from_text(q) == window


# ── no camera named ───────────────────────────────────────────────────

SIX = [f"cam{i}" for i in range(1, 7)]
SIX_ROLES = {"cam1": "the front door", "cam2": "the gate", "cam3": "the yard",
             "cam4": "the garage", "cam5": "the hall", "cam6": "the roof"}


def test_no_camera_named_looks_at_all_for_detection_and_history():
    d = _t0("is anyone there?")                      # two cameras, no name, no selection
    assert d.routed and d.tool == "detect_objects" and d.args["camera_id"] == "all"
    d = _t0("how many people are there right now")
    assert d.routed and d.tool == "detect_objects" and d.args["camera_id"] == "all"
    d = _t0("did anyone come by this morning")
    assert d.routed and d.tool == "search_history" and "camera_id" not in d.args


def test_no_camera_named_describes_all_up_to_the_cap_then_asks():
    d = _t0("what do you see?")                      # two cameras: describe them both
    assert d.routed and d.tool == "describe_camera" and d.args["camera_id"] == "all"
    d = _t0("what is the person wearing?", cameras=SIX, roles=SIX_ROLES)
    assert not d.routed and d.ask is not None
    assert d.ask.startswith("Which camera do you mean: the front door, the gate")
    assert d.ask.endswith("or the roof?")
    # counting six cameras is cheap: no question, every camera
    d = _t0("is anyone there?", cameras=SIX, roles=SIX_ROLES)
    assert d.routed and d.args["camera_id"] == "all"


def test_a_selected_or_named_camera_still_wins():
    d = _t0("what do you see?", preferred="cam2")
    assert d.routed and d.args["camera_id"] == "cam2"
    d = _t0("what do you see on the back yard", cameras=SIX, roles=SIX_ROLES | {"cam2": "back yard"})
    assert d.routed and d.args["camera_id"] == "cam2"


def test_decide_carries_the_which_camera_question_past_the_hints():
    d = router.decide("what is the person wearing?", cameras=SIX, advertised=ALL, roles=SIX_ROLES, now=NOW)
    assert d.tier == 2 and d.hint is None and d.ask.startswith("Which camera do you mean")


# ── the office session of 2026-11-09 ─────────────────────────────────

@pytest.mark.parametrize("q", [
    "Hey, how are you doing?", "do you hear me?", "Yeah, I'm good. Yeah. Um okay. Okay. Thank you. Bye.",
    "um okay", "yes", "alright, thanks",
])
def test_spoken_check_ins_and_fillers_are_small_talk(q):
    """'Hey, how are you doing?' was routed to describe_camera and answered
    by the VLM ("I'm doing well, thank you for asking!"); a tail of
    fillers reached the model with tools and came back as the room."""
    d = _t0(q)
    assert d is not None and d.routed and d.tool is None and d.reason == "small talk", q


@pytest.mark.parametrize("q, reply", [
    ("how is the weather today?", "I am sorry, I cannot provide the weather. I can only access the camera feeds and history."),
    ("Hey, do you hear me? Hi, how are you?", "I am the OpenNVR Agent. I am here to assist you with your camera system."),
    ("remove the alarms", "I see that you are asking to remove alarms. I need an alarm ID."),
    ("okay then remove it for person", "I don't have a watch number to stop."),
])
def test_a_reply_that_mentions_cameras_without_a_sighting_is_not_forced(q, reply):
    """Every one of these was forced into describe_camera on the box: the
    weather question came back as a description of the office, and
    'remove the alarms' as 'I see two chairs'."""
    assert not router._should_force_grounding(q, reply), (q, reply)


@pytest.mark.parametrize("q, reply", [
    ("what's on hammer 2", "Camera 2 shows a person at the door."),
    ("hmm", "I see a dog in the yard."),
    ("is anyone at the gate?", "No one is there."),
])
def test_a_claimed_sighting_or_a_camera_question_still_forces(q, reply):
    assert router._should_force_grounding(q, reply), (q, reply)


@pytest.mark.parametrize("q", [
    "Thank you. Bye.", "Yeah, I'm good. Yeah. Um okay. Okay. Thank you. Bye.", "ok thanks bye bye",
    "alright, see you", "that's it, good night!",
])
def test_a_turn_that_ends_in_a_goodbye_is_a_closing(q):
    assert router.is_closing(q), q


@pytest.mark.parametrize("q", ["bye the way, is anyone there?", "thanks, that's all", "good night camera 1 looks dark"])
def test_a_goodbye_word_mid_sentence_is_not_a_closing(q):
    assert not router.is_closing(q), q


@pytest.mark.parametrize("q", [
    "okay then remove it for person. I don't need any notification when that person arrives.",
    "can you remove it for the car?", "I don't need that notification any more", "delete the watch",
])
def test_taking_a_standing_thing_down_is_a_side_effect_not_a_detection(q):
    """'remove it for person…' was routed to detect_objects and answered
    'two chairs and a toilet'."""
    d = _t0(q)
    assert d is not None and not d.routed and "side effect" in d.reason, q


@pytest.mark.parametrize("last, tool", [("alarm", "stop_alarm"), ("report", "stop_report"),
                                         ("monitor", "stop_monitor"), (None, "stop_monitor")])
def test_remove_it_means_the_thing_set_up_last(last, tool):
    d = router.decide("can you remove it?", cameras=CAMS, advertised=ALL | {"stop_monitor", "stop_report"},
                      now=NOW, last_kind=last)
    assert d.tier == 0 and d.tool == tool, (last, d)
    # named kinds still win over the memory
    d = router.decide("remove the alarm", cameras=CAMS, advertised=ALL | {"stop_monitor"}, now=NOW, last_kind="monitor")
    assert d.tool == "stop_alarm"
    # with the stop tools not advertised, the hint path is what remains
    d = router.decide("can you remove it?", cameras=CAMS, advertised=ALL - {"stop_alarm"} | {"stop_monitor"},
                      now=NOW, last_kind="alarm")
    assert d.tool is None and d.hint is None


@pytest.mark.parametrize("q", ["Bye. Thank you.", "ok bye, thanks a lot", "goodbye and thanks", "bye bye. okay."])
def test_a_goodbye_anywhere_in_pure_small_talk_is_a_closing(q):
    assert router.is_closing(q), q


@pytest.mark.parametrize("q", ["thanks a lot", "okay, hello there", "good morning"])
def test_small_talk_without_a_goodbye_is_not_a_closing(q):
    assert not router.is_closing(q), q


@pytest.mark.parametrize("q,at,every", [
    ("every hour tell me what you see on cam1", None, 60),
    ("every 15 minutes count the people", None, 15),
    ("every two hours summarise the gate", None, 120),
    ("every half hour tell me who came", None, 30),
    ("hourly report of visitors", None, 60),
    ("every morning at 7 summarise overnight activity", 7 * 60, None),
    ("every evening tell me how many cars came", 18 * 60, None),
    ("daily at 9pm give me a rundown", 21 * 60, None),
    ("every morning summarise cam1", 8 * 60, None),
    ("summarise the day", None, None),
])
def test_a_report_schedule_is_read_from_the_words(q, at, every):
    assert router.schedule_from_text(q) == (at, every), q


def test_a_long_stop_request_is_judged_by_its_first_clause():
    d = router.decide("okay then remove it, I don't need any notification when a car arrives",
                      cameras=CAMS, advertised=ALL | {"stop_monitor", "stop_report"}, now=NOW,
                      last_kind="monitor")
    assert d.tier == 0 and d.tool == "stop_monitor", d

