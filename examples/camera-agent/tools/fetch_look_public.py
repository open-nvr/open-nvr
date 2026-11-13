#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fetch the public Look cases: night-yard pictures from open datasets.

tools/look_cases_public.csv says where each picture comes from, so the
pictures themselves never enter this repository: anyone can rebuild the
same set, and each dataset's terms stay with its own download.

  ENA24 (LILA BC, CDLA-Permissive)   night infrared camera-trap animals
  MEVA (Kitware / IARPA, CC BY 4.0)  CCTV: people (daytime actors) and
                                     empty scenes (thermal, and midnight)

A MEVA case is one frame of a five-minute clip (100-230 MB): each clip is
downloaded once, its frames taken with ffmpeg (``frame`` by index, or
``seconds`` in), and the clip deleted. Pictures already there are kept,
so a run that stopped carries on.

    python tools/fetch_look_public.py --out ~/look-public
    python tools/look_eval.py --cases ~/look-public/cases.csv --model qwen3-vl:2b-instruct

These are a first number for the yard_threat step, not a site's gate:
the cameras are not the site's, the strangers are in daylight, and
public pictures may be in a model's training data (MODELS_AND_LATENCY.md).
"""
from __future__ import annotations

import argparse
import csv
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import defaultdict
from collections.abc import Callable

HERE = pathlib.Path(__file__).resolve().parent
MANIFEST = HERE / "look_cases_public.csv"
#: Where a manifest row may point: the two datasets' own public stores.
HOSTS = ("https://storage.googleapis.com/public-datasets-lila/ena24/",
         "https://mevadata-public-01.s3.amazonaws.com/")
ATTRIBUTION = """\
These pictures come from public datasets, under their own terms:

ENA24-detection, via LILA BC (https://lila.science/datasets/ena24detection),
Community Data License Agreement (permissive variant). Yousif H, Kays R,
Zhihai H. Dynamic Programming Selection of Object Proposals for
Sequence-Level Animal Species Classification in the Wild. IEEE Transactions
on Circuits and Systems for Video Technology, 2019.

"Multiview Extended Video with Activities" (MEVA) dataset by Kitware Inc.
and the Intelligence Advanced Research Projects Activity (IARPA), licensed
under CC BY 4.0 (https://mevadata.org). Frames were taken from its clips.
"""


def load_manifest(path: str | pathlib.Path = MANIFEST) -> list[dict[str, str]]:
    with pathlib.Path(path).open(newline="") as f:
        rows = list(csv.DictReader(f))
    for i, r in enumerate(rows, start=2):
        if not r["url"].startswith(HOSTS):
            raise ValueError(f"line {i}: {r['url']} is not one of the datasets' stores")
        if r["url"].endswith(".avi") and bool(r["frame"]) == bool(r["seconds"]):
            raise ValueError(f"line {i}: a clip case gives a frame or seconds, one of them")
    return rows


def download(url: str, dest: pathlib.Path) -> None:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=600) as r, tmp.open("wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)


def grab_frame(clip: pathlib.Path, dest: pathlib.Path, *, frame: str, seconds: str) -> None:
    where = (["-ss", seconds, "-i", str(clip)] if seconds
             else ["-i", str(clip), "-vf", f"select=eq(n\\,{int(frame)})"])
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *where, "-frames:v", "1",
                    "-q:v", "3", str(dest)], check=True)
    if not dest.is_file() or dest.stat().st_size == 0:
        raise RuntimeError(f"ffmpeg wrote no frame for {dest.name}")


def fetch(rows: list[dict[str, str]], out: pathlib.Path, *,
          download: Callable[[str, pathlib.Path], None] = download,
          grab_frame: Callable[..., None] = grab_frame,
          progress: Callable[[str], None] = lambda _: None) -> list[str]:
    """Fetch every missing picture; returns the files that could not be had."""
    failed: list[str] = []
    clips: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        dest = out / r["file"]
        if dest.is_file():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if r["url"].endswith(".avi"):
            clips[r["url"]].append(r)
            continue
        try:
            download(r["url"], dest)
            progress(r["file"])
        except Exception as err:  # noqa: BLE001 - one missing picture must not stop the rest
            failed.append(f"{r['file']}: {err}")
    for url, want in clips.items():
        with tempfile.TemporaryDirectory(dir=out) as tmp:
            clip = pathlib.Path(tmp) / "clip.avi"
            try:
                download(url, clip)
            except Exception as err:  # noqa: BLE001
                failed += [f"{r['file']}: {err}" for r in want]
                continue
            for r in want:
                try:
                    grab_frame(clip, out / r["file"], frame=r["frame"], seconds=r["seconds"])
                    progress(r["file"])
                except Exception as err:  # noqa: BLE001
                    failed.append(f"{r['file']}: {err}")
    return failed


def write_cases(rows: list[dict[str, str]], out: pathlib.Path) -> pathlib.Path:
    """The look_eval.py labels for the pictures that are there."""
    path = out / "cases.csv"
    with path.open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["file", "template", "expect", "inputs", "note"])
        for r in rows:
            if (out / r["file"]).is_file():
                w.writerow([r["file"], r["template"], r["expect"], "",
                            f"{r['note']} [{r['source']}]"])
    (out / "ATTRIBUTION.txt").write_text(ATTRIBUTION)
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, help="where the pictures go (outside the repo)")
    ap.add_argument("--manifest", default=str(MANIFEST))
    a = ap.parse_args(argv)
    if shutil.which("ffmpeg") is None:
        ap.error("ffmpeg is needed for the MEVA frames")
    out = pathlib.Path(a.out).expanduser()
    out.mkdir(parents=True, exist_ok=True)
    rows = load_manifest(a.manifest)
    failed = fetch(rows, out, progress=lambda f: print(f, file=sys.stderr, flush=True))
    cases = write_cases(rows, out)
    print(f"{len(rows) - len(failed)}/{len(rows)} pictures; labels in {cases}")
    for f in failed:
        print(f"  missing {f}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
