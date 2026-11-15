# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`FrameApp` — the archetype that DRIVES its own inference.

Demonstrates: `FrameApp`, `FrameApp.on_frame`, `KaiCClient`,
`KaiCError`, `build_frame_source`, `dict_frame_source`,
`HttpSnapshotSource`, `FileFrameSource`, `FrameSourceError`.

This is the costly archetype, so reach for it last. The platform already
detects and tracks on every camera, and a rule about a person or a
vehicle is a `Detector` on that stream, with no model of its own
(`02_detector.py`, `11_tier0.py`). A `FrameApp` is right when the
subject is the SCENE and no track describes it — is the gate open, is
the bin overflowing — so there is nothing on the bus to react to.

Even then: poll slowly (the answer here changes in minutes, not
frames), make every model call through KAI-C, and say in the code why
this rule polls. `opennvr-app validate` flags a model call inside
`on_frame`; the waiver below is how a real exception is recorded
(docs/BUILDING_EFFICIENT_APPS.md, rule 2).
"""
from dataclasses import dataclass, field

from opennvr_app_sdk import (
    Alert, AlertType, AppManifest, BaseAppConfig, FrameApp, KaiCClient, KaiCError,
    Param, build_frame_source, dict_frame_source, load_app_config,
)

QUESTION = "Is the gate open? Answer yes or no."

MANIFEST = AppManifest(
    id="gate-open",
    name="Gate Open",
    version="1.0.0",
    category="perimeter",
    summary="Asks a vision model, once a minute, whether the gate is standing open.",
    requires_tasks=["vqa"],
    subscribes=None,                    # a FrameApp drives inference itself
    params=[
        Param("poll_interval_seconds", float, default=60.0),
    ],
    emits=[AlertType("gate_open", severity="high")],
)


@dataclass
class AppConfig(BaseAppConfig):
    cameras: dict = field(default_factory=dict)     # {camera_id: snapshot_url}
    poll_interval_seconds: float = 60.0
    kaic_url: str = "http://kai-c:8100"     # KAI-C, never an adapter's own address
    adapter: str = "moondream"


class GateOpen(FrameApp):
    manifest = MANIFEST

    def setup(self) -> None:
        # One client for the app's lifetime; it pools connections.
        self.kaic = KaiCClient(self.cfg.kaic_url, self.cfg.adapter,
                               api_key=self.cfg.opennvr_token, timeout_seconds=30.0)
        self.was_open: dict[str, bool] = {}

    def on_frame(self, camera_id: str, frame_bytes: bytes) -> list[Alert]:
        """THE RULE. Called once per fetched frame, per camera. A raising
        rule is logged and skipped — one bad camera never stalls the
        others."""
        try:
            # opennvr: allow[per-frame-model] the gate's state is not a track; one call a minute per camera
            body = self.kaic.infer(frame_bytes, task="vqa", camera_id=camera_id,
                                   params={"question": QUESTION})
        except KaiCError as exc:
            # Inference is a network call; treat a failure as "could not
            # tell this tick" — not as closed, and not as a crash.
            self.log_inference_failure(camera_id, exc)
            return []

        answer = str((body.get("result") or {}).get("answer") or "").strip().lower()
        is_open = answer.startswith("yes")
        was_open, self.was_open[camera_id] = self.was_open.get(camera_id, False), is_open
        if not is_open or was_open:          # alert on the change, not on every poll
            return []
        return [Alert(
            title=f"Gate open on {camera_id}",
            description="The gate is standing open.",
            camera_id=camera_id, severity="high", tags=["gate_open"],
        )]

    def log_inference_failure(self, camera_id: str, exc: Exception) -> None:
        import logging
        logging.getLogger(MANIFEST.id).warning(
            "inference failed for %s: %s", camera_id, exc)


def build(config_path: str) -> GateOpen:
    """FrameApp needs a frame source injected — that is what makes it
    testable without cameras (`DictFrameSource` in tests, real snapshot
    URLs in production)."""
    from opennvr_app_sdk import build_dispatcher

    cfg = load_app_config(config_path, AppConfig)
    sources = {cam: build_frame_source(camera_id=cam, url=url)
               for cam, url in cfg.cameras.items()}
    return GateOpen(
        cfg,
        build_dispatcher(webhook_url=cfg.webhook_url),
        frame_source=dict_frame_source(sources),
        cameras=list(cfg.cameras),
        poll_interval_seconds=cfg.poll_interval_seconds,
    )
