# OpenNVR — fastsam-weights image

A ~55 MB image carrying one file: `FastSAM-s.onnx`, already exported.
`fastsam-weights-init` in `docker-compose.apps.yml` copies it into the
`opennvr_fastsam_weights` volume, which the gold-bar-desk app reads as
`/weights/FastSAM-s.onnx`.

## Why this image exists

The same reasons as `examples/yolo-pose-weights/`:

* Ultralytics publishes the FastSAM-s `.pt` and never the ONNX, so someone has
  to export it. Doing it once, here, beats every operator doing it.
* An operator on a filtered network can't reach PyPI or ultralytics.com at
  first boot.

The build asserts the exported graph has the layout the app decodes (boxes
plus 32 mask coefficients over 8400 anchors, and 32 prototypes at 160×160).
A different layout would load fine and segment nothing.

## Licence

FastSAM-s weights come from the Ultralytics assets release and are AGPL-3.0,
the same as OpenNVR and the yolo11n-pose weights.

## Building it yourself

```bash
docker build -t ghcr.io/open-nvr/fastsam-weights:v8.3.0 examples/fastsam-weights
```

The exporter stage uses the ~5 GB `ultralytics/ultralytics` image. Nothing of
it survives into the final image.
