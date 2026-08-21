# AI Observability

Every AI component in OpenNVR answers "which model, on which task, how
fast, failing how often, and what is it actually producing" from a
Prometheus `/metrics` scrape. There are three vantage points, and they
deliberately overlap — self-reporting misses queueing and dies with the
process; client-side observation can't see inside the model:

| Layer | Endpoint | What it knows |
|---|---|---|
| **Adapters** (self-reported) | `<adapter>:<port>/metrics` | identity (`adapter_model_info`: model, version, framework, weights fingerprint), per-task latency histogram + outcome counters, in-flight/queue gauges, and **domain series** per model family — see below |
| **KAI-C** (client-observed) | `opennvr-core:8100/metrics` | proxied-inference latency as callers experience it (network + adapter queue + inference), per-adapter result counters, and `kaic_adapter_up` / consecutive-health-failure gauges from the 60 s poll — still meaningful when an adapter is too wedged to answer its own `/metrics` |
| **Tier-0 detect-pipeline** | `detect-pipeline:9109/metrics` | frames/detector runs/skips, per-stage latency (decode/motion/detect/track), tracks, per-class detections, bounded-load guard counters |

No Prometheus? The **AI Adapters page** in the OpenNVR UI shows the same
signals with hour-long sparklines — KAI-C scrapes every registered
adapter on its 60 s poll and keeps a one-hour ring buffer, no external
stack required. (That's also why the camera-agent compose registers the
whisper/piper voice adapters with KAI-C even though the agent calls them
directly: registration is what puts them on the health panel.)

## Domain metrics per adapter

The generic series are identical everywhere; each model family also
reports the numbers that define *its* health:

| Adapter | Series | Reading it |
|---|---|---|
| whisper, piper | `adapter_audio_seconds_total`, `adapter_realtime_factor` (histogram) | RTF = compute-seconds per audio-second. **> 1.0 means it can't keep up** with speech (STT) or playback (TTS) on this hardware |
| yolov8 | `adapter_detections_total{label=…}` | per-class output volume — a class you don't expect climbing fast is the false-positive signature |
| blip, moondream, ollamavlm | `adapter_generated_chars_total` | generation volume; ollamavlm adds `adapter_upstream_latency_seconds` (time inside Ollama vs adapter overhead) |
| fast-plate-ocr | `adapter_plate_reads_total{result=accepted\|below_threshold}` | read quality trend |
| insightface | `adapter_faces_total{stage=detected\|recognized\|unrecognized}` | recognition hit-rate |
| bytetrack | `adapter_tracked_objects_total{result=tracked\|untracked}` | tracker association quality |

## Prometheus scrape config

Drop this into `prometheus.yml` on a Prometheus that can reach the
compose network (run it as a service on `opennvr_internal`, or publish
the ports to a monitoring host):

```yaml
scrape_configs:
  # KAI-C: fleet view — client-observed latency, per-adapter up/health.
  - job_name: opennvr-kaic
    metrics_path: /metrics
    static_configs:
      - targets: ["opennvr-core:8100"]

  # Adapters: self-reported + domain series. List the ones you run.
  - job_name: opennvr-adapters
    metrics_path: /metrics
    static_configs:
      - targets:
          - "yolov8-adapter:9002"
          - "whisper-adapter:9003"     # camera-agent voice profile
          - "piper-adapter:9001"       # camera-agent voice profile
          - "caption-adapter:9006"     # moondream / blip / ollamavlm
    relabel_configs:
      - source_labels: [__address__]
        regex: "([^:]+):.*"
        target_label: adapter
        replacement: "$1"

  # Tier-0 detect-pipeline.
  - job_name: opennvr-tier0
    metrics_path: /metrics
    static_configs:
      - targets: ["detect-pipeline:9109"]
```

## Grafana starter dashboard

Import [`grafana/opennvr-ai-health.json`](grafana/opennvr-ai-health.json)
(Dashboards → New → Import). It assumes a Prometheus datasource and the
scrape config above, and gives you: fleet up/down, client-observed p95
per adapter, request + error rates, per-class detection volume, audio
realtime factor, and Tier-0 stage latency. Treat it as a starting point —
every panel is a single PromQL expression to copy and adapt.

Useful queries to build on:

```promql
# Client-observed p95 per adapter (as callers experience it)
histogram_quantile(0.95, sum by (adapter, le)
  (rate(kaic_proxy_infer_latency_seconds_bucket[5m])))

# Self-reported p95 per task for one adapter
histogram_quantile(0.95, sum by (task, le)
  (rate(adapter_infer_latency_seconds_bucket{adapter="moondream"}[5m])))

# Error ratio per adapter
sum by (adapter) (rate(kaic_proxy_infer_total{result!="ok"}[5m]))
  / sum by (adapter) (rate(kaic_proxy_infer_total[5m]))

# Detections per class per minute (false-positive watch)
sum by (label) (rate(adapter_detections_total[5m])) * 60

# Audio realtime factor, windowed mean (>1 = can't keep up)
rate(adapter_realtime_factor_sum[10m]) / rate(adapter_realtime_factor_count[10m])

# Detector time share of each Tier-0 frame
sum by (stage) (rate(tier0_stage_latency_seconds_sum[5m]))
```

## Model identity & tamper visibility

`adapter_model_info{model, model_version, framework, fingerprint}` is an
info-metric (value always 1; the labels are the payload) refreshed on
every capabilities poll. Join it in PromQL to annotate any panel with the
exact weights that produced the numbers:

```promql
histogram_quantile(0.95, sum by (le) (rate(adapter_infer_latency_seconds_bucket[5m])))
  * on () group_left (model, fingerprint) adapter_model_info
```

A fingerprint label change on a running adapter is the same §11.3 drift
signal KAI-C's tamper detection audits — visible on a chart as a series
break.
