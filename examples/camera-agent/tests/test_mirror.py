# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""OpenNVR Mirror: the narration loop's policy, with every outside call
faked. The vision model is the bottleneck, so the tests pin WHAT is asked
next and WHEN something is said, not how fast."""
from __future__ import annotations

import asyncio

import pytest

from mirror import (
    PERSON_PROMPT, SCENE_PROMPT, SCENE_PROMPT_EMPTY, MirrorSession, consensus, crop_person,
    normalise_fields, parse_person_fields, person_summary, same_sentence, scene_prompt,
)


# ── parsing ─────────────────────────────────────────────────────────

def test_parses_key_value_answer_and_drops_unknowns():
    f = parse_person_fields("gender=man; age=30s; top=red t-shirt; bottom=unknown; "
                            "headwear=none; glasses=no; carrying=backpack; expression=Smiling.")
    assert f == {"gender": "man", "age": "30s", "top": "red t-shirt",
                 "carrying": "backpack", "expression": "smiling"}
    assert person_summary(f) == "man, 30s, red t-shirt, backpack"


def test_glasses_yes_shows_as_a_word_and_colon_separators_work():
    f = parse_person_fields("Gender: woman\nAge: 20-30\nTop: blue jacket\nGlasses: yes")
    assert f["glasses"] == "yes"
    assert person_summary(f) == "woman, 20-30, blue jacket, glasses"


def test_free_text_answer_becomes_a_summary():
    f = parse_person_fields("A man in a red shirt holding a phone, smiling at the camera.")
    assert list(f) == ["summary"]
    assert f["summary"].startswith("A man in a red shirt")
    assert person_summary(f) == f["summary"]


def test_empty_answer_is_empty():
    assert parse_person_fields("") == {}
    assert parse_person_fields("unknown") == {}
    assert person_summary({}) == ""


def test_same_sentence_is_a_rephrase_not_news():
    a = "A man in a blue shirt stands in front of the camera holding a phone."
    b = "One man wearing a blue shirt is standing before the camera with a phone."
    c = "Two women in red jackets walk past a table with laptops."
    assert same_sentence(a, b)
    assert not same_sentence(a, c)
    assert same_sentence("", "")


def test_scene_prompt_is_grounded_in_the_tracks():
    assert scene_prompt([]) == SCENE_PROMPT_EMPTY
    assert "Do not mention people" in SCENE_PROMPT_EMPTY
    p = scene_prompt([{"label": "person"}, {"label": "person"}, {"label": "car"}, {"label": "cell_phone"}])
    assert "currently sees: 2 people, 1 car, 1 cell phone" in p and "Never invent a person" in p
    assert scene_prompt([{"label": "person"}]).count("1 person") == 1
    cars = scene_prompt([{"label": "car"}, {"label": "car"}])
    assert "sees: 2 cars" in cars and "Do not mention people" in cars and "Never invent" not in cars


# ── the loop's policy ───────────────────────────────────────────────

class Fake:
    """Every outside call, recorded. ``vision`` answers by prompt."""

    def __init__(self):
        self.now = 1000.0
        self.published: list[dict] = []
        self.asked: list[str] = []
        self.spoken: list[str] = []
        self.frames = 0
        self.crops: list = []
        self.caption_text = "A man in a red shirt waves at the camera."
        self.person_text = "gender=man; age=30s; top=red shirt; carrying=none"

    def clock(self):
        return self.now

    async def frame(self, cam):
        self.frames += 1
        return b"frame"

    async def crop(self, cam, track):
        self.crops.append(track)
        return b"crop-%s" % str(track).encode()

    async def vision(self, image, question):
        self.asked.append("person" if question == PERSON_PROMPT else "scene")
        self.prompts = getattr(self, "prompts", []); self.prompts.append(question)
        self.now += 2.0                       # the model takes two seconds
        return self.person_text if question == PERSON_PROMPT else self.caption_text

    async def speech(self, text):
        self.spoken.append(text)
        return b"RIFF" + b"\0" * 44100       # ~1 s of 16-bit mono at 22.05 kHz

    def publish(self, payload):
        self.published.append(payload)


def _session(fake, **kw):
    return MirrorSession("cam1", frame=fake.frame, crop=fake.crop, vision=fake.vision,
                         speech=fake.speech, publish=fake.publish, clock=fake.clock, **kw)


def _person_frame(*ids, box=(0.3, 0.2, 0.2, 0.6)):
    return {"tracks": [{"id": i, "label": "person", "score": 0.9, "box": list(box)} for i in ids]}


@pytest.mark.asyncio
async def test_the_scene_line_comes_from_core_when_wired_and_the_loop_makes_no_call():
    """RFC-0004 step 4: the subtitle is core's shared description; the
    loop's own vision call is not made for the scene."""
    fake = Fake()
    asked: list[float] = []

    async def description(max_age_s):
        asked.append(max_age_s)
        return {"text": "A man in a red shirt waves at the camera.", "source": "visit",
                "model_called": False, "age_s": 2.0}

    s = _session(fake, description=description)
    assert await s.step() is True
    assert fake.asked == [] and asked == [s._scene_every]
    assert s.caption == "A man in a red shirt waves at the camera."
    m = fake.published[-1]["mirror"]
    assert m["caption"] == s.caption and m["source"] == "visit" and m["spoken"] == s.caption
    assert s.stats.core_answers == 1 and s.stats.captions == 1


