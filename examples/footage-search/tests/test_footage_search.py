# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for the query parser and the search path onto the event store.

The index is gone, and with it most of what this file used to hold:
keyframe extraction, coalescing windows, retention pruning, Tier-0
frame walking. All of that was the cost of keeping a second copy of the
platform's data, and none of it is this app's job any more.

What is left is the part that was always its own — turning a sentence a
human typed into a structured query — plus the seams where that meets
core. Those seams get the attention, because the app's whole surface is
now one call to somebody else's store, and the two ways that call can
fail to mean what it says.
"""
from __future__ import annotations

import datetime as _dt
import inspect

import pytest
from opennvr_app_sdk.client import Camera, TimelineAPI

from footage_search import (AppConfig, CameraNotHeld, FootageSearch, Hit,
                            OllamaConfig, StoreUnreachable, format_results,
                            resolve_camera, run_search)
from query import parse_heuristic

NOW = _dt.datetime(2026, 6, 14, 12, 0, 0, tzinfo=_dt.timezone.utc)

#: Bound against the real client, so a rename in the SDK breaks these
#: tests instead of quietly making the fake accept a call no client can
#: take. That exact fake — ``def find(self, *a, **kw)`` — is how the
#: camera-agent shipped a call to a method that did not exist and had it
#: read as working for a month.
_REAL_FIND = inspect.signature(TimelineAPI.find)


class _Timeline:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def find(self, *a, **kw):
        bound = _REAL_FIND.bind(self, *a, **kw)
        bound.apply_defaults()
        self.calls.append({k: v for k, v in list(bound.arguments.items())[1:]})
        return self.answer


def _camera(cid, name):
    """A roster entry as core reports one."""
    return Camera(id=cid, handle=f"cam{cid}", name=name, role=name,
                  frame_url=f"http://core/frames/{cid}")


class _Client:
    """Stands in for ``OpenNVR``, reduced to what this app asks of it."""

    def __init__(self, answer=None, cameras=()):
        self.timeline = _Timeline(answer)
        self._cameras = list(cameras)

    def cameras(self):
        return self._cameras


def _cfg(**over):
    base = dict(extra_labels=[], camera_aliases={}, ollama=OllamaConfig(),
                result_limit=25, opennvr_url="http://core")
    base.update(over)
    return AppConfig(**base)


def _row(**over):
    row = {
        "id": 8140, "camera_id": 3, "camera_name": "Dock", "label": "truck",
        "started_at": "2026-06-13T14:22:08+00:00",
        "caption": "a red truck parked near a loading dock",
        "plate_text": None, "has_evidence": False, "claims": [],
    }
    row.update(over)
    return row


def _answer(*rows):
    return {"results": list(rows), "count": len(rows), "total": len(rows),
            "answer": {}}


# ── Query parser (unchanged by the port) ───────────────────────────


def test_parses_label_keyword_and_time():
    qf = parse_heuristic(
        "show me every red truck at the dock yesterday",
        now=NOW, camera_aliases={"dock": "Dock"},
    )
    assert "truck" in qf.labels
    assert "red" in qf.keywords
    assert qf.camera_id == "Dock"
    assert qf.since is not None and qf.until is not None
    y = (NOW - _dt.timedelta(days=1)).date()
    assert _dt.datetime.fromtimestamp(qf.since, _dt.timezone.utc).date() == y


def test_parses_rolling_window():
    qf = parse_heuristic("people in the last 30 minutes", now=NOW)
    assert "person" in qf.labels          # "people" → person alias
    assert qf.since is not None
    assert abs((NOW.timestamp() - qf.since) - 1800) < 2


def test_descriptor_only_query_has_no_labels():
    qf = parse_heuristic("anyone in a yellow jacket today", now=NOW)
    assert qf.labels == []                # no object class named
    assert "yellow" in qf.keywords and "jacket" in qf.keywords
    assert qf.since is not None           # today window


# ── The parsed query reaches the store intact ──────────────────────


def test_the_headline_query_end_to_end():
    """"red truck at the dock yesterday" — the query on the tin.

    The label goes out as a label, the descriptor as search text, the
    camera as an id resolved from the roster, and the day as a window.
    Each of those used to be a column in this app's own database.
    """
    client = _Client(answer=_answer(_row()),
                     cameras=[_camera(3, "Dock")])
    hits = run_search(_cfg(camera_aliases={"dock": "Dock"}), client,
                      "red truck at the dock yesterday")

    call = client.timeline.calls[0]
    assert call["label"] == ["truck"]
    assert "red" in call["text"]
    assert call["camera"] == [3]
    assert call["start"] is not None and call["end"] is not None
    assert len(hits) == 1
    assert hits[0].caption.startswith("a red truck")


def test_a_descriptor_only_query_sends_no_label_filter():
    """``label=[]`` would be a filter matching nothing. It has to be
    ``None`` — no filter at all — or "anyone in a yellow jacket" comes
    back empty against a store that holds exactly that."""
    client = _Client(answer=_answer())
    run_search(_cfg(), client, "anyone in a yellow jacket today")
    assert client.timeline.calls[0]["label"] is None


def test_a_query_with_no_time_words_sends_no_window():
    client = _Client(answer=_answer())
    run_search(_cfg(), client, "red truck")
    call = client.timeline.calls[0]
    assert call["start"] is None and call["end"] is None


def test_the_result_limit_is_honoured():
    client = _Client(answer=_answer())
    run_search(_cfg(result_limit=7), client, "red truck")
    assert client.timeline.calls[0]["limit"] == 7
    run_search(_cfg(result_limit=7), client, "red truck", limit=3)
    assert client.timeline.calls[1]["limit"] == 3


# ── What a visit carries that a keyframe never could ───────────────


def test_a_hit_carries_the_plate_and_the_photo():
    """The plate and the evidence frame are on the VISIT. Neither
    existed on the inference bus, so neither could ever have been in the
    old index — this is the part of the port that adds rather than
    removes."""
    client = _Client(answer=_answer(
        _row(plate_text="KA01AB1234", has_evidence=True)))
    hit = run_search(_cfg(), client, "red truck")[0]

    assert hit.plate == "KA01AB1234"
    assert hit.has_evidence is True
    rendered = format_results([hit])
    assert "plate KA01AB1234" in rendered
    assert "photo kept" in rendered


def test_a_skill_claim_becomes_a_label():
    """A colour claim from an enrichment skill shows beside the object
    class, so "red truck" reads back as "truck red" rather than as a
    caption the operator has to squint at."""
    client = _Client(answer=_answer(_row(claims=[
        {"kind": "colour", "value": "red", "confidence": 0.8},
        {"kind": "face_id", "value": "someone", "confidence": 0.9},
    ])))
    hit = run_search(_cfg(), client, "red truck")[0]
    assert hit.labels == ["truck", "red"]
    assert "someone" not in hit.labels, (
        "only colour claims become labels; anything else riding along "
        "would put an identity claim in a list rendered to every viewer")


# ── The two failures that are not 'nothing matched' ────────────────


def test_an_unreachable_store_is_not_reported_as_no_matches():
    """The worst answer this app can give is that no red truck came
    past, when the truth is that nobody was able to look. ``find``
    returns ``None`` for that, and ``None`` must not become ``[]``."""
    client = _Client(answer=None)
    with pytest.raises(StoreUnreachable):
        run_search(_cfg(), client, "red truck")


def test_an_empty_result_is_a_real_answer():
    """The other half of the same distinction: core answered, and the
    answer was nothing. That is not an error."""
    client = _Client(answer=_answer())
    assert run_search(_cfg(), client, "red truck") == []


def test_a_camera_the_app_does_not_hold_is_refused_not_widened():
    """An alias naming a camera outside the roster must not fall back to
    searching every camera. The operator asked about the gate; answering
    with the loading dock is worse than refusing."""
    client = _Client(answer=_answer(_row()),
                     cameras=[_camera(3, "Dock")])
    with pytest.raises(CameraNotHeld):
        run_search(_cfg(camera_aliases={"gate": "Gate"}), client,
                   "red truck at the gate")
    assert client.timeline.calls == [], (
        "the query went to the store anyway, unscoped")


# ── Camera resolution ──────────────────────────────────────────────


@pytest.mark.parametrize("alias", ["Dock", "dock", "DOCK", "3", "cam3", "cam-3"])
def test_an_alias_resolves_by_name_id_or_handle(alias):
    client = _Client(cameras=[_camera(3, "Dock")])
    assert resolve_camera(client, alias) == [3]


def test_an_alias_matches_nothing_outside_the_roster():
    client = _Client(cameras=[_camera(3, "Dock")])
    assert resolve_camera(client, "Gate") == []
    assert resolve_camera(client, "9") == []


# ── The operator action ────────────────────────────────────────────


def _app(client):
    return FootageSearch(_cfg(camera_aliases={"dock": "Dock"}), client=client)


def test_the_search_action_returns_catalog_renderable_rows():
    app = _app(_Client(answer=_answer(_row(plate_text="KA01AB1234")),
                       cameras=[_camera(3, "Dock")]))
    out = app.on_action("search", {"query": "red truck", "limit": 5})

    assert out["query"] == "red truck"
    assert len(out["results"]) == 1
    row = out["results"][0]
    assert row["camera"] == "Dock"
    assert row["event_id"] == 8140
    assert row["plate"] == "KA01AB1234"
    assert "red truck" in row["caption"]


def test_the_search_action_validates_params():
    app = _app(_Client(answer=_answer()))
    with pytest.raises(ValueError, match="non-empty"):
        app.on_action("search", {"query": "   "})
    with pytest.raises(ValueError, match="between 1 and 200"):
        app.on_action("search", {"query": "x", "limit": 0})
    with pytest.raises(ValueError, match="whole number"):
        app.on_action("search", {"query": "x", "limit": "soon"})
    with pytest.raises(KeyError):
        app.on_action("enroll-face", {})


def test_the_state_records_hit_counts_and_never_the_words():
    """``/state`` is shown to every operator, and a query may have come
    from someone's voice assistant. The count is safe; the sentence is
    not."""
    app = _app(_Client(answer=_answer(_row())))
    app.on_action("search", {"query": "anyone in a yellow jacket"})

    state = app.state_snapshot()
    assert state["searches"] == 1
    assert state["recent"][0]["message"] == "search: 1 hit"
    assert "jacket" not in str(state), "the query text reached /state"


def test_an_outage_shows_in_the_catalog_and_does_not_count_as_a_search():
    app = _app(_Client(answer=None))
    assert app.not_ready_reason() is None, "not asked yet is not broken"

    with pytest.raises(StoreUnreachable):
        app.on_action("search", {"query": "red truck"})

    assert "did not answer" in (app.not_ready_reason() or "")
    assert app.state_snapshot()["searches"] == 0
    assert app.state_snapshot()["store_status"] == "not answering"


def test_a_successful_search_clears_the_outage():
    client = _Client(answer=None)
    app = _app(client)
    with pytest.raises(StoreUnreachable):
        app.on_action("search", {"query": "red truck"})

    client.timeline.answer = _answer(_row())
    app.on_action("search", {"query": "red truck"})
    assert app.not_ready_reason() is None
    assert app.state_snapshot()["store_status"] == "answering"


# ── Config ─────────────────────────────────────────────────────────


def test_the_store_url_is_required(tmp_path):
    """Every other example app treats ``opennvr_url`` as optional
    because it can still do its job standalone. This one cannot: the
    store IS the app, and starting without it would produce something
    that serves /health and answers nothing."""
    from footage_search import load_config

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text("result_limit: 25\n")
    with pytest.raises(ValueError, match="opennvr_url"):
        load_config(str(cfg_file))


def test_a_config_with_a_store_loads(tmp_path):
    from footage_search import load_config

    cfg_file = tmp_path / "config.yml"
    cfg_file.write_text(
        "opennvr_url: http://core:8000\n"
        "camera_aliases:\n  dock: Dock\n"
        "extra_labels: [forklift]\n")
    cfg = load_config(str(cfg_file))
    assert cfg.opennvr_url == "http://core:8000"
    assert cfg.camera_aliases == {"dock": "Dock"}
    assert cfg.extra_labels == ["forklift"]


# ── Rendering ──────────────────────────────────────────────────────


def test_nothing_found_says_so():
    assert format_results([]) == "No matching footage found."


def test_a_hit_with_no_camera_name_still_renders():
    """``camera_name`` comes from a lookup that can miss — a camera
    deleted since the visit. The row is still a real visit and must not
    render as a crash or a blank."""
    hit = Hit(event_id=1, camera_id=4, camera_name=None,
              when="2026-06-13T14:22:08+00:00", labels=["truck"],
              caption="", plate=None, has_evidence=False)
    rendered = format_results([hit])
    assert "camera 4" in rendered
    assert "event #1" in rendered
