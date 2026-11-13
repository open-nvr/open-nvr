#!/usr/bin/env bash
# Per-inference latency of yolo26n (640) on this host, per backend and thread
# count, with the stack's pipeline stopped so nothing else competes. Runs the
# image's own bench module in a throwaway container from the same image.
set -u
S=${SWEEP_DIR:-$PWD/bench/cpu-sweep-out}; mkdir -p "$S"
OUT=$S/microbench.jsonl; : > "$OUT"
docker stop opennvr_detect_pipeline >/dev/null 2>&1
for backend in cvdnn ort; do for threads in 1 2 4 8; do
  echo "=== $(date -u +%H:%M:%SZ) $backend threads=$threads"
  r=$(docker run --rm --entrypoint python -v "${BENCH_CLIP:?set BENCH_CLIP to an H.264 clip}:/tmp/road.mp4:ro" -v ${WEIGHTS_VOLUME:-open-nvr_opennvr_yolov8_weights}:/app/model_weights:ro ${DETECT_PIPELINE_IMAGE:-ghcr.io/open-nvr/detect-pipeline:main} \
        -m detect_pipeline.bench --source /tmp/road.mp4 --detector onnx --model /app/model_weights/yolo26n.onnx \
        --backend "$backend" --threads "$threads" --latency 30 --warmup 10 --max-frames 60 2>&1)
  j=$(printf '%s\n' "$r" | python3 -c '
import sys, json
text = sys.stdin.read(); dec = json.JSONDecoder(); last = None; i = 0
while True:
    i = text.find("{", i)
    if i < 0: break
    try:
        obj, end = dec.raw_decode(text[i:]); last = obj; i += end
    except ValueError:
        i += 1
print(json.dumps(last) if isinstance(last, dict) else "null")')
  printf '{"backend":"%s","threads":%s,"result":%s}\n' "$backend" "$threads" "$j" >> "$OUT"
  [ "$j" = "null" ] && printf '%s\n' "$r" | tail -5
done; done
docker start opennvr_detect_pipeline >/dev/null 2>&1
echo "=== microbench finished $(date -u +%H:%M:%SZ)"
