# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Config loading: YAML only, no .env, no environment machinery."""
import pytest

from camera_agent import Config, load_config


def test_defaults():
    cfg = Config()
    assert cfg.listen_port == 9101
    assert cfg.llm_url.endswith(":9014")
    assert cfg.cameras == []
    assert cfg.opennvr_cameras_url == ""


def test_load_from_yaml(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(
        "adapter_token: tok\n"
        "opennvr_cameras_url: http://core:8000/api/v1/internal/camera-agent/cameras\n"
        "opennvr_api_key: key\n"
        "llm_max_tokens: 99\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.adapter_token == "tok"
    assert cfg.opennvr_api_key == "key"
    assert cfg.llm_max_tokens == 99


def test_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit, match="config file not found"):
        load_config(tmp_path / "nope.yml")


def test_static_cameras_parse(tmp_path):
    p = tmp_path / "config.yml"
    p.write_text(
        "cameras:\n"
        "  - camera_id: camera_1\n"
        "    name: Door\n"
        "    frame_url: file:///tmp/x.jpg\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.cameras[0]["camera_id"] == "camera_1"
