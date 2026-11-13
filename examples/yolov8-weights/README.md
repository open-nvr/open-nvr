# OpenNVR — yolov8-weights image

A ~20 MB Docker image that contains `yolo26n.onnx` pre-baked: YOLO26n exported the way the yolov8 adapter and the detect pipeline expect. The image keeps its `yolov8` name because the decode is the YOLOv8 one, and yolov8n, yolo11n and yolo26n all emit the same `(1, 84, 8400)` tensor when exported with the flags below. Measured on a 4-thread CPU: 62 ms a frame against yolov8n's 75, COCO mAP 40.9 against 37.3. Used by `docker-compose.yml`'s `yolov8-weights-init` container instead of the older runtime apt+pip+export path (which failed for operators behind ISP firewalls that filter `deb.debian.org` or `pypi.org`).

## You usually don't need to touch this

`docker compose -f docker-compose.yml up -d` Just Works:

- If `ghcr.io/open-nvr/yolov8-weights:v8.4.0-yolo26n` is published (the OpenNVR release path), Docker pulls it in ~5 seconds and the init container copies the ONNX into the volume.
- If the published image isn't reachable from your host (registry blocked, fresh repo before first release, etc.), Docker Compose's `build:` fallback kicks in and builds the image locally from the Dockerfile in this directory. First-time build takes ~10 min (dominated by the `ultralytics/ultralytics:8.4.177` base pull, ~3 GB). Subsequent `up -d` runs use the cached image instantly.

## When you might want to build it yourself

Three scenarios:

**1. Your network blocks ghcr.io specifically.** Rare but happens. Build on a machine with internet, transfer the image tarball:

```bash
cd examples/yolov8-weights
docker build -t yolov8-weights:v8.4.0-yolo26n .

docker save yolov8-weights:v8.4.0-yolo26n | gzip > /tmp/yolov8-weights.tar.gz
scp /tmp/yolov8-weights.tar.gz ubuntu@deploy-box:/tmp/

# On the deploy box:
docker load < /tmp/yolov8-weights.tar.gz
echo 'YOLOV8_WEIGHTS_IMAGE=yolov8-weights:v8.4.0-yolo26n' >> .env
docker compose -f docker-compose.yml up -d
```

**2. You have a fine-tuned YOLO model you want to bake in** (yolov8, yolo11 or yolo26: the export below gives all three the same tensor). Override the `YOLOV8_PT_URL` build arg to point at your `.pt`:

```bash
docker build \
    --build-arg YOLOV8_PT_URL=https://example.com/my-fine-tuned-yolo26n.pt \
    -t my-registry.local:5000/yolov8-weights:custom \
    .

docker push my-registry.local:5000/yolov8-weights:custom

# Then in your .env on the deploy box:
echo 'YOLOV8_WEIGHTS_IMAGE=my-registry.local:5000/yolov8-weights:custom' >> .env
```

Or copy your `.pt` directly into the build context:

```dockerfile
FROM ultralytics/ultralytics:8.4.177 AS exporter
WORKDIR /build
COPY my-fine-tuned-model.pt yolo26n.pt
RUN yolo export model=yolo26n.pt format=onnx opset=12 imgsz=640 simplify=False

FROM alpine:3.20
COPY --from=exporter /build/yolo26n.onnx /yolo26n.onnx
```

**3. You want to pin a specific Ultralytics release.** Change the base image tag in the Dockerfile:

```dockerfile
FROM ultralytics/ultralytics:8.4.200 AS exporter   # ← instead of 8.4.177
```

## Critical: keep these export flags unchanged

```
opset=12 imgsz=640 simplify=False
```

The OpenNVR YOLOv8 adapter and the detect pipeline decode the `(1, 84, 8400)` tensor these flags produce (`end2end: False` in the ONNX metadata; YOLO26's NMS-free end-to-end export is a different tensor and is NOT what they read), and the adapter is conformance-tested against this exact byte layout (see `docs/AI_ADAPTER_CONTRACT.md` §11.4). Changing any of them — especially `simplify=True` — folds constants, renames graph nodes, and shifts the model's sha256 fingerprint that lands in the audit chain. The adapter may also break or produce wrong outputs.

## CI publishes this automatically

`.github/workflows/build-yolov8-weights.yml` builds + pushes `ghcr.io/open-nvr/yolov8-weights:v<assets-tag>-<model>` (today `v8.4.0-yolo26n`) on every OpenNVR release tag, and on a manual run of the workflow from any branch. Multi-arch (amd64 + arm64). On main-branch pushes it publishes `:main` and `:sha-<short>` for traceability. The standard `docker compose -f docker-compose.yml up -d` picks up whatever's published on GHCR.
