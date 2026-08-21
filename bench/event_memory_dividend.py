#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event-memory dividend — what the event store is WORTH.

The other half of the tier-0 debate: the memory. A look-only agent (one that
grabs a frame and runs the VLM only when asked) sees nothing between questions.
Every event the store holds is something that agent would have missed — unless
a human happened to be asking at that exact second, which for overnight and
quiet hours is essentially never.

This queries the events API over a window and reports the dividend: how many
events were remembered, by label and by hour, with the UNATTENDED hours called
out — those are the clearest "would have been lost without the store" set.

Usage:
    python bench/event_memory_dividend.py --url http://localhost:8000 \
        --token "$JWT" --days 7 [--attended 8 18]

The summariser is stdlib-only and unit-tested (bench/test_bench.py); the fetch
needs a running backend.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone


# ── Pure summary (unit-tested) ─────────────────────────────────────

def summarize_events(events: list[dict], attended: tuple[int, int] = (8, 18)) -> dict:
    """Aggregate events into the memory dividend.

    ``attended`` is the [start, end) local-hour window a human plausibly might
    be watching live; events OUTSIDE it are the clearest dividend (no one was
    around to ask). Each event needs a ``started_at`` ISO string and a
    ``label``. Returns totals, per-label, per-hour, and the unattended count.
    """
    lo, hi = attended
    by_label: dict[str, int] = {}
    by_hour: dict[int, int] = {h: 0 for h in range(24)}
    unattended = 0
    total = 0
    for e in events:
        ts = e.get("started_at")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except ValueError:
            continue
        total += 1
        lab = str(e.get("label") or "?").lower()
        by_label[lab] = by_label.get(lab, 0) + 1
        h = dt.hour
        by_hour[h] += 1
        if not (lo <= h < hi):
            unattended += 1
    return {
        "total": total,
        "by_label": dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        "by_hour": by_hour,
        "unattended": unattended,
        "attended_window": [lo, hi],
    }


# ── Fetch (needs a running backend) ────────────────────────────────

def fetch_events(url: str, token: str | None, days: int, timeout: float = 30.0) -> list[dict]:
    frm = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    q = f"{url.rstrip('/')}/api/v1/events?from={frm}&limit=100000"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(q, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    return payload.get("events") or []


def _report(summary: dict, days: int) -> None:
    lo, hi = summary["attended_window"]
    print(f"\nEvent-memory dividend over {days} day(s)\n" + "=" * 42)
    print(f"  {summary['total']} events remembered — every one a moment a")
    print("  look-only agent would not have captured.\n")
    print("  by label:")
    for lab, n in summary["by_label"].items():
        print(f"    {lab:10s} {n}")
    print(f"\n  {summary['unattended']} of them fell OUTSIDE {lo:02d}:00-{hi:02d}:00")
    print("  (unattended hours) — the clearest dividend: no one was there to ask.")
    peak = max(summary["by_hour"].items(), key=lambda kv: kv[1], default=(0, 0))
    print(f"\n  busiest hour: {peak[0]:02d}:00 with {peak[1]} events")
    print("\nIf this number is large, the store is earning its keep: it is the")
    print("memory a query-only agent structurally cannot have.\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Event-memory dividend")
    ap.add_argument("--url", required=True, help="Backend base URL")
    ap.add_argument("--token", default=None, help="Bearer JWT (if required)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--attended", type=int, nargs=2, default=(8, 18),
                    metavar=("START_HOUR", "END_HOUR"),
                    help="Local-hour window a human might watch live (default 8 18)")
    args = ap.parse_args(argv)
    try:
        events = fetch_events(args.url, args.token, args.days)
    except Exception as e:  # noqa: BLE001
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    _report(summarize_events(events, tuple(args.attended)), args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
