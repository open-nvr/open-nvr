#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The difference between the first and last snapshot of a traffic window.

    python3 tools/rfc0004_window_diff.py /tmp/paired/window-A.jsonl

Input is what tools/rfc0004_window.sh appends, one JSON line per
snapshot. Output: the captioner's calls, time and mean per task; the
detector's inferences and mean latency per camera; visits, captions and
descriptor rows written; all over the window, plus derived per-visit
figures. Prints a markdown table so it pastes into the ledger as is.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime


def main(path: str) -> int:
    rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    if len(rows) < 2:
        print("need two snapshots", file=sys.stderr)
        return 1
    a, b = rows[0], rows[-1]
    t0 = datetime.fromisoformat(a["ts"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
    secs = (t1 - t0).total_seconds()
    print(f"window {a['ts']} → {b['ts']} ({secs / 60:.1f} min); load {a['load']} → {b['load']}\n")

    print("| Measurement | Value |\n|---|---|")
    total_calls = total_time = 0.0
    tasks = sorted(set(a["adapter"].get("infer_total", {})) | set(b["adapter"].get("infer_total", {})))
    for task in tasks:
        n = b["adapter"]["infer_total"].get(task, 0) - a["adapter"]["infer_total"].get(task, 0)
        s = b["adapter"]["latency_sum_s"].get(task, 0) - a["adapter"]["latency_sum_s"].get(task, 0)
        total_calls += n
        total_time += s
        name = task or "vqa (question or structured)"
        print(f"| captioner {name}: calls | {n:.0f} |")
        print(f"| captioner {name}: time, mean | {s:.0f} s, {s / n * 1000 if n else 0:.0f} ms |")
    print(f"| captioner all tasks: calls, time, share of window | {total_calls:.0f}, {total_time:.0f} s, "
          f"{total_time / secs * 100 if secs else 0:.0f} % |")
    chars = b["adapter"].get("generated_chars", 0) - a["adapter"].get("generated_chars", 0)
    print(f"| captioner characters generated | {chars:.0f} |")
    for cam in sorted(b.get("detector", {})):
        dc = b["detector"][cam]["count"] - a["detector"].get(cam, {}).get("count", 0)
        ds = b["detector"][cam]["sum"] - a["detector"].get(cam, {}).get("sum", 0)
        print(f"| detector {cam}: inferences, mean latency | {dc:.0f}, {ds / dc * 1000 if dc else 0:.1f} ms |")
    db = {k: b["db"].get(k, 0) - a["db"].get(k, 0) for k in ("visits", "captions", "descriptor_rows")}
    for k, v in db.items():
        print(f"| {k} written | {v} |")
    if db["visits"]:
        print(f"| captioner calls per visit | {total_calls / db['visits']:.2f} |")
        print(f"| captioner seconds per visit | {total_time / db['visits']:.2f} |")
        print(f"| descriptor rows per visit | {db['descriptor_rows'] / db['visits']:.2f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/paired/window-A.jsonl"))
