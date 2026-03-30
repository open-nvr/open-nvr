# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

import json
import os
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Response
from fastapi.responses import JSONResponse

from core.config import settings

# Suricata log paths are configurable via environment (see server/config.py)
WSL_EVE_PATH = settings.suricata_eve_path
WSL_FASTLOG_PATH = settings.suricata_fastlog_path

router = APIRouter(prefix="/suricata", tags=["suricata"])


def _read_lines(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read().splitlines()


def _parse_eve_lines(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            obj = json.loads(line)
            events.append(obj)
        except Exception:
            # skip invalid lines
            continue
    return events


_FAST_RE = re.compile(
    r"^(?P<ts>\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s+\[\*\*\]\s+\[(?P<sid>\d+):(?P<gid>\d+):(?P<rev>\d+)\]\s+(?P<sig>.*?)\s+\[\*\*\]\s+\[Classification:\s*(?P<class>.*?)\]\s+\[Priority:\s*(?P<prio>\d+)\]\s+\{(?P<proto>[^}]+)\}\s+(?P<src_ip>[^:]+):(?P<src_port>\d+)\s+->\s+(?P<dst_ip>[^:]+):(?P<dst_port>\d+)"
)


def _parse_fast_lines(lines: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        m = _FAST_RE.search(line)
        if m:
            d = m.groupdict()
            items.append(
                {
                    "timestamp": d.get("ts"),
                    "sid": int(d["sid"]) if d.get("sid") else None,
                    "rev": int(d["rev"]) if d.get("rev") else None,
                    "signature": d.get("sig"),
                    "classification": d.get("class"),
                    "priority": int(d["prio"]) if d.get("prio") else None,
                    "protocol": d.get("proto"),
                    "src_ip": d.get("src_ip"),
                    "src_port": int(d["src_port"]) if d.get("src_port") else None,
                    "dst_ip": d.get("dst_ip"),
                    "dst_port": int(d["dst_port"]) if d.get("dst_port") else None,
                    "raw": line,
                }
            )
        else:
            items.append({"raw": line})
    return items


@router.get("/logs", response_class=Response)
def get_suricata_logs_raw():
    """Backward-compatible raw eve.json NDJSON response used by early UI."""
    if not os.path.exists(WSL_EVE_PATH):
        return Response(content="Log file not found.", status_code=404)
    with open(WSL_EVE_PATH, encoding="utf-8", errors="ignore") as f:
        data = f.read()
    # Keep text to allow client-side line-splitting and JSON.parse per line
    return Response(content=data, media_type="text/plain; charset=utf-8")


@router.get("/logs/eve")
def get_suricata_eve_logs(
    limit: int = Query(200, ge=1, le=5000),
    skip: int = Query(0, ge=0),
    only_alerts: bool = Query(
        False, description="Return only entries with 'event_type=alert'"
    ),
):
    lines = _read_lines(WSL_EVE_PATH)
    total_lines = len(lines)
    if skip > 0:
        window = lines[max(0, total_lines - (skip + limit)) : total_lines - skip]
    else:
        window = lines[-limit:]
    events = _parse_eve_lines(window)
    if only_alerts:
        events = [e for e in events if e.get("event_type") == "alert"]
    return JSONResponse(content={"items": events, "total": total_lines})


@router.get("/logs/fast")
def get_suricata_fast_logs(
    limit: int = Query(200, ge=1, le=5000),
    skip: int = Query(0, ge=0),
):
    lines = _read_lines(WSL_FASTLOG_PATH)
    total_lines = len(lines)
    if skip > 0:
        window = lines[max(0, total_lines - (skip + limit)) : total_lines - skip]
    else:
        window = lines[-limit:]
    items = _parse_fast_lines(window)
    return JSONResponse(content={"items": items, "total": total_lines})


def _to_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Suricata eve typically uses ISO 8601 with Z
        # Example: 2025-10-01T12:34:56.789012+0000 or 2025-10-01T12:34:56.789012Z
        # Normalize timezone format
        v = value.replace("+0000", "+00:00")
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    except Exception:
        return None


@router.get("/stats")
def get_suricata_stats(
    start: str | None = Query(None, description="ISO start timestamp to filter events"),
    end: str | None = Query(None, description="ISO end timestamp to filter events"),
    limit: int = Query(
        10000, ge=10, le=200000, description="Max lines to scan from end of eve.json"
    ),
):
    """
    Aggregate Suricata eve.json into dashboard-friendly stats.
    Default scans last N lines for performance, optionally filters by time window.
    """
    lines = _read_lines(WSL_EVE_PATH)
    if not lines:
        return JSONResponse(
            content={
                "total_events": 0,
                "total_alerts": 0,
                "by_severity": {},
                "by_category": [],
                "by_signature": [],
                "by_src_ip": [],
                "by_proto": [],
                "timeseries": [],
            }
        )

    # Window from tail for performance
    window = lines[-limit:]
    events = _parse_eve_lines(window)

    start_dt = _to_dt(start) if start else None
    end_dt = _to_dt(end) if end else None

    def in_range(ev: dict[str, Any]) -> bool:
        ts = _to_dt(ev.get("timestamp"))
        if start_dt and ts and ts < start_dt:
            return False
        if end_dt and ts and ts > end_dt:
            return False
        return True

    filtered = [e for e in events if in_range(e)]

    total_events = len(filtered)
    alerts = [e for e in filtered if e.get("event_type") == "alert"]
    total_alerts = len(alerts)

    # Severity distribution (1=high, 2=med, 3=low per Suricata)
    by_severity: dict[str, int] = {}
    for a in alerts:
        sev = None
        try:
            sev = a.get("alert", {}).get("severity")
        except Exception:
            sev = None
        key = str(sev) if sev is not None else "unknown"
        by_severity[key] = by_severity.get(key, 0) + 1

    # Top categories
    cat_counts: dict[str, int] = {}
    for a in alerts:
        cat = a.get("alert", {}).get("category") or "uncategorized"
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
    by_category = sorted(
        ({"name": k, "value": v} for k, v in cat_counts.items()),
        key=lambda x: x["value"],
        reverse=True,
    )[:10]

    # Top signatures
    sig_counts: dict[str, int] = {}
    for a in alerts:
        sig = a.get("alert", {}).get("signature") or "unknown"
        sig_counts[sig] = sig_counts.get(sig, 0) + 1
    by_signature = sorted(
        ({"name": k, "value": v} for k, v in sig_counts.items()),
        key=lambda x: x["value"],
        reverse=True,
    )[:10]

    # Top source IPs
    src_counts: dict[str, int] = {}
    for a in alerts:
        sip = a.get("src_ip") or "unknown"
        src_counts[sip] = src_counts.get(sip, 0) + 1
    by_src_ip = sorted(
        ({"name": k, "value": v} for k, v in src_counts.items()),
        key=lambda x: x["value"],
        reverse=True,
    )[:10]

    # Protocol distribution
    proto_counts: dict[str, int] = {}
    for a in alerts:
        p = a.get("proto") or a.get("app_proto") or "unknown"
        proto_counts[p] = proto_counts.get(p, 0) + 1
    by_proto = sorted(
        ({"name": k, "value": v} for k, v in proto_counts.items()),
        key=lambda x: x["value"],
        reverse=True,
    )

    # Timeseries per hour
    buckets: dict[str, int] = {}
    for e in alerts:
        ts = _to_dt(e.get("timestamp"))
        if not ts:
            continue
        key = ts.replace(minute=0, second=0, microsecond=0).isoformat()
        buckets[key] = buckets.get(key, 0) + 1
    timeseries = [
        {"ts": k, "count": v} for k, v in sorted(buckets.items(), key=lambda x: x[0])
    ]

    return JSONResponse(
        content={
            "total_events": total_events,
            "total_alerts": total_alerts,
            "by_severity": by_severity,
            "by_category": by_category,
            "by_signature": by_signature,
            "by_src_ip": by_src_ip,
            "by_proto": by_proto,
            "timeseries": timeseries,
        }
    )
