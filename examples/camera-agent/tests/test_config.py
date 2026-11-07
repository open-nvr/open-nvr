# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for camera-agent config loading + the per-camera role
prompt assembly."""
from __future__ import annotations

from pathlib import Path

import pytest

from camera_agent import (
    AppConfig,
    CameraAgentRuntime,
    load_config,
)


def _write(tmp_path: Path, body: str) -> Path:
    cfg = tmp_path / "c.yml"
    cfg.write_text(body)
    return cfg


def _minimal_yaml(frame_path: Path | None = None) -> str:
    frame_url = (
        f"file://{frame_path}" if frame_path is not None else "http://example.invalid/snap.jpg"
    )
    return (
        "kaic_url: http://x\n"
        "kaic_api_key: y\n"
        "cameras:\n"
        "  - camera_id: front-porch\n"
        f"    frame_url: {frame_url}\n"
        "    role: entrance\n"
    )


def _seed_frame(tmp_path: Path, name: str = "frame.jpg") -> Path:
    """Create a tiny placeholder file the FileFrameSource will accept.
    Contents don't matter for config-load tests; only existence does."""
    p = tmp_path / name
    p.write_bytes(b"\xff\xd8\xff\xe0fakejpeg")
    return p


def test_load_requires_kaic_url(tmp_path):
    cfg = _write(tmp_path, "kaic_api_key: y\ncameras: []\n")
    with pytest.raises(SystemExit, match="kaic_url"):
        load_config(cfg)


def test_load_requires_kaic_api_key(tmp_path):
    cfg = _write(tmp_path, "kaic_url: http://x\ncameras: []\n")
    with pytest.raises(SystemExit, match="kaic_api_key"):
        load_config(cfg)


def test_load_allows_empty_camera_list(tmp_path):
    # An empty camera list is intentionally allowed: the Docker install
    # ships ``cameras: []`` so the stack comes up before any RTSP source
    # is wired, and the agent still serves the demo + voice loop.
    cfg = _write(tmp_path, "kaic_url: http://x\nkaic_api_key: y\ncameras: []\n")
    loaded = load_config(cfg)
    assert loaded.cameras == []


def test_load_rejects_camera_without_id_or_url(tmp_path):
    cfg = _write(
        tmp_path,
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n  - role: x\n",
    )
    with pytest.raises(SystemExit, match="camera_id"):
        load_config(cfg)


def test_load_pulls_camera_role(tmp_path):
    parsed = load_config(_write(tmp_path, _minimal_yaml()))
    assert parsed.cameras[0].role == "entrance"


