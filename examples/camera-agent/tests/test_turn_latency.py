"""The four rules for a fast turn on CPU (MODELS_AND_LATENCY.md).

A field trace read `stt 14s → llm 17s → search_history 12s → llm 2.6s →
tts 25s · 71s`. Each rule below removes one structural cause; each test
pins the rule, not the timing.
"""
from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from adapter_clients import PiperClient, WhisperClient
from camera_agent import AppConfig, CameraAgentRuntime, load_config
from context import CameraContext, CameraSpec
from tools import CameraTools


def _runtime():
    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t", text_mode=True,
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg",
                                        role="front door", opennvr_camera_id=7)])
    return CameraAgentRuntime(cfg)


# ── 1. the prompt's static prefix is byte-identical across turns ──────

def test_the_turns_system_prompt_carries_no_clock_and_the_clock_rides_the_user_turn():
    """Ollama renders the tool schemas inside the system turn, after the
    system text: a clock anywhere in that text sits ahead of ~3.5k tokens
    of schemas and changes every minute. And EVERY system-role message
    lands in that one block — template.go's collate joins them — so a
    "trailing" system clock is no better. The turn's system prompt has
    NO clock, no system message follows the history, and the clock rides
    in the user turn: the one message rendered last."""
    rt = _runtime()
    # The un-routed shape is under test; with one camera the router would
    # take this turn (and its compose prompt, with no tools, may end with
    # the clock — that is fine and is router.py's own test).
    rt.cfg.router_tier0 = False
    rt.cfg.router_hints = False
    marker = "The current date and time is"
    assert marker not in rt.build_system_prompt(clock=False)
    assert marker in rt.build_system_prompt(), "the streaming context still gets it in-prompt"

    seen = {}

    class _LLM:
        async def chat(self, *, messages, **kw):
            seen["messages"] = messages
            return {"message": {"content": "All quiet.", "tool_calls": []}}

    rt.ollama = _LLM()
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    from camera_agent import _run_conversation_turn
    asyncio.run(_run_conversation_turn(rt, history, "is the front door clear", max_iterations=1))
    msgs = seen["messages"]
    assert msgs[0]["role"] == "system" and marker not in msgs[0]["content"]
    assert msgs[1:3] == history, "history right after the static prefix"
    assert msgs[3]["role"] == "user", "the user turn right after the history"
    assert msgs[3]["content"].startswith("[" + marker)
    assert msgs[3]["content"].endswith("\n\nis the front door clear"), "the user's words, last"
    assert not any(m["role"] == "system" for m in msgs[1:]), (
        "Ollama would hoist it in front of the tool schemas")


def test_two_builds_share_everything_before_the_clock():
    rt = _runtime()
    a, b = rt.build_system_prompt(clock=False), rt.build_system_prompt(clock=False)
    assert a == b, "byte-identical: the whole system+tools prefix stays cached"
    assert len(a) > 500
    assert rt.build_system_prompt().startswith(a), "with the clock in, only the tail differs"
    rt2 = _runtime(); rt2.cfg.llm_think = False; rt2.cfg.llm_model = "qwen3:1.7b"
    tail = rt2.build_system_prompt().strip().split("\n\n")
    assert tail[-1] == "/no_think" and tail[-2].startswith("The current date and time is"), (
        "Qwen3's constant control switch may still follow the clock")


# ── 2. Whisper runs greedy ────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _Http:
    def __init__(self, payload):
        self.payload, self.calls = payload, []

    async def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return _Resp(self.payload)


def test_whisper_is_asked_for_a_greedy_decode():
    c = WhisperClient(url="http://w:9003", token="")
    c._http = _Http({"result": {"text": "is anyone at the door"}})
    text = asyncio.run(c.transcribe(b"RIFFwav"))
    assert text.strip() == "is anyone at the door"
    (_, body, _), = c._http.calls
    assert body["beam_size"] == 1, "beam 5 is 30-50% more CPU for nothing on a short clip"
    assert body["vad_filter"] is False


# ── 3. the Piper voice is asked for per request ───────────────────────

def test_the_configured_voice_rides_every_synthesis_request():
    wav = base64.b64encode(b"RIFF").decode()
    c = PiperClient(url="http://p:9001", token="", voice="en_US-lessac-medium")
    c._http = _Http({"result": {"audio_b64": wav}})
    assert asyncio.run(c.synthesize("hello")) == b"RIFF"
    (_, body, _), = c._http.calls
    assert body["voice"] == "en_US-lessac-medium"

    d = PiperClient(url="http://p:9001", token="")
    d._http = _Http({"result": {"audio_b64": wav}})
    asyncio.run(d.synthesize("hello"))
    assert "voice" not in d._http.calls[0][1], "unset = the adapter's own default"


def test_piper_voice_comes_from_the_environment_when_config_is_silent(tmp_path, monkeypatch):
    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"\xff\xd8\xff\xd9")
    cfg_path = tmp_path / "config.yml"
    cfg_path.write_text(
        "kaic_url: http://x\nkaic_api_key: y\n"
        f"cameras:\n  - {{camera_id: cam1, frame_url: 'file://{frame}', role: 'door'}}\n")
    monkeypatch.setenv("PIPER_VOICE", "en_GB-alan-medium")
    assert load_config(str(cfg_path)).piper_voice == "en_GB-alan-medium"
    monkeypatch.delenv("PIPER_VOICE")
    assert load_config(str(cfg_path)).piper_voice == ""


# ── 4. search_history fetches each photo once; face-ID only when asked who

