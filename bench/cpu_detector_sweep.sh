#!/usr/bin/env bash
# CPU-only detector sweep on a running stack (bench/cpu-detector-defaults-2026-11-13.md): for each fps:cap combination,
# recreate the detect pipeline with those two knobs (shell env outranks
# .env for Compose interpolation), wait for the four fake cameras' workers,
# warm up, then measure a fixed window: pipeline counters (delta) and
# container CPU (docker stats samples). One JSON line per run.
set -u
cd "$(dirname "$0")/.."
S=${SWEEP_DIR:-$PWD/bench/cpu-sweep-out}; mkdir -p "$S"
OUT=${OUT:-$S/runs.jsonl}
WARM=${WARM:-90}; WIN=${WIN:-340}
COMBOS=${COMBOS:-"2:8 2:4 3:4 4:4 5:8 5:4 5:3 5:2 3:3"}
snap() { docker exec -i opennvr_detect_pipeline python3 - <<'PY'
import urllib.request, json
t = urllib.request.urlopen("http://127.0.0.1:9109/metrics", timeout=10).read().decode()
out = {}
for l in t.splitlines():
    if not l.startswith("tier0_"): continue
    name, _, val = l.rpartition(" ")
    try: out[name] = float(val)
    except ValueError: pass
print(json.dumps(out))
PY
}
for c in $COMBOS; do
  IFS=: read -r fps cap backend <<<"$c"; backend=${backend:-auto}
  echo "=== $(date -u +%H:%M:%SZ) combo fps=$fps cap=$cap backend=$backend"
  DETECT_FPS=$fps DETECT_MAX_REGIONS=$cap DETECT_ONNX_BACKEND=$backend docker compose up -d --no-deps --force-recreate detect-pipeline >/dev/null 2>&1
  up=0
  for i in $(seq 1 36); do
    up=$(snap 2>/dev/null | python3 -c 'import sys,json; d=json.load(sys.stdin); print(sum(1 for k,v in d.items() if k.startswith("tier0_worker_up{") and v==1))' 2>/dev/null || echo 0)
    [ "${up:-0}" -ge 4 ] && break; sleep 5
  done
  echo "workers up: $up (after $((i*5)) s)"
  sleep "$WARM"
  A=$(snap); t0=$(date +%s)
  ST=$S/stats-$fps-$cap-$backend.txt; : > "$ST"
  end=$((t0+WIN))
  while [ "$(date +%s)" -lt "$end" ]; do
    docker stats --no-stream --format '{{.Name}} {{.CPUPerc}} {{.MemUsage}}' opennvr_detect_pipeline opennvr_mediamtx opennvr_fakecams opennvr_core >> "$ST" 2>/dev/null
    sleep 8
  done
  B=$(snap); t1=$(date +%s)
  python3 - "$fps" "$cap" "$A" "$B" "$((t1-t0))" "$ST" "$backend" >> "$OUT" <<'PY'
import sys, json, re, statistics
fps, cap, A, B, secs, st, backend = int(sys.argv[1]), int(sys.argv[2]), json.loads(sys.argv[3]), json.loads(sys.argv[4]), int(sys.argv[5]), sys.argv[6], sys.argv[7]
def lab(k, l):
    m = re.search(l + r'="([^"]*)"', k); return m.group(1) if m else None
def delta(prefix, where=lambda k: True):
    return sum(B[k] - A.get(k, 0.0) for k in B if k.startswith(prefix) and where(k))
cams = sorted({lab(k, "camera") for k in B if k.startswith("tier0_worker_up{")} - {None})
per = {}
for c in cams:
    w = lambda k, c=c: lab(k, "camera") == c
    lat_s = delta("tier0_detector_latency_seconds_sum", w); lat_n = delta("tier0_detector_latency_seconds_count", w)
    per[c] = {
        "frames": delta("tier0_frames_total", w), "dropped": delta("tier0_frames_dropped_total", w),
        "idle_dups": delta("tier0_frames_idle_duplicates_total", w),
        "crops": delta("tier0_detector_runs_total", w), "det_calls": lat_n,
        "det_ms_mean": round(1000 * lat_s / lat_n, 1) if lat_n else None,
        "capped": delta("tier0_regions_capped_total", w),
        "budget_end": next((v for k, v in B.items() if k.startswith("tier0_regions_budget{") and w(k)), None),
        "configured": next((v for k, v in B.items() if k.startswith("tier0_regions_configured{") and w(k)), None),
        "processing_fps": next((v for k, v in B.items() if k.startswith("tier0_processing_fps{") and w(k)), None),
        "target_fps": next((v for k, v in B.items() if k.startswith("tier0_target_fps{") and w(k)), None),
        "visits": delta("tier0_visits_posted_total", w), "visits_dropped": delta("tier0_visits_dropped_total", w),
        "spawns_dropped": next((v for k, v in B.items() if k.startswith("tier0_track_spawns_dropped{") and w(k)), 0) - next((v for k, v in A.items() if k.startswith("tier0_track_spawns_dropped{") and w(k)), 0),
        "tracks_pop_end": next((v for k, v in B.items() if k.startswith("tier0_tracks_population{") and w(k)), None),
        "detections": {lab(k, "label"): round(B[k] - A.get(k, 0.0)) for k in B if k.startswith("tier0_detections_total{") and w(k) and B[k] - A.get(k, 0.0) > 0},
        "stationary_skipped": delta("tier0_stationary_skipped_total", w),
    }
proc_cpu = [v for k, v in B.items() if k.startswith("tier0_process_cpu_percent")]
stats = {}
for line in open(st):
    p = line.split()
    if len(p) >= 2 and p[1].endswith("%"):
        stats.setdefault(p[0], []).append(float(p[1].rstrip("%")))
cpu = {n: round(statistics.mean(v), 1) for n, v in stats.items() if v}
print(json.dumps({"fps": fps, "cap": cap, "backend": backend, "window_s": secs, "container_cpu_mean_pct": cpu, "process_cpu_pct_end": proc_cpu, "cameras": per}))
PY
  echo "done fps=$fps cap=$cap backend=$backend"
done
echo "=== restoring the pipeline to .env values"
docker compose up -d --no-deps --force-recreate detect-pipeline >/dev/null 2>&1
echo "=== sweep finished $(date -u +%H:%M:%SZ)"
