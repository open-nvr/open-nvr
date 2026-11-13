# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The four tamper conditions, the confirm delay, cooldown, clearing and
re-baselining — driven with synthetic frames, no broker, no core."""
from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageFilter

from camera_tamper import AppConfig, CameraTamper, correlation, frame_change, sharpness, structure_correlation, to_gray
from opennvr_app_sdk.testing import RecorderChannel, app_config

W, H = 320, 240


def _jpeg(im: Image.Image, quality: int = 90) -> bytes:
    b = io.BytesIO()
    im.save(b, "JPEG", quality=quality)
    return b.getvalue()


def scene(seed: int = 1) -> Image.Image:
    """A textured, high-contrast scene: a noisy field with blocks and lines."""
    rng = np.random.default_rng(seed)
    a = rng.integers(60, 200, size=(H, W), dtype=np.uint8)
    a[40:120, 50:150] = 230
    a[150:220, 180:300] = 20
    a[::16, :] = 255
    a[:, ::24] = 0
    return Image.fromarray(a).convert("RGB")


def dark(seed: int) -> Image.Image:
    """A covered lens as a camera really sends it: near black with sensor noise."""
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 12, size=(H, W), dtype=np.uint8)
    return Image.fromarray(a).convert("RGB")


def with_noise(im: Image.Image, seed: int) -> Image.Image:
    """The same scene with sensor noise, so consecutive frames differ in bytes."""
    rng = np.random.default_rng(seed)
    a = np.asarray(im.convert("L"), dtype=np.int16)
    a = np.clip(a + rng.integers(-3, 4, size=a.shape), 0, 255).astype(np.uint8)
    return Image.fromarray(a).convert("RGB")


class Source:
    def __init__(self) -> None:
        self.frames: dict[str, bytes] = {}

    def get_frame(self, camera_id: str) -> bytes | None:
        return self.frames.get(camera_id)


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make_app(**overrides):
    cfg = app_config(**{**AppConfig().__dict__, **overrides})
    rec = RecorderChannel()
    src = Source()
    app = CameraTamper(cfg, rec.dispatcher(), frame_source=src, cameras=["cam1"],
                       poll_interval_seconds=5.0)
    clock = Clock()
    app._clock = clock
    return app, rec, src, clock


def run_ticks(app, src, clock, frames: list[bytes], step: float = 5.0) -> None:
    for f in frames:
        src.frames["cam1"] = f
        app.handle_tick()
        clock.t += step


def learn_reference(app, src, clock, n: int = 4) -> None:
    run_ticks(app, src, clock, [_jpeg(with_noise(scene(), i)) for i in range(n)])


def test_arithmetic_tells_sharp_from_blurred_and_same_from_moved():
    base = to_gray(_jpeg(scene()))
    blurred = to_gray(_jpeg(scene().filter(ImageFilter.GaussianBlur(6))))
    assert sharpness(blurred) < 0.3 * sharpness(base)
    shifted = to_gray(_jpeg(scene().transform(scene().size, Image.AFFINE, (1, 0, 120, 0, 1, 80))))
    noisy = to_gray(_jpeg(with_noise(scene(), 9)))
    assert correlation(base, noisy) > 0.9 and structure_correlation(base, noisy) > 0.9
    assert structure_correlation(base, shifted) < 0.45
    # A 60% crop of the same scene, scaled back up: brightness correlation
    # stays high (same sky, same asphalt), edge correlation does not.
    zoomed = to_gray(_jpeg(scene().crop((110, 30, 300, 170)).resize((W, H))))
    assert structure_correlation(base, zoomed) < 0.45
    assert frame_change(base, noisy) > 0.2 and frame_change(base, base) == 0.0


def test_normal_frames_learn_a_reference_and_fire_nothing():
    app, rec, src, clock = make_app()
    learn_reference(app, src, clock, n=6)
    assert rec.alerts == []
    st = app.state_snapshot()["cameras"]["cam1"]
    assert st["has_reference"] and st["condition"] is None and st["checks"] == 6


@pytest.mark.parametrize("condition, make_frame", [
    ("covered", lambda i: _jpeg(dark(i))),
    ("defocused", lambda i: _jpeg(with_noise(scene(), i).filter(ImageFilter.GaussianBlur(6)))),
    ("moved", lambda i: _jpeg(with_noise(scene(), i).transform((W, H), Image.AFFINE, (1, 0, 120, 0, 1, 80)))),
])
def test_each_condition_alerts_once_after_the_confirm_delay(condition, make_frame):
    app, rec, src, clock = make_app(confirm_seconds=10, cooldown_seconds=300, attach_snapshot=False)
    learn_reference(app, src, clock)
    # Two polls (0 s, 5 s held) stay quiet; the third (10 s held) fires.
    run_ticks(app, src, clock, [make_frame(i) for i in range(2)])
    assert rec.alerts == [], f"{condition} alerted before confirm_seconds"
    run_ticks(app, src, clock, [make_frame(2)])
    assert [a.alert_type for a in rec.alerts] == [f"tamper_{condition}"]
    assert rec.alerts[0].camera_id == "cam1" and rec.alerts[0].severity == "high"
    # Still tampered: cooldown keeps it to one alert.
    run_ticks(app, src, clock, [make_frame(i) for i in range(3, 8)])
    assert len(rec.alerts) == 1


def _ticking_corner(im: Image.Image, i: int) -> Image.Image:
    """The same picture with a few pixels changing, as a frozen decoder
    with a burned-in clock would send: not the same bytes, still frozen."""
    a = np.asarray(im.convert("L")).copy()
    a[2:8, 2:8] = (i * 40) % 255
    return Image.fromarray(a).convert("RGB")


@pytest.mark.parametrize("how", ["same-bytes", "nearly-same-picture"])
def test_frozen_stream_alerts_on_an_unchanging_picture_and_clears_when_it_moves(how):
    """A frozen stream arrives as identical bytes, or as different bytes of
    a picture that does not change. Both are frozen; sensor noise on a
    live camera (see learn_reference's frames) is not."""
    app, rec, src, clock = make_app(confirm_seconds=0, freeze_polls=3, attach_snapshot=False)
    learn_reference(app, src, clock)
    still = scene()
    frames = ([_jpeg(still)] * 3 if how == "same-bytes"
              else [_jpeg(_ticking_corner(still, i)) for i in range(3)])
    run_ticks(app, src, clock, frames[:2])
    assert rec.alerts == []
    run_ticks(app, src, clock, frames[2:])        # third unchanged frame
    assert [a.alert_type for a in rec.alerts] == ["tamper_frozen"]
    run_ticks(app, src, clock, [_jpeg(with_noise(scene(), 42))])
    assert [a.alert_type for a in rec.alerts] == ["tamper_frozen", "tamper_cleared"]
    assert rec.alerts[-1].severity == "low"


def test_cleared_fires_once_when_the_lens_is_uncovered():
    app, rec, src, clock = make_app(confirm_seconds=0, attach_snapshot=False)
    learn_reference(app, src, clock)
    run_ticks(app, src, clock, [_jpeg(dark(7))])
    run_ticks(app, src, clock, [_jpeg(with_noise(scene(), i)) for i in range(20, 24)])
    assert [a.alert_type for a in rec.alerts] == ["tamper_covered", "tamper_cleared"]


def test_a_camera_moved_for_long_enough_becomes_the_new_reference():
    app, rec, src, clock = make_app(confirm_seconds=0, rebaseline_seconds=60, attach_snapshot=False)
    learn_reference(app, src, clock)
    moved = [_jpeg(with_noise(scene(), i).transform((W, H), Image.AFFINE, (1, 0, 120, 0, 1, 80)))
             for i in range(30, 45)]
    run_ticks(app, src, clock, moved)             # 15 polls x 5 s = 75 s moved
    types = [a.alert_type for a in rec.alerts]
    assert types[0] == "tamper_moved" and types[-1] == "tamper_cleared"
    # The moved view is now normal: more of it fires nothing.
    before = len(rec.alerts)
    run_ticks(app, src, clock, moved[:3])
    assert len(rec.alerts) == before


def test_a_passing_hand_does_not_alert():
    app, rec, src, clock = make_app(confirm_seconds=10, attach_snapshot=False)
    learn_reference(app, src, clock)
    run_ticks(app, src, clock, [_jpeg(dark(8))])                                  # one dark poll
    run_ticks(app, src, clock, [_jpeg(with_noise(scene(), i)) for i in range(50, 53)])
    assert rec.alerts == []


def test_config_rejects_nonsense():
    with pytest.raises(ValueError):
        AppConfig(poll_interval_seconds=0)
    with pytest.raises(ValueError):
        AppConfig(alert_severity="loud")
    with pytest.raises(ValueError):
        AppConfig(freeze_polls=1)
    with pytest.raises(ValueError):
        AppConfig(frozen_mad=-1)



def test_catalog_config_applies_live_and_rejects_bad_values():
    app, rec, src, clock = make_app()
    app.on_config_update({"confirm_seconds": 2, "poll_interval_seconds": 1.5, "alert_severity": "medium"})
    assert app.cfg.confirm_seconds == 2.0 and app._interval == 1.5 and app.cfg.alert_severity == "medium"
    app.on_config_update({"freeze_polls": 0, "blur_ratio": 0.2})       # the bad one is skipped, the good one lands
    assert app.cfg.freeze_polls == 6 and app.cfg.blur_ratio == 0.2
    app.on_config_update({"confirm_seconds": 2})                       # idempotent
    assert app.cfg.confirm_seconds == 2.0


def test_dashboard_state_has_the_views_the_manifest_declares():
    app, rec, src, clock = make_app(confirm_seconds=10)
    learn_reference(app, src, clock)
    run_ticks(app, src, clock, [_jpeg(Image.new("RGB", scene().size, (0, 0, 0)))] * 4)
    assert [a.alert_type for a in rec.alerts] == ["tamper_covered"]
    st = app.state_snapshot()
    for view in CameraTamper.manifest.state_schema:
        assert view.path.split(".")[0] in st, view.path
    assert st["watched"] == 1 and st["tampered_now"] == 1 and st["today"]["alerts"] == 1
    row = st["per_camera"][0]
    assert row["camera"] == "cam1" and row["condition"] == "covered" and row["alerts_today"] == 1
    assert row["has_reference"] is True and row["focus_pct"] is not None
    assert st["by_condition"] == [{"condition": "covered", "today": 1}]
    assert st["recent"][0]["message"] == "Camera cam1 covered" and st["recent"][0]["level"] == "alert"
    labels = [g["label"] for g in st["gallery"]]
    assert labels == ["cam1 — reference view", "Camera cam1 covered — now"]
    assert all(g["image"].startswith("data:image/jpeg;base64,") for g in st["gallery"])
    run_ticks(app, src, clock, [_jpeg(with_noise(scene(), 9))] * 2)
    assert rec.alerts[-1].alert_type == "tamper_cleared"
    st = app.state_snapshot()
    assert st["tampered_now"] == 0 and st["per_camera"][0]["condition"] == "normal"
    assert st["recent"][0]["level"] == "info"
