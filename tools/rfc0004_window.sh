#!/usr/bin/env bash
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
#
# RFC-0004: one snapshot of the counters a traffic window is measured
# from, on the box, as JSON on one line. Run it at the start and the end
# of a window and diff the two (tools/rfc0004_window_diff.py does that):
#
#   bash tools/rfc0004_window.sh >> /tmp/paired/window-A.jsonl
#
# What it reads, and from where:
#   - the captioner's call count, latency sum and generated characters,
#     per task, from the adapter's own /metrics (inside its container, with
#     its own token — the token never leaves the container);
#   - the detector's latency sum and count per camera from the detect
#     pipeline's metrics (read-only);
#   - visits, captions written and descriptor rows from the database.
# It changes nothing. Container names are the compose defaults.
set -euo pipefail

TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

ADAPTER=$(docker exec opennvr_caption_adapter python3 -c '
import urllib.request, os, json, re
req = urllib.request.Request("http://127.0.0.1:9006/metrics",
                             headers={"Authorization": "Bearer " + os.environ.get("OPENNVR_ADAPTER_TOKEN", "")})
t = urllib.request.urlopen(req, timeout=5).read().decode()
out = {"infer_total": {}, "latency_sum_s": {}, "latency_count": {}, "generated_chars": 0.0}
for line in t.splitlines():
    if line.startswith("#") or not line.strip():
        continue
    name, _, value = line.rpartition(" ")
    m = re.match(r"(\w+)(\{.*\})?$", name)
    if not m:
        continue
    metric, labels = m.group(1), m.group(2) or ""
    task = (re.search(r"task=\"([^\"]*)\"", labels) or [None, "all"])[1]
    v = float(value)
    if metric == "adapter_infer_total":
        out["infer_total"][task] = out["infer_total"].get(task, 0.0) + v
    elif metric == "adapter_infer_latency_seconds_sum":
        out["latency_sum_s"][task] = out["latency_sum_s"].get(task, 0.0) + v
    elif metric == "adapter_infer_latency_seconds_count":
        out["latency_count"][task] = out["latency_count"].get(task, 0.0) + v
    elif metric == "adapter_generated_chars_total":
        out["generated_chars"] += v
print(json.dumps(out))
' 2>/dev/null || echo '{}')

DETECTOR=$(docker exec opennvr_detect_pipeline python3 -c '
import urllib.request, json, re
t = urllib.request.urlopen("http://127.0.0.1:9109/metrics", timeout=5).read().decode()
out = {}
for line in t.splitlines():
    if not line.startswith("tier0_detector_latency_seconds_"):
        continue
    name, _, value = line.rpartition(" ")
    cam = (re.search(r"camera[_a-z]*=\"([^\"]*)\"", name) or [None, "?"])[1]
    key = "sum" if name.startswith("tier0_detector_latency_seconds_sum") else ("count" if name.startswith("tier0_detector_latency_seconds_count") else None)
    if key:
        out.setdefault(cam, {})[key] = out.get(cam, {}).get(key, 0.0) + float(value)
print(json.dumps(out))
' 2>/dev/null || echo '{}')

DB=$(docker exec opennvr_db psql -U opennvr_user -d opennvr_db -tA -c "
select json_build_object(
  'visits', (select count(*) from events),
  'captions', (select count(*) from event_text where coalesce(caption,'')<>''),
  'descriptor_rows', (select count(*) from visit_descriptors),
  'visits_last_10m', (select count(*) from events where created_at > now() - interval '10 minutes'),
  'captions_last_10m', (select count(*) from event_text where updated_at > now() - interval '10 minutes' and coalesce(caption,'')<>''),
  'descriptor_rows_last_10m', (select count(*) from visit_descriptors where created_at > now() - interval '10 minutes'))" 2>/dev/null || echo '{}')

LOAD=$(cut -d' ' -f1-3 /proc/loadavg)

printf '{"ts":"%s","load":"%s","adapter":%s,"detector":%s,"db":%s}\n' "$TS" "$LOAD" "$ADAPTER" "$DETECTOR" "$DB"