def test_load_uses_default_role_when_missing(tmp_path):
    parsed = load_config(_write(
        tmp_path,
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n  - {camera_id: a, frame_url: http://example.invalid/x}\n",
    ))
    assert "no role" in parsed.cameras[0].role


def test_load_rejects_non_numeric_temperature(tmp_path):
    cfg = _write(
        tmp_path,
        _minimal_yaml() + "llm_temperature: very-hot\n",
    )
    with pytest.raises(SystemExit, match="llm_temperature"):
        load_config(cfg)


def test_load_carries_llm_overrides(tmp_path):
    cfg = _write(
        tmp_path,
        _minimal_yaml() +
        "llm_model: llama3.1:8b\nllm_temperature: 0.2\nllm_max_tokens: 64\n",
    )
    parsed = load_config(cfg)
    assert parsed.llm_model == "llama3.1:8b"
    assert parsed.llm_temperature == pytest.approx(0.2)
    assert parsed.llm_max_tokens == 64


# ── opennvr_base_url single-URL derivation ─────────────────────────


def test_base_url_derives_api_and_ui_when_unset(tmp_path):
    """opennvr_base_url fills in opennvr_api_url + opennvr_ui_url when
    neither is set explicitly (the simple single-deployment path)."""
    parsed = load_config(_write(
        tmp_path,
        _minimal_yaml() + "opennvr_base_url: http://nvr.local:8000\n",
    ))
    assert parsed.opennvr_base_url == "http://nvr.local:8000"
    assert parsed.opennvr_api_url == "http://nvr.local:8000"
    assert parsed.opennvr_ui_url == "http://nvr.local:8000"


def test_base_url_does_not_derive_kaic_or_nats(tmp_path):
    """kaic_url / nats_inference_url are separate services and must NOT be
    derived from opennvr_base_url."""
    parsed = load_config(_write(
        tmp_path,
        _minimal_yaml() + "opennvr_base_url: http://nvr.local:8000\n",
    ))
    # kaic_url keeps the explicitly-configured value from _minimal_yaml.
    assert parsed.kaic_url == "http://x"
    assert parsed.nats_inference_url is None


def test_base_url_trailing_slash_is_normalised(tmp_path):
    parsed = load_config(_write(
        tmp_path,
        _minimal_yaml() + "opennvr_base_url: http://nvr.local:8000/\n",
    ))
    assert parsed.opennvr_base_url == "http://nvr.local:8000"
    assert parsed.opennvr_api_url == "http://nvr.local:8000"


def test_shipped_docker_configs_use_internal_api_url():
    """Regression: the shipped docker configs must pin opennvr_api_url to the
    compose-INTERNAL service origin. opennvr_base_url is the browser-facing
    host URL (localhost:8000); if it were left to derive opennvr_api_url, the
    AppRegistryClient inside the bridge-networked container would call itself
    on localhost and the app door would silently never work under compose."""
    import yaml

    cfg_dir = Path(__file__).resolve().parent.parent
    for name in ("config.docker.yml", "config.docker.chat.yml"):
        raw = yaml.safe_load((cfg_dir / name).read_text())
        api_url = raw.get("opennvr_api_url")
        assert api_url, f"{name}: opennvr_api_url must be set explicitly"
        assert "opennvr-core" in api_url, (
            f"{name}: opennvr_api_url must use the compose-internal service "
            f"name (got {api_url!r}) — localhost is the agent container itself"
        )
        assert "localhost" not in api_url and "127.0.0.1" not in api_url, (
            f"{name}: opennvr_api_url points at the agent container, not core"
        )


def test_explicit_fields_override_base_url(tmp_path):
    """Explicit per-field values always win over the derived base."""
    parsed = load_config(_write(
        tmp_path,
        _minimal_yaml()
        + "opennvr_base_url: http://nvr.local:8000\n"
        + "opennvr_api_url: http://api.example:9000\n"
        + "opennvr_ui_url: https://ui.example\n",
    ))
    assert parsed.opennvr_api_url == "http://api.example:9000"
    assert parsed.opennvr_ui_url == "https://ui.example"


def test_partial_override_derives_the_rest_from_base(tmp_path):
    """One explicit field wins; the unset sibling still derives from base."""
    parsed = load_config(_write(
        tmp_path,
        _minimal_yaml()
        + "opennvr_base_url: http://nvr.local:8000\n"
        + "opennvr_api_url: http://api.example:9000\n",
    ))
    assert parsed.opennvr_api_url == "http://api.example:9000"
    assert parsed.opennvr_ui_url == "http://nvr.local:8000"


def test_no_base_url_keeps_current_behaviour(tmp_path):
    """No opennvr_base_url → the sibling URLs behave exactly as before
    (explicit-or-None), fully backward-compatible."""
    # None configured → all None.
    parsed = load_config(_write(tmp_path, _minimal_yaml()))
    assert parsed.opennvr_base_url is None
    assert parsed.opennvr_api_url is None
    assert parsed.opennvr_ui_url is None

    # Per-field still settable individually with no base.
    parsed2 = load_config(_write(
        tmp_path,
        _minimal_yaml()
        + "opennvr_api_url: http://api.example:9000\n"
        + "opennvr_ui_url: https://ui.example\n",
    ))
    assert parsed2.opennvr_base_url is None
    assert parsed2.opennvr_api_url == "http://api.example:9000"
    assert parsed2.opennvr_ui_url == "https://ui.example"


# ── System-prompt assembly ─────────────────────────────────────────


def test_system_prompt_includes_camera_roster(tmp_path):
    """The LLM should see the configured camera ids + roles so it
    can map natural language ('the front door') to camera_id values."""
    f1 = _seed_frame(tmp_path, "porch.jpg")
    f2 = _seed_frame(tmp_path, "back.jpg")
    parsed = load_config(_write(
        tmp_path,
        "kaic_url: http://x\nkaic_api_key: y\n"
        "cameras:\n"
        f"  - {{camera_id: front-porch, frame_url: 'file://{f1}', role: 'main entrance'}}\n"
        f"  - {{camera_id: back-door,  frame_url: 'file://{f2}', role: 'garden facing'}}\n",
    ))
    runtime = CameraAgentRuntime(parsed)
    prompt = runtime.build_system_prompt()
    assert "front-porch" in prompt and "main entrance" in prompt
    assert "back-door" in prompt and "garden facing" in prompt
    assert "exactly as listed" in prompt


# ── opennvr_cameras_url auto-discovery ────────────────────────────


class TestLoadOpenNvrCameras:
    """Tests for the HTTP-based camera discovery path.

    We patch ``httpx.get`` so no real network calls are made.
    """

    def _make_response(self, cameras: list[dict], status: int = 200):
        """Return a minimal httpx.Response-shaped mock."""
        import json
        from unittest.mock import MagicMock

        mock = MagicMock()
        mock.status_code = status
        mock.json.return_value = {"cameras": cameras}
        mock.raise_for_status.return_value = None
        if status >= 400:
            import httpx
            mock.raise_for_status.side_effect = httpx.HTTPStatusError(
                message="error",
                request=MagicMock(),
                response=mock,
            )
        return mock

    def test_cameras_loaded_from_opennvr_when_list_empty(self, tmp_path, monkeypatch):
        """When cameras: [] and opennvr_cameras_url is set, the agent
        should call the endpoint and populate the camera list."""
        import httpx
        from unittest.mock import patch

        opennvr_camera = {
            "camera_id": "cam1",
            "frame_url": "rtsp://mediamtx:8554/cam-1",
            "role": "Front door; location: hallway",
        }

        with patch("httpx.get", return_value=self._make_response([opennvr_camera])):
            cfg_text = (
                "kaic_url: http://x\n"
                "kaic_api_key: y\n"
                "cameras: []\n"
                "opennvr_cameras_url: http://opennvr-core:8000/api/v1/internal/camera-agent/cameras\n"
                "opennvr_api_key: y\n"
            )
            parsed = load_config(_write(tmp_path, cfg_text))

        assert len(parsed.cameras) == 1
        assert parsed.cameras[0].camera_id == "cam1"
        assert "rtsp://mediamtx" in parsed.cameras[0].frame_url
        assert "Front door" in parsed.cameras[0].role

    def test_static_cameras_take_priority_over_opennvr_url(self, tmp_path, monkeypatch):
        """If cameras are explicitly listed they should be used and the
        OpenNVR endpoint should NOT be called."""
        from unittest.mock import patch, MagicMock

        fake_get = MagicMock()
        f = _seed_frame(tmp_path, "f.jpg")
        with patch("httpx.get", fake_get):
            cfg_text = (
                "kaic_url: http://x\n"
                "kaic_api_key: y\n"
                f"cameras:\n  - {{camera_id: cam-local, frame_url: 'file://{f}', role: local}}\n"
                "opennvr_cameras_url: http://opennvr-core:8000/api/v1/internal/camera-agent/cameras\n"
                "opennvr_api_key: y\n"
            )
            parsed = load_config(_write(tmp_path, cfg_text))

        fake_get.assert_not_called()
        assert len(parsed.cameras) == 1
        assert parsed.cameras[0].camera_id == "cam-local"

    def test_graceful_failure_on_network_error(self, tmp_path):
        """If the OpenNVR endpoint is unreachable, the agent should start
        with an empty camera list rather than crashing."""
        import httpx
        from unittest.mock import patch

        with patch("httpx.get", side_effect=httpx.ConnectError("unreachable")):
            cfg_text = (
                "kaic_url: http://x\n"
                "kaic_api_key: y\n"
                "cameras: []\n"
                "opennvr_cameras_url: http://opennvr-core:8000/api/v1/internal/camera-agent/cameras\n"
                "opennvr_api_key: y\n"
            )
            parsed = load_config(_write(tmp_path, cfg_text))

        # Should not raise; cameras list is empty but agent still starts.
        assert parsed.cameras == []

    def test_graceful_failure_on_bad_response_shape(self, tmp_path):
        """If the endpoint returns JSON without a 'cameras' key,
        the agent should start with an empty camera list."""
        from unittest.mock import patch, MagicMock

        bad_response = MagicMock()
        bad_response.status_code = 200
        bad_response.json.return_value = {"error": "unexpected"}
        bad_response.raise_for_status.return_value = None

        with patch("httpx.get", return_value=bad_response):
            cfg_text = (
                "kaic_url: http://x\n"
                "kaic_api_key: y\n"
                "cameras: []\n"
                "opennvr_cameras_url: http://opennvr-core:8000/api/v1/internal/camera-agent/cameras\n"
                "opennvr_api_key: y\n"
            )
            parsed = load_config(_write(tmp_path, cfg_text))

        assert parsed.cameras == []

    def test_entries_without_camera_id_or_frame_url_are_skipped(self, tmp_path):
        """Incomplete entries in the OpenNVR response (missing required
        fields) should be silently skipped rather than crashing."""
        from unittest.mock import patch

        cameras = [
            {"camera_id": "cam1", "frame_url": "rtsp://mediamtx:8554/cam-1", "role": "good"},
            {"camera_id": "cam2"},        # missing frame_url — should be skipped
            {"frame_url": "rtsp://x"},    # missing camera_id — should be skipped
        ]
        with patch("httpx.get", return_value=self._make_response(cameras)):
            cfg_text = (
                "kaic_url: http://x\n"
                "kaic_api_key: y\n"
                "cameras: []\n"
                "opennvr_cameras_url: http://opennvr-core:8000/api/v1/internal/camera-agent/cameras\n"
                "opennvr_api_key: y\n"
            )
            parsed = load_config(_write(tmp_path, cfg_text))

        assert len(parsed.cameras) == 1
        assert parsed.cameras[0].camera_id == "cam1"


def test_llm_prewarm_is_on_by_default_and_off_when_the_installer_says_so(tmp_path):
    """The start-up pre-warm costs minutes of every core on a CPU box; the
    installer writes LLM_PREWARM=false there and the template passes it
    through as a string, so 'false' and '0' must read as off."""
    base = "kaic_url: http://x\nkaic_api_key: y\ncameras: []\n"
    assert load_config(_write(tmp_path, base)).llm_prewarm is True
    assert load_config(_write(tmp_path, base + "llm_prewarm: false\n")).llm_prewarm is False
    assert load_config(_write(tmp_path, base + "llm_prewarm: '0'\n")).llm_prewarm is False
    assert load_config(_write(tmp_path, base + "llm_prewarm: ${LLM_PREWARM}\n")).llm_prewarm is True, \
        "an unsubstituted placeholder keeps the default"
    assert load_config(_write(tmp_path, base + "llm_prewarm: true\n")).llm_prewarm is True