@pytest.mark.asyncio
async def test_a_stale_core_answer_is_shown_but_not_spoken():
    fake = Fake()

    async def description(max_age_s):
        return {"text": "A parked car.", "source": "stale", "age_s": 120.0,
                "reason": "no fresh frame from the camera", "model_called": False}

    s = _session(fake, description=description)
    assert await s.step() is True
    m = fake.published[-1]["mirror"]
    assert m["caption"] == "A parked car." and m["stale_s"] == 120.0 and "spoken" not in m
    assert fake.spoken == [] and fake.asked == []


@pytest.mark.asyncio
async def test_when_core_does_not_answer_the_loop_looks_itself():
    fake = Fake()

    async def description(max_age_s):
        return None

    s = _session(fake, description=description)
    assert await s.step() is True
    assert fake.asked == ["scene"] and s.caption == fake.caption_text and s.stats.core_answers == 0


@pytest.mark.asyncio
async def test_first_step_describes_the_scene_and_speaks_it():
    fake = Fake(); s = _session(fake)
    assert await s.step() is True
    assert fake.asked == ["scene"]
    assert s.caption == fake.caption_text
    m = fake.published[-1]["mirror"]
    assert m["caption"] == fake.caption_text and m["audio_b64"] and m["spoken"] == fake.caption_text
    assert s.stats.captions == 1 and s.stats.spoken == 1 and s.stats.vlm_ms_last == 2000


@pytest.mark.asyncio
async def test_a_person_in_frame_long_enough_is_described_before_the_scene_refreshes():
    fake = Fake(); s = _session(fake)
    await s.step()                                   # the first scene sentence
    s.observe_tracks(_person_frame(7))
    assert await s.step() is False                   # too new: no call yet
    fake.now += 1.0
    s.observe_tracks(_person_frame(7))
    assert await s.step() is True
    assert fake.asked[-1] == "person" and fake.crops == [7]
    card = s.cards[7]
    assert card.summary == "man, 30s, red shirt" and card.described_at is not None
    m = fake.published[-1]["mirror"]
    assert m["card"]["track_id"] == 7 and m["card"]["confirmed"] is False and "greeting" not in m
    assert not any(x.startswith("Hello") for x in fake.spoken), "one look is not spoken: it must be confirmed first"
    # Still in frame 2.5 s later: the second look, and the two agree.
    for _ in range(2):
        fake.now += 1.5; s.observe_tracks(_person_frame(7))
    assert s.next_person() is card
    await s.step()
    m = fake.published[-1]["mirror"]
    assert m["card"]["confirmed"] is True and m["greeting"].startswith("Hello! I see a man, 30s, red shirt")
    assert fake.spoken[-1] == m["greeting"]


@pytest.mark.asyncio
async def test_a_tiny_or_fleeting_person_is_not_asked_about():
    fake = Fake(); s = _session(fake)
    await s.step()
    fake.now += 2.0
    s.observe_tracks(_person_frame(3, box=(0.5, 0.5, 0.05, 0.1)))   # 0.5 % of the frame
    assert s.next_person() is None
    s.observe_tracks(_person_frame(4))                                # just appeared
    assert s.next_person() is None


