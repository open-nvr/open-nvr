#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tier-0 detection recall harness.

Answers the question behind the whole tier-0 debate with a number: how much
does the cheap always-on detector actually SEE? Recall bounds the event index
and every app that rides the inference stream — a static person the detector
misses is an event that never gets remembered and an alert that never fires.

Point it at any detector that speaks the ``/infer`` contract (the tier-0
adapter, a YOLOv8 adapter) and a labelled image manifest; it reports presence
recall / precision per label, broken down by CONDITION so the hard cases
(static, edge-of-frame, close-range, low-light) show separately from the easy
ones — because the easy-case average hides exactly the failures QA hits.

Manifest (JSON):
    {"images": [
        {"path": "clips/static_person_01.jpg",
         "truth": {"person": 1}, "condition": "static"},
        {"path": "clips/empty_hall.jpg", "truth": {"person": 0}}]}

Usage:
    python bench/tier0_recall.py --url http://localhost:9108/infer \
        --manifest labels.json [--score 0.35]

The scoring helpers are stdlib-only and unit-tested (bench/test_bench.py);
the network/IO parts need a running detector + real images.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.request


# ── Pure scoring (unit-tested) ─────────────────────────────────────

def presence_scores(records: list[dict]) -> dict:
    """Per-label presence recall/precision from scored records.

    Each record: ``{"truth": {label: count}, "detected": {label: count},
    "condition": str}``. Presence = count > 0. Returns
    ``{label: {"recall","precision","support","detected"}}``.
    """
    labels: set[str] = set()
    for r in records:
        labels |= set(r["truth"]) | set(r["detected"])
    out: dict[str, dict] = {}
    for lab in sorted(labels):
        tp = fn = fp = 0
        for r in records:
            present = r["truth"].get(lab, 0) > 0
            found = r["detected"].get(lab, 0) > 0
            if present and found:
                tp += 1
            elif present and not found:
                fn += 1
            elif not present and found:
                fp += 1
        support = tp + fn
        out[lab] = {
            "recall": (tp / support) if support else None,
            "precision": (tp / (tp + fp)) if (tp + fp) else None,
            "support": support,
            "detected": tp + fp,
        }
    return out


def scores_by_condition(records: list[dict], label: str) -> dict:
    """Recall for one label split by each record's ``condition`` tag — the
    point of the harness: an overall 0.9 can hide a 0.4 on 'static'."""
    buckets: dict[str, list[dict]] = {}
    for r in records:
        if r["truth"].get(label, 0) > 0:
            buckets.setdefault(r.get("condition") or "unlabelled", []).append(r)
    out = {}
    for cond, rs in sorted(buckets.items()):
        tp = sum(1 for r in rs if r["detected"].get(label, 0) > 0)
        out[cond] = {"recall": tp / len(rs), "support": len(rs)}
    return out


# ── Detector call (needs a running /infer) ─────────────────────────

def _infer(url: str, image_bytes: bytes, score_min: float, timeout: float = 30.0) -> dict:
    body = json.dumps({"frame_b64": base64.b64encode(image_bytes).decode("ascii")}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    dets = (payload.get("result") or {}).get("detections") or []
    counts: dict[str, int] = {}
    for d in dets:
        if float(d.get("score", 0)) < score_min:
            continue
        lab = str(d.get("label") or d.get("class") or "").strip().lower()
        if lab:
            counts[lab] = counts.get(lab, 0) + 1
    return counts


def run(url: str, manifest_path: str, score_min: float) -> list[dict]:
    manifest = json.loads(open(manifest_path).read())
    records = []
    for item in manifest["images"]:
        try:
            img = open(item["path"], "rb").read()
        except OSError as e:
            print(f"skip {item['path']}: {e}", file=sys.stderr)
            continue
        detected = _infer(url, img, score_min)
        records.append({
            "truth": {k.lower(): int(v) for k, v in item.get("truth", {}).items()},
            "detected": detected,
            "condition": item.get("condition"),
        })
    return records


def _report(records: list[dict]) -> None:
    print(f"\nTier-0 recall over {len(records)} images\n" + "=" * 42)
    for lab, s in presence_scores(records).items():
        r = "n/a" if s["recall"] is None else f"{s['recall']:.2f}"
        p = "n/a" if s["precision"] is None else f"{s['precision']:.2f}"
        print(f"  {lab:10s}  recall {r}  precision {p}  (present in {s['support']})")
    by = scores_by_condition(records, "person")
    if by:
        print("\n  person recall by condition:")
        for cond, c in by.items():
            print(f"    {cond:14s} {c['recall']:.2f}  (n={c['support']})")
    print("\nLow recall on 'static'/'close'/'edge' is the blind spot that makes the")
    print("agent miss a sitting person and drops events from memory. Track it.\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier-0 detection recall harness")
    ap.add_argument("--url", required=True, help="Detector /infer endpoint")
    ap.add_argument("--manifest", required=True, help="Labelled image manifest (JSON)")
    ap.add_argument("--score", type=float, default=0.35, help="Min detection score")
    args = ap.parse_args(argv)
    records = run(args.url, args.manifest, args.score)
    if not records:
        print("no images scored", file=sys.stderr)
        return 1
    _report(records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
