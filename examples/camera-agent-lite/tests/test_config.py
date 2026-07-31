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


# ── auth + TLS validation ────────────────────────────────────────────────

def _write(tmp_path, text):
    p = tmp_path / "config.yml"
    p.write_text(text, encoding="utf-8")
    return p


def test_auth_mode_enum_enforced(tmp_path):
    with pytest.raises(SystemExit, match="auth_mode"):
        load_config(_write(tmp_path, "auth_mode: banana\n"))


def test_opennvr_mode_requires_api_url(tmp_path):
    with pytest.raises(SystemExit, match="opennvr_api_url"):
        load_config(_write(tmp_path, "auth_mode: opennvr\n"))


def test_tls_pair_must_be_set_together(tmp_path):
    with pytest.raises(SystemExit, match="set together"):
        load_config(_write(tmp_path, "tls_certfile: /nope/cert.pem\n"))


def test_tls_files_must_exist(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        load_config(_write(
            tmp_path,
            "tls_certfile: /nope/cert.pem\ntls_keyfile: /nope/key.pem\n"))


def test_tls_pair_accepted_when_present(tmp_path):
    cert = tmp_path / "c.pem"; cert.write_text("x")
    key = tmp_path / "k.pem"; key.write_text("x")
    cfg = load_config(_write(
        tmp_path,
        f"tls_certfile: {cert.as_posix()}\ntls_keyfile: {key.as_posix()}\n"))
    assert cfg.tls_certfile == cert.as_posix()


def test_auth_opennvr_with_url_loads(tmp_path):
    cfg = load_config(_write(
        tmp_path,
        "auth_mode: opennvr\nopennvr_api_url: http://core:8000\n"))
    assert cfg.auth_mode == "opennvr"