@pytest.mark.asyncio
async def test_described_people_are_refreshed_only_after_a_while_and_never_re_greeted():
    fake = Fake(); s = _session(fake)
    await s.step(); fake.now += 1.0
    s.observe_tracks(_person_frame(7)); fake.now += 1.0; s.observe_tracks(_person_frame(7))
    await s.step()                                               # first look
    assert s.cards[7].described_at is not None
    for _ in range(2):
        fake.now += 1.5; s.observe_tracks(_person_frame(7))
    await s.step()                                               # second look: confirmed + greeted
    assert s.cards[7].confirmed
    greeted_once = len([p for p in fake.published if p["mirror"].get("greeting")])
    assert greeted_once == 1
    for _ in range(3):                                           # still in frame, 6 s on
        fake.now += 2.0; s.observe_tracks(_person_frame(7))
    assert s.next_person() is None                               # fresh enough
    for _ in range(15):                                          # 30 s later, never left
        fake.now += 2.0; s.observe_tracks(_person_frame(7))
    assert s.next_person() is s.cards[7]
    await s.step()
    assert fake.asked.count("person") == 3
    assert len([p for p in fake.published if p["mirror"].get("greeting")]) == greeted_once


@pytest.mark.asyncio
async def test_the_same_sentence_is_not_spoken_twice_and_voice_off_is_silent():
    fake = Fake(); s = _session(fake)
    await s.step()
    assert fake.prompts[-1] == SCENE_PROMPT_EMPTY, "no tracks: the model is told nobody is there"
    fake.now += 20.0                                 # past the speaking floor and the cadence
    fake.caption_text = "One man wearing a red shirt is waving at the camera."
    await s.step()
    assert fake.asked == ["scene", "scene"] and len(fake.spoken) == 1, "a rephrase is not news"
    assert s.caption == "One man wearing a red shirt is waving at the camera.", "after the hold it may replace the text"
    fake.caption_text = "Two women in green jackets carry a large box across the hall."
    fake.now += 20.0
    await s.step()
    assert len(fake.spoken) == 2
    s.voice = False
    fake.caption_text = "An empty hall with a blue banner and three chairs."
    fake.now += 20.0
    await s.step()
    assert len(fake.spoken) == 2 and s.caption.startswith("An empty hall")


@pytest.mark.asyncio
async def test_scene_cadence_is_slower_when_nobody_is_there():
    fake = Fake(); s = _session(fake, scene_every_s=3.0)
    await s.step()                                   # caption_at = now
    fake.now += 4.0
    assert s.scene_due() is False                    # empty view: 12 s cadence
    s.observe_tracks({"tracks": [{"id": 1, "label": "chair", "score": .8, "box": [.1, .1, .2, .2]}]})
    assert s.scene_due() is True                     # something in frame: 3 s cadence


@pytest.mark.asyncio
async def test_cards_follow_tracks_and_expire():
    fake = Fake(); s = _session(fake)
    s.observe_tracks(_person_frame(1, 2))
    assert set(s.cards) == {1, 2}
    fake.now += 1.0
    s.observe_tracks(_person_frame(2, box=(0.4, 0.1, 0.3, 0.7)))
    assert s.cards[2].box == [0.4, 0.1, 0.3, 0.7] and 1 in s.cards
    fake.now += 5.0
    s.observe_tracks({"tracks": []})
    assert s.cards == {}
    wire = s.to_wire()
    assert wire["camera"] == "cam1" and wire["people"] == 0 and wire["running"] is False


@pytest.mark.asyncio
async def test_the_loop_stops_without_a_heartbeat():
    fake = Fake(); s = _session(fake)
    s.start()
    assert s.running and fake.published[0] == {"mirror": {"state": "started", "camera": "cam1"}}
    fake.now += 100.0                                # no page for longer than the TTL
    for _ in range(40):
        await asyncio.sleep(0.01)
        if not s.running:
            break
    assert not s.running
    assert fake.published[-1]["mirror"] == {"state": "stopped", "camera": "cam1", "reason": "no viewer"}


