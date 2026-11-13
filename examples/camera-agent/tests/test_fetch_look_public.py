# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tools/fetch_look_public.py and its manifest, with no network."""
from __future__ import annotations

import collections
import csv
import importlib.util
import pathlib
import sys

import pytest

_TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(_TOOLS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _TOOLS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


fl = _load("fetch_look_public")
le = _load("look_eval")
ROWS = fl.load_manifest()


def test_the_manifest_is_the_night_yard_set_it_says():
    count = collections.Counter(r["expect"] for r in ROWS)
    assert count == {"who=animal": 20, "who=stranger": 18, "who=nobody": 20}
    assert all(r["template"] == "yard_threat" for r in ROWS)
    assert len({r["file"] for r in ROWS}) == len(ROWS)
    assert all(("CC BY 4.0" in r["source"]) or ("CDLA-Permissive" in r["source"]) for r in ROWS)


def test_every_label_parses_against_the_template(tmp_path):
    for r in ROWS:
        (tmp_path / r["file"]).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / r["file"]).write_bytes(b"x")
    cases = le.load_cases(fl.write_cases(ROWS, tmp_path), le.load_templates(
        _TOOLS / "look_templates.yml"))
    assert len(cases) == len(ROWS)
    assert collections.Counter(c["decision"] for c in cases) == {"ignore": 40, "alert": 18}
    assert "Kitware" in (tmp_path / "ATTRIBUTION.txt").read_text()


def test_a_row_off_the_datasets_stores_is_refused(tmp_path):
    p = tmp_path / "m.csv"
    with p.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(ROWS[0]))
        w.writeheader()
        w.writerow({**ROWS[0], "url": "https://example.com/x.jpg"})
    with pytest.raises(ValueError, match="not one of the datasets' stores"):
        fl.load_manifest(p)


def test_each_clip_is_downloaded_once_and_its_frames_taken(tmp_path):
    got, frames = [], []

    def download(url, dest):
        got.append(url)
        dest.write_bytes(b"data")

    def grab(clip, dest, *, frame, seconds):
        frames.append((dest.name, frame, seconds))
        dest.write_bytes(b"jpg")

    meva = [r for r in ROWS if r["url"].endswith(".avi")]
    failed = fl.fetch(ROWS, tmp_path, download=download, grab_frame=grab)
    assert failed == []
    assert len(got) == len(ROWS) - len(meva) + len({r["url"] for r in meva})
    assert len(frames) == len(meva)
    # A second run fetches nothing: what is there is kept.
    got.clear()
    assert fl.fetch(ROWS, tmp_path, download=download, grab_frame=grab) == [] and got == []


def test_a_failure_is_reported_and_the_rest_carry_on(tmp_path):
    def download(url, dest):
        if "7150" in url:
            raise OSError("gone")
        dest.write_bytes(b"data")

    def grab(clip, dest, *, frame, seconds):
        dest.write_bytes(b"jpg")

    failed = fl.fetch(ROWS, tmp_path, download=download, grab_frame=grab)
    assert len(failed) == 1 and "gone" in failed[0]
    cases = (tmp_path / "cases.csv")
    fl.write_cases(ROWS, tmp_path)
    assert len(cases.read_text().splitlines()) == len(ROWS)   # header + all but one
