# OpenNVR — yolo-pose-weights image

A ~17 MB image carrying one file: `yolo11n-pose.onnx`, already exported.
`yolo-pose-weights-init` in `docker-compose.apps.yml` copies it into the
`opennvr_yolo_pose_weights` volume, where the pose adapter reads it as
`/weights/yolo11n-pose.onnx`.

## Why this image exists at all

Every other model in the stack can be fetched from somewhere. This one
cannot: **Ultralytics publishes the `.pt` checkpoint and never the
ONNX**, so the graph has to be exported by somebody. Until this image
existed, that somebody was the operator — and nothing told them so.

That failure was the worst kind. A fresh `--profile apps` install pulled
the adapter, started it, and got a green healthcheck, because
`/health` answers before a model is ever loaded. The weights volume was
empty. Every `/infer` failed on a missing file, the guard-scan app
screened nobody, and the UI showed an app that was running fine and
doing nothing.

The second reason is the one `examples/yolov8-weights/` was built for:
an operator on a filtered network (reported from IN/CN/IR) can't reach
PyPI or ultralytics.com, so "export it at first boot" is not a plan.

## You usually don't need to touch this

`docker compose --profile apps up -d` pulls
`ghcr.io/open-nvr/yolo-pose-weights:v8.3.0` in a few seconds, the init
container copies the graph in, and `yolo-pose-adapter` starts only once
that copy has succeeded (`condition: service_completed_successfully`).
If the file is already in the volume the init container says so and
exits without touching it.

There is **no `build:` fallback** on the compose service. If the image
can't be pulled the init container fails, and the adapter — by design —
never starts, rather than starting into the silent failure above.

## Building it yourself

**Your network blocks ghcr.io.** Build where the network works and
carry the result over:

```bash
docker build -t yolo-pose-weights:v8.3.0 examples/yolo-pose-weights
docker save yolo-pose-weights:v8.3.0 | gzip > /tmp/yolo-pose-weights.tar.gz
scp /tmp/yolo-pose-weights.tar.gz ubuntu@deploy-box:/tmp/

# On the deploy box:
docker load < /tmp/yolo-pose-weights.tar.gz
echo 'YOLO_POSE_WEIGHTS_IMAGE=yolo-pose-weights:v8.3.0' >> .env
docker compose --profile apps up -d
```

The first build takes ~10 min, almost all of it pulling the ~5 GB
`ultralytics/ultralytics:8.3.40` base. Nothing of that base survives
into the published image.

**You have a fine-tuned pose model.** Point the build arg at your `.pt`:

```bash
docker build --build-arg POSE_PT_URL=https://example.com/my-pose.pt \
    -t my-registry.local:5000/yolo-pose-weights:custom \
    examples/yolo-pose-weights
```

Keep the *keypoint layout* COCO-17. The adapter maps joint indices to
names through `adapters/yolo_pose/coco_keypoints.py`, and guard-scan's
wrist-to-torso geometry reads those names — a model with a different
skeleton will return confident nonsense rather than an error.

**You'd rather host the ONNX yourself.** The adapter can fetch it at
first boot instead: set `YOLO_POSE_MODEL_URL` to a URL serving the
exported `.onnx` and the init container becomes unnecessary. The
default is empty, which means "never download" — the
`sovereignty=local_only` posture.

## Keep these export flags unchanged

```
opset=12  imgsz=448  dynamic=True
```

They match the adapter's own recipe (`download_models.py`,
`yolo_pose_adapter.export_kwargs`), and each one matters:

- **`dynamic=True` is not optional.** `OPENNVR_POSE_IMGSZ` (448 by
  default, any multiple of 32 from 160 to 1280) is the adapter's CPU
  dial. A fixed-size export rejects every size but the one it was
  exported at, which turns that dial into a crash. The Dockerfile
  asserts the graph really is dynamic after exporting, because a
  silently-static export passes every check except the one that
  happens on the operator's machine, weeks later.
- **`imgsz=448`, not 640.** Roughly twice the throughput for a keypoint
  error that stays well inside what limb-angle logic tolerates at
  entrance-camera framing. A doorway app wants frame rate.
- **`opset=12`** is what the adapter is conformance-tested against.

## CI publishes this

`.github/workflows/build-yolo-pose-weights.yml` builds and pushes
`ghcr.io/open-nvr/yolo-pose-weights:v<weights-tag>` plus
`:sha-<short>` on pushes to main, and `:latest` on a `v*` release tag.
Multi-arch (amd64 + arm64) — an ONNX graph is the same file on both,
and an amd64-only manifest would abort `docker compose pull` for the
whole stack on Apple Silicon or a Pi.

After pushing, CI pulls the image back and checks the `.onnx` is really
inside and really is a model — cheap insurance against "the build
succeeded but the COPY didn't", which is the exact silent failure this
image exists to prevent.