@pytest.mark.asyncio
async def test_no_frame_means_no_model_call_and_no_spin():
    fake = Fake(); s = _session(fake)

    async def none(cam):
        return None
    s._frame = none; s._crop = None
    await s.step()
    assert fake.asked == [] and fake.published[-1]["mirror"].get("no_frame") is True
    s.observe_tracks(_person_frame(9)); fake.now += 2.0; s.observe_tracks(_person_frame(9))
    await s.step()
    assert fake.asked == [] and s.cards[9].described_at is not None


# ── the routes ──────────────────────────────────────────────────────

def _runtime_with_fakes():
    from fastapi.testclient import TestClient
    from camera_agent import AppConfig, CameraAgentRuntime, build_app
    from context import CameraSpec

    cfg = AppConfig(kaic_url="http://k", kaic_api_key="x", system_prompt="t",
                    cameras=[CameraSpec(camera_id="cam1", frame_url="http://x/1.jpg", role="front")])
    rt = CameraAgentRuntime(cfg)
    asked: list[str] = []

    async def vision(image, question):
        asked.append("person" if question == PERSON_PROMPT else "scene")
        return "gender=woman; age=40s; top=green coat" if question == PERSON_PROMPT else "A quiet hall."

    async def frame(cam, allow_pinned=True):
        return b"jpeg"

    class _Piper:
        async def synthesize(self, text):
            return b"RIFF" + b"\0" * 2000

        async def aclose(self):
            pass
    rt.tools._vlm_caption = vision
    rt.context.get_frame = frame
    rt.piper = _Piper()
    # As a context manager the client keeps ONE event loop for the whole
    # test, which the mirror's background task needs; a bare client runs
    # each request on a loop of its own and tears it down after.
    return rt, TestClient(build_app(rt)), asked


def test_mirror_page_is_served_without_a_token():
    rt, client, _ = _runtime_with_fakes()
    with client:
        r = client.get("/mirror")
    assert r.status_code == 200 and "OpenNVR Mirror" in r.text and "/updates" in r.text


def test_start_heartbeat_state_and_stop():
    rt, client, asked = _runtime_with_fakes()
    with client:
        _start_heartbeat_state_and_stop(client)


def _start_heartbeat_state_and_stop(client):
    assert client.post("/mirror/start", json={"camera": "nope"}).status_code == 404
    r = client.post("/mirror/start", json={"camera": "cam1", "voice": False})
    assert r.status_code == 200
    m = r.json()["mirror"]
    assert m["camera"] == "cam1" and m["running"] is True and m["voice"] is False
    r = client.post("/mirror/heartbeat", json={"voice": True})
    assert r.json() == {"running": True, "voice": True}
    st = client.get("/mirror/state").json()
    assert st["cameras"] == ["cam1"] and st["mirror"]["camera"] == "cam1"
    # No detect-pipeline origin configured: no crops, and the card falls
    # back to the whole frame in the loop.
    assert client.get("/mirror/crop/cam1/7").status_code == 404
    r = client.post("/mirror/stop")
    assert r.json() == {"stopped": True}
    assert client.get("/mirror/state").json()["mirror"]["running"] is False
    assert client.post("/mirror/heartbeat").json() == {"running": False}


def test_relayed_tracks_reach_the_running_mirror():
    rt, client, asked = _runtime_with_fakes()
    with client:
        client.post("/mirror/start", json={"camera": "cam1", "voice": False})
        rt.cfg.cameras[0].opennvr_camera_id = 3
        rt._relay_tracks(3, {"tracks": [{"id": 5, "label": "person", "score": .9, "box": [.1, .1, .4, .6]}]})
        assert 5 in rt.mirror.cards
        rt._relay_tracks(4, {"tracks": [{"id": 6, "label": "person", "score": .9, "box": [.1, .1, .4, .6]}]})
        assert 6 not in rt.mirror.cards, "another core camera is not this mirror's"
        client.post("/mirror/stop")


@pytest.mark.asyncio
async def test_a_rephrase_inside_the_hold_keeps_the_subtitle():
    fake = Fake(); s = _session(fake)
    await s.step()
    first = s.caption
    fake.now += 13.0                                 # empty view: the 12 s cadence
    fake.caption_text = "One man in a red shirt waves at the camera."
    assert await s.step() is True
    assert s.caption == first and s.stats.captions == 1
    assert fake.published[-1]["mirror"].get("same") is True
    fake.now += 13.0
    fake.caption_text = "Two women in green jackets carry a large box across the hall."
    await s.step()
    assert s.caption.startswith("Two women") and s.stats.captions == 2


