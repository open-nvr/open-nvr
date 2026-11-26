# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The live overlay names its layers.

The live view lets a viewer switch layers of boxes on and off. Without a
``layer`` on each box it grouped them by label, and guard scan's labels
carry live numbers ("Scanning — 0%", "Scanning LRFB 100%"), so one kind
of box showed up as several layers, each with a number in its name.

SDK-free like the rest of this suite: ``publish_overlay`` is read out of
the app module's source and bound to stubs.
"""
from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

APP_SRC = (Path(__file__).resolve().parents[1]
           / "guard_scan_compliance.py").read_text(encoding="utf-8")
NL = chr(10)


def _slice(start: str, end: str) -> str:
    a = APP_SRC.rindex(NL, 0, APP_SRC.index(start)) + 1
    b = APP_SRC.rindex(NL, 0, APP_SRC.index(end, a)) + 1
    return textwrap.dedent(APP_SRC[a:b])


def _constants() -> dict:
    ns: dict = {}
    for name in ("LAYER_GUARD", "LAYER_SCREENING", "LAYER_FLAGGED", "LAYER_PEOPLE"):
        m = re.search(rf'^{name} = "([^"]+)"', APP_SRC, re.M)
        assert m, f"{name} is not defined in the app"
        ns[name] = m.group(1)
    return ns


class _Events:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def publish_overlay(self, camera_id, boxes, seq=None):
        self.sent.append({"camera": camera_id, "boxes": boxes})


class _Session:
    def __init__(self, done, score, flagged=False) -> None:
        self.done, self._score, self.flagged = set(done), score, flagged

    def result(self):
        return {"score": self._score}


def _publish(bodies, engine):
    ns = {"log": SimpleNamespace(debug=lambda *a, **k: None), **_constants()}
    exec(_slice("    def publish_overlay(self", "    # ── state the catalog shows ──"), ns)
    events = _Events()
    app = SimpleNamespace(events=events)
    frame = SimpleNamespace(width=100, height=100, seq=1)
    ns["publish_overlay"](app, "cam1", bodies, engine, frame)
    return {b["id"]: b for b in events.sent[0]["boxes"]}


def _body(track_id):
    return SimpleNamespace(track_id=track_id, box=(10, 10, 30, 60))


def test_every_box_names_a_layer_that_does_not_carry_numbers():
    from guard_scan.core import STEPS

    engine = SimpleNamespace(guard_id=1, sessions={
        2: _Session([], 0),
        3: _Session(STEPS, 100),
        4: _Session(STEPS[:1], 40, flagged=True),
    })
    boxes = _publish([_body(1), _body(2), _body(3), _body(4), _body(5)], engine)
    c = _constants()
    assert boxes[1]["layer"] == c["LAYER_GUARD"]
    # Two people being screened, at different stages: one layer.
    assert boxes[2]["layer"] == boxes[3]["layer"] == c["LAYER_SCREENING"]
    assert boxes[2]["label"] != boxes[3]["label"]
    # A flag is its own layer: hiding the screening boxes never hides it.
    assert boxes[4]["layer"] == c["LAYER_FLAGGED"]
    assert boxes[4]["label"].startswith("FLAGGED")
    assert boxes[5]["layer"] == c["LAYER_PEOPLE"]
    for b in boxes.values():
        assert not re.search(r"\d", b["layer"])
        assert len(b["layer"]) <= 40          # core caps layer names at 40
