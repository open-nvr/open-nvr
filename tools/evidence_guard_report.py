#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The evidence guard, read back from a debug dump.

The detect-pipeline writes every main-stream evidence attempt to
``DETECT_EVIDENCE_DEBUG_DIR/<camera>/`` as a pair of bare-box crops (the
sub frame's and the main frame's) plus a JSON record: the match score, the
time between the two frames, the refusal reason, the reference-frame
similarities and the crops' texture. This tool reads a dump and says what
the guard did and what it would have done at other floors:

    python3 tools/evidence_guard_report.py /path/to/dump            # all cameras
    python3 tools/evidence_guard_report.py /path/to/dump --sheet out.jpg   # plus a contact sheet

It needs only python3; the contact sheet needs OpenCV (skipped without it).
Copy a dump out of the container with ``docker cp opennvr_detect_pipeline:/tmp/evidence-dump .``.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

FLOORS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8)


def load(dump: Path) -> list[dict]:
    recs = []
    for f in sorted(dump.rglob("*.json")):
        try:
            r = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        r["_camera"] = f.parent.name
        r["_stem"] = str(f)[:-5]
        recs.append(r)
    return recs


def _q(vals, q):
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(q * len(vals)))] if vals else None


def summarise(recs: list[dict]) -> dict:
    """Pure: counts by reason, score/skew distributions, and what each
    candidate floor would have refused among the scored attempts."""
    by_reason: dict[str, int] = {}
    for r in recs:
        key = r.get("reason") or ("accepted" if r.get("accepted") else "?")
        by_reason[key] = by_reason.get(key, 0) + 1
    scored = [r for r in recs if r.get("score") is not None]
    acc = [r["score"] for r in scored if r.get("accepted")]
    ref = [r["score"] for r in scored if not r.get("accepted")]
    skews = [r["skew_s"] for r in recs if r.get("skew_s") is not None]
    # score against skew: does the score fall with the time gap? (timing)
    near = [r["score"] for r in scored if (r.get("skew_s") or 0) <= 0.5]
    far = [r["score"] for r in scored if (r.get("skew_s") or 0) > 1.0]
    floors = {f: sum(1 for r in scored if r["score"] < f) for f in FLOORS}
    refs = [(r["sim_main_ref"], r["sim_sub_ref"]) for r in recs
            if r.get("sim_main_ref") is not None and r.get("sim_sub_ref") is not None]
    # background-dominated boxes: accepted, yet the main crop looks like the
    # empty scene too — the compare said nothing about the subject
    bg = sum(1 for r in recs if r.get("accepted") and (r.get("sim_main_ref") or 0) >= 0.8
             and (r.get("sim_sub_ref") or 0) >= 0.8)
    return {
        "attempts": len(recs), "by_reason": by_reason,
        "score_accepted": {"n": len(acc), "p10": _q(acc, 0.1), "median": _q(acc, 0.5)},
        "score_refused": {"n": len(ref), "median": _q(ref, 0.5), "p90": _q(ref, 0.9)},
        "skew_s": {"n": len(skews), "median": _q(skews, 0.5), "p90": _q(skews, 0.9)},
        "score_near_vs_far": {"near_median": _q(near, 0.5), "far_median": _q(far, 0.5),
                              "near_n": len(near), "far_n": len(far)},
        "refused_at_floor": floors, "scored": len(scored),
        "reference_pairs": len(refs), "accepted_but_background_like": bg,
    }


def render(summary: dict, camera: str) -> str:
    s = summary
    lines = [f"== {camera}: {s['attempts']} evidence attempts",
             "   outcome: " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_reason"].items()))]
    a, r = s["score_accepted"], s["score_refused"]
    if a["n"]:
        lines.append(f"   accepted scores: n {a['n']}, p10 {a['p10']:.2f}, median {a['median']:.2f}")
    if r["n"]:
        lines.append(f"   refused scores:  n {r['n']}, median {r['median']:.2f}, p90 {r['p90']:.2f}")
    if s["skew_s"]["n"]:
        lines.append(f"   main-to-detect time gap: median {s['skew_s']['median']:.2f} s, p90 {s['skew_s']['p90']:.2f} s")
    nf = s["score_near_vs_far"]
    if nf["near_n"] and nf["far_n"]:
        lines.append(f"   score when the gap is ≤0.5 s: median {nf['near_median']:.2f} ({nf['near_n']}); "
                     f">1 s: median {nf['far_median']:.2f} ({nf['far_n']})"
                     + ("  → timing drives the refusals" if nf["near_median"] - nf["far_median"] > 0.2 else ""))
    if s["scored"]:
        lines.append("   would refuse at floor: " + ", ".join(
            f"{f:.1f} → {n} ({100 * n / s['scored']:.0f} %)" for f, n in s["refused_at_floor"].items()))
    if s["reference_pairs"]:
        lines.append(f"   reference frames measured on {s['reference_pairs']} attempts; accepted but background-like: "
                     f"{s['accepted_but_background_like']}")
    return "\n".join(lines)


def contact_sheet(recs: list[dict], out: Path, per_reason: int = 12, cell: int = 96) -> bool:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False
    groups: dict[str, list[dict]] = {}
    for r in recs:
        key = r.get("reason") or ("accepted" if r.get("accepted") else "?")
        groups.setdefault(key, []).append(r)
    rows = []
    for key, items in sorted(groups.items()):
        row = []
        for r in items[:per_reason]:
            pair = []
            for suffix in ("_sub.jpg", "_main.jpg"):
                img = cv2.imread(r["_stem"] + suffix)
                if img is None:
                    img = np.zeros((cell, cell, 3), np.uint8)
                pair.append(cv2.resize(img, (cell, cell)))
            tile = np.hstack(pair)
            label = f"{key} {r.get('score') if r.get('score') is not None else '-'}"
            cv2.putText(tile, str(label)[:18], (2, 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 255, 255), 1)
            row.append(tile)
        while len(row) < per_reason:
            row.append(np.zeros((cell, 2 * cell, 3), np.uint8))
        rows.append(np.hstack(row))
    if not rows:
        return False
    cv2.imwrite(str(out), np.vstack(rows))
    return True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dump", help="DETECT_EVIDENCE_DEBUG_DIR as copied out of the container")
    p.add_argument("--sheet", default=None, help="write a contact sheet (sub | main pairs by outcome) to this JPEG")
    args = p.parse_args(argv)
    recs = load(Path(args.dump))
    if not recs:
        print("no attempts found under", args.dump)
        return 1
    cams = sorted({r["_camera"] for r in recs})
    for cam in cams:
        print(render(summarise([r for r in recs if r["_camera"] == cam]), cam))
    if len(cams) > 1:
        print(render(summarise(recs), "all cameras"))
    if args.sheet:
        print("contact sheet:", args.sheet if contact_sheet(recs, Path(args.sheet)) else "skipped (OpenCV not available)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
