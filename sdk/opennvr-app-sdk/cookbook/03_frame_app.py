# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`FrameApp` — the archetype that DRIVES its own inference.

Demonstrates: `FrameApp`, `FrameApp.on_frame`, `KaiCClient`,
`KaiCError`, `build_frame_source`, `dict_frame_source`,
`HttpSnapshotSource`, `FileFrameSource`, `FrameSourceError`.

Use it when no adapter is already producing what you need — you pull
frames on an interval and pay for the inference yourself. When another
app is already running the model you want, use a `Detector` instead and
ride its stream for free.
"""
from dataclasses import dataclass, field

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, BaseAppConfig, FrameApp, KaiCClient, KaiCError,
    Param, build_frame_source, dict_frame_source, load_app_config,
)

MANIFEST = AppManifest(
    id="intrusion-detection",
    name="Intrusion Detection",
    version="1.0.0",
    category="perimeter",
    summary="Polls camera frames and alerts on people in a restricted area.",
    requires_tasks=["object_detection"],
    subscribes=None,                    # a FrameApp drives inference itself
    params=[
        Param("poll_interval_seconds", float, default=5.0),
        Param("min_confidence", float, default=0.5),
    ],
    emits=[AlertType("intrusion", severity="high")],
)


@dataclass
class AppConfig(BaseAppConfig):
    cameras: dict = field(default_factory=dict)     # {camera_id: snapshot_url}
    poll_interval_seconds: float = 5.0
    min_confidence: float = 0.5
    kaic_url: str = "http://kai-c:8100"
    adapter: str = "yolov8"


class Intrusion(FrameApp):
    manifest = MANIFEST

    def setup(self) -> None:
        # One client for the app's lifetime; it pools connections.
        self.kaic = KaiCClient(self.cfg.kaic_url,
                               api_key=self.cfg.opennvr_token)

    def on_frame(self, camera_id: str, frame_bytes: bytes) -> list[Alert]:
        """THE RULE. Called once per fetched frame, per camera. A raising
        rule is logged and skipped — one bad camera never stalls the
        others."""
        try:
            result = self.kaic.infer(frame_bytes, adapter=self.cfg.adapter,
                                     task="object_detection", camera_id=camera_id)
        except KaiCError as exc:
            # Inference is a network call; treat a failure as "no
            # detections this tick", not as a crash.
            self.log_inference_failure(camera_id, exc)
            return []

        people = [d for d in (result.get("detections") or [])
                  if d.get("label") == "person"
                  and float(d.get("confidence", 0)) >= self.cfg.min_confidence]
        if not people:
            return []
        return [Alert(
            title=f"Intrusion on {camera_id}",
            description=f"{len(people)} person(s) in a restricted area.",
            camera_id=camera_id, severity="high", tags=["intrusion"],
        )]

    def log_inference_failure(self, camera_id: str, exc: Exception) -> None:
        import logging
        logging.getLogger(MANIFEST.id).warning(
            "inference failed for %s: %s", camera_id, exc)


def build(config_path: str) -> Intrusion:
    """FrameApp needs a frame source injected — that is what makes it
    testable without cameras (`DictFrameSource` in tests, real snapshot
    URLs in production)."""
    from opennvr_app_sdk import build_dispatcher

    cfg = load_app_config(config_path, AppConfig)
    sources = {cam: build_frame_source(camera_id=cam, url=url)
               for cam, url in cfg.cameras.items()}
    return Intrusion(
        cfg,
        build_dispatcher(webhook_url=cfg.webhook_url),
        frame_source=dict_frame_source(sources),
        cameras=list(cfg.cameras),
        poll_interval_seconds=cfg.poll_interval_seconds,
    )