class _Events:
    def __init__(self, rows):
        self.rows, self.fetches, self.last_pending = rows, [], None

    async def search(self, **kw):
        return self.rows

    async def evidence(self, event_id):
        self.fetches.append(event_id)
        return b"\xff\xd8crop%d" % event_id


class _Recognise:
    def __init__(self):
        self.calls = 0

    async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
        self.calls += 1
        return {"result": {"recognized": True, "name": "Priya"}}


def _visit(i):
    return SimpleNamespace(id=i, camera_id=7, label="person", score=0.9,
                           started_at="2026-09-28T14:02:00+05:30",
                           ended_at="2026-09-28T14:05:00+05:30",
                           stationary=False, plate_text=None, has_evidence=True)


def _tools(events, recognise):
    ctx = CameraContext(cameras=[CameraSpec(camera_id="cam1", frame_url="x", role="door",
                                            opennvr_camera_id=7)])
    return CameraTools(context=ctx, detection_client=None, caption_client=None,
                       recognition_client=recognise, events_client=events)


def test_did_anyone_come_fetches_each_photo_once_and_never_recognises():
    ev, rec = _Events([_visit(i) for i in range(1, 9)]), _Recognise()
    tools = _tools(ev, rec)
    tools.current_question = "did anyone come to the door today"
    out = asyncio.run(tools.search_history({"label": "person"}))
    assert "I remember 8 person visit" in out
    assert sorted(ev.fetches) == [1, 2, 3, 4, 5], "three shown + two of slack, each once"
    assert rec.calls == 0, "'did anyone come' does not pay for face recognition"
    assert len(tools.last_evidence_frames) == 3
    assert "Recognised" not in out


def test_who_came_recognises_on_the_same_fetched_photos():
    ev, rec = _Events([_visit(i) for i in range(1, 9)]), _Recognise()
    tools = _tools(ev, rec)
    out = asyncio.run(tools.search_history({"label": "person", "identify_faces": True}))
    assert sorted(ev.fetches) == [1, 2, 3, 4, 5, 6], "four for face-ID + slack, no photo twice"
    assert rec.calls == 4
    assert "Recognised: Priya" in out
    assert len(tools.last_evidence_frames) == 3


def test_the_question_decides_face_matching_when_the_model_left_the_flag_out():
    """qwen2.5:1.5b routinely omits optional booleans. 'was Priya here'
    must not become a list of times with no name because of that."""
    for q, expect in (("was Priya at the door this afternoon", True),
                      ("who came to the door", True),
                      ("did anyone come to the door", False)):
        ev, rec = _Events([_visit(i) for i in range(1, 5)]), _Recognise()
        tools = _tools(ev, rec)
        tools.current_question = q
        out = asyncio.run(tools.search_history({"label": "person"}))
        assert (rec.calls > 0) is expect, q
        assert ("Recognised: Priya" in out) is expect, q


def test_the_flag_as_a_string_is_read_as_a_boolean():
    """qwen2.5:1.5b also hands over "false" — the string. bool("false")
    is True; that was four recognitions for 'did anyone come'."""
    ev, rec = _Events([_visit(i) for i in range(1, 5)]), _Recognise()
    tools = _tools(ev, rec)
    tools.current_question = "who came to the door"
    asyncio.run(tools.search_history({"label": "person", "identify_faces": "false"}))
    assert rec.calls == 0


def test_a_weekday_or_a_camera_word_is_not_a_name():
    from tools import asked_who
    assert asked_who("did anyone come to the door on Monday") is False
    assert asked_who("how many people came in September") is False
    assert asked_who("is there a Car on the Front door camera") is False
    assert asked_who("was Priya at the door") is True
    assert asked_who("who came by") is True
    assert asked_who(None) is None, "no question to read"


def test_no_question_to_read_means_match_as_before():
    """The streaming /ws path calls the handler straight from the
    pipeline, in a task that set no question: match, as it always did —
    never another path's stale question."""
    ev, rec = _Events([_visit(i) for i in range(1, 5)]), _Recognise()
    tools = _tools(ev, rec)
    asyncio.run(tools.search_history({"label": "person"}))
    assert rec.calls == 4


def test_the_question_is_task_local():
    """A scheduled report's turn runs while a person's is in flight; each
    tool call must see ITS turn's question, not whichever was set last."""
    ev, rec = _Events([_visit(i) for i in range(1, 5)]), _Recognise()
    tools = _tools(ev, rec)

    async def turn(q, expect):
        tools.current_question = q
        await asyncio.sleep(0.01)              # the other turn sets its own meanwhile
        assert tools.current_question == q
        out = await tools.search_history({"label": "person"})
        assert ("Recognised" in out) is expect, q

    async def both():
        await asyncio.gather(turn("who came to the door", True),
                             turn("did anything happen overnight", False))
    asyncio.run(both())


def test_a_bad_read_does_not_cost_a_photo_or_a_name():
    class _Flaky(_Events):
        async def evidence(self, event_id):
            self.fetches.append(event_id)
            return None if event_id == 1 else b"\xff\xd8crop%d" % event_id

    class _Once(_Recognise):
        async def infer(self, *, frame_jpeg, extra=None, correlation_id=None):
            self.calls += 1
            if frame_jpeg.endswith(b"2"):
                raise RuntimeError("adapter hiccup")
            return {"result": {"recognized": True, "name": "Priya"}}

    ev, rec = _Flaky([_visit(i) for i in range(1, 9)]), _Once()
    tools = _tools(ev, rec)
    out = asyncio.run(tools.search_history({"label": "person", "identify_faces": True}))
    assert len(tools.last_evidence_frames) == 3, "the failed read was covered by the slack"
    assert rec.calls == 4 and "Recognised: Priya" in out, (
        "one recognition failing does not abort the others")