@pytest.mark.asyncio
async def test_the_scene_prompt_names_what_is_tracked():
    fake = Fake(); s = _session(fake)
    s.observe_tracks({"tracks": [{"id": 1, "label": "person", "score": .9, "box": [.1, .1, .3, .6]},
                                 {"id": 2, "label": "car", "score": .8, "box": [.5, .5, .3, .3]}]})
    await s.step()
    assert "currently sees: 1 person, 1 car" in fake.prompts[-1]


# ── vocabulary, consensus, crops ────────────────────────────────────

def test_normalise_folds_into_the_vocabulary_and_drops_the_rest():
    f = normalise_fields({"gender": "Male", "age": "30-40", "top": "dark navy jacket", "bottom": "jeans",
                          "headwear": "baseball cap", "glasses": "yes", "carrying": "a mobile phone",
                          "expression": "grinning", "posture": "stands near the desk", "summary": "x"})
    assert f == {"gender": "man", "age": "30s", "top": "blue jacket", "bottom": "denim", "headwear": "cap",
                 "glasses": "yes", "carrying": "phone", "expression": "smiling", "posture": "standing"}
    assert normalise_fields({"gender": "robot", "age": "unknown", "top": "none", "carrying": "a strange device"}) == {}
    assert normalise_fields({"age": "65", "top": "red"}) == {"age": "60+", "top": "red"}
    assert normalise_fields({"age": "late teens", "glasses": "no"}) == {"age": "teen"}


def test_consensus_keeps_what_two_looks_agree_on():
    a = {"gender": "man", "age": "30s", "top": "blue jacket", "carrying": "phone", "expression": "smiling"}
    b = {"gender": "man", "age": "40s", "top": "blue", "carrying": "phone", "expression": "neutral"}
    assert consensus(a, b) == {"gender": "man", "top": "blue jacket", "carrying": "phone"}
    assert consensus({"top": "red shirt"}, {"top": "blue shirt"}) == {}, "a colour clash drops the garment too"
    assert consensus({"top": "shirt"}, {"top": "red shirt"}) == {"top": "red shirt"}
    assert person_summary({"gender": "woman", "top": "green coat", "posture": "waving"}) == "woman, green coat, waving"


def test_crop_person_cuts_the_box_with_a_margin_or_declines_a_tiny_one():
    import io
    from PIL import Image
    im = Image.new("RGB", (1920, 1080), (10, 20, 30)); buf = io.BytesIO(); im.save(buf, format="JPEG")
    jpeg = buf.getvalue()
    cut = crop_person(jpeg, [0.4, 0.2, 0.1, 0.5])            # 192 × 540 px box
    w, h = Image.open(io.BytesIO(cut)).size
    assert 270 <= w <= 300 and 790 <= h <= 820                 # 25 % margin each side
    assert crop_person(jpeg, [0.5, 0.5, 0.02, 0.05]) is None   # 38 × 54 px: too small
    assert crop_person(b"not a jpeg", [0, 0, 1, 1]) is None


@pytest.mark.asyncio
async def test_a_person_is_cut_from_the_full_resolution_frame_when_there_is_one():
    import io
    from PIL import Image
    im = Image.new("RGB", (1920, 1080), (200, 30, 30)); buf = io.BytesIO(); im.save(buf, format="JPEG")
    fake = Fake(); seen = []

    async def hires(cam):
        return buf.getvalue()

    async def vision(image, question):
        seen.append(len(image)); fake.now += 1.0
        return fake.person_text if question == PERSON_PROMPT else fake.caption_text
    s = MirrorSession("cam1", frame=fake.frame, crop=fake.crop, vision=vision, speech=None,
                      publish=fake.publish, clock=fake.clock, hires=hires)
    s.observe_tracks(_person_frame(3)); fake.now += 1.0; s.observe_tracks(_person_frame(3))
    await s.step()
    assert fake.crops == [], "the hi-res crop replaced Tier-0's best crop"
    assert 0 < seen[-1] < len(buf.getvalue()), "a crop, not the whole frame"

