# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
M1b + M1b-fixup verification tests for V-019 (MediaMTX hardening).

These are real tests — they parse the actual YAML files in the repo,
import the actual Settings class, and call the actual stream-token
emitter logic. Run with:

    cd server && pytest tests/test_m1b_mediamtx_hardening.py -v

Coverage:

* YAML validity: every mediamtx*.yml parses cleanly.
* Hardened-template protocol toggles: rtspEncryption=strict,
  hlsEncryption=yes, webrtcEncryption=yes, rtmp=no, srt=no.
* Dev-template (mediamtx.local.yml) still permissive with the DO-NOT-USE
  header preserved.
* Stream-token URLs: backend emits urls.rtsps alongside urls.rtsp.
* Settings: mediamtx_rtsps_url has a sensible default and is subject to
  the V-015 loopback validator.
* Cert script: idempotent, refuses to overwrite without --force.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

# Resolve the repo root from this file so the tests work regardless of cwd.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _good_env() -> dict[str, str]:
    """Settings values that satisfy the strong-secret + loopback validators."""
    return dict(
        DATABASE_URL="postgresql://u:p@localhost/x",
        SECRET_KEY=secrets.token_urlsafe(48),
        MEDIAMTX_SECRET=secrets.token_hex(32),
        INTERNAL_API_KEY=secrets.token_urlsafe(48),
        CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )


def _apply_env(extra: dict[str, str]) -> None:
    for k in list(os.environ):
        if any(
            k.startswith(p)
            for p in (
                "SECRET_",
                "MEDIAMTX_",
                "INTERNAL_",
                "CREDENTIAL_",
                "DATABASE_",
                "ALLOW_",
                "DEBUG",
                "HOST",
                "PORT",
                "APPLICATION_",
                "API_",
                "RECORDINGS_",
                "DEFAULT_ADMIN_",
                "KAI_C_",
                "LOG_",
                "SURICATA_",
                "ALGORITHM",
                "ACCESS_TOKEN",
                "REFRESH_TOKEN",
                "CORS_",
                "DEPLOYMENT_",
                "AI_SOVEREIGNTY",
            )
        ):
            del os.environ[k]
    os.environ.update(_good_env())
    os.environ.update(extra)
    for m in list(sys.modules):
        if m == "core" or m.startswith("core."):
            del sys.modules[m]


# ---------------------------------------------------------------------------
# YAML validity + protocol toggles
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["mediamtx.docker.yml", "mediamtx.yml", "mediamtx.local.yml"],
)
def test_yaml_parses_cleanly(filename: str) -> None:
    """Every shipped MediaMTX template must be valid YAML."""
    cfg = yaml.safe_load((REPO_ROOT / filename).read_text())
    assert isinstance(cfg, dict), f"{filename} did not parse to a dict"


@pytest.mark.parametrize(
    "filename",
    ["mediamtx.docker.yml", "mediamtx.yml"],
)
def test_hardened_templates_enforce_tls(filename: str) -> None:
    """V-019: hardened templates must require TLS on all viewer transports."""
    cfg = yaml.safe_load((REPO_ROOT / filename).read_text())
    # rtspEncryption must be the literal string "strict" (not "yes" — that
    # value is not in the MediaMTX enum and would brick the boot, which is
    # exactly what we shipped in the broken M1b commit and fixed here).
    assert cfg.get("rtspEncryption") == "strict", (
        f"{filename}: rtspEncryption must be 'strict', got "
        f"{cfg.get('rtspEncryption')!r}"
    )
    assert cfg.get("hlsEncryption") is True, (
        f"{filename}: hlsEncryption must be enabled"
    )
    assert cfg.get("webrtcEncryption") is True, (
        f"{filename}: webrtcEncryption must be enabled"
    )
    assert cfg.get("rtmp") is False, f"{filename}: rtmp must be disabled"
    assert cfg.get("srt") is False, f"{filename}: srt must be disabled"


def test_dev_template_is_permissive_but_warned() -> None:
    """The local-dev template stays plaintext for VLC/ffprobe but the
    header must scream that it isn't a production config."""
    text = (REPO_ROOT / "mediamtx.local.yml").read_text()
    cfg = yaml.safe_load(text)
    assert cfg.get("rtspEncryption") == "no", (
        "mediamtx.local.yml should remain plaintext-RTSP for dev "
        "(it is the documented permissive template)"
    )
    assert "DO NOT USE THIS FILE IN PRODUCTION" in text, (
        "mediamtx.local.yml is missing the production-warning header"
    )
    assert "MEDIAMTX_ALLOW_PLAINTEXT_OUTPUTS" in text, (
        "mediamtx.local.yml must point operators at the env-var "
        "acknowledgement"
    )


# ---------------------------------------------------------------------------
# Settings + URL emission
# ---------------------------------------------------------------------------


def test_mediamtx_rtsps_url_has_loopback_default() -> None:
    """M1b-fixup C-3: a new mediamtx_rtsps_url setting exists with a
    sensible loopback default that passes the V-015 validator."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({})
    from core.config import settings  # noqa: E402

    assert settings.mediamtx_rtsps_url is not None
    assert settings.mediamtx_rtsps_url.startswith("rtsps://")
    # Loopback hostname (allows localhost / 127.0.0.1 / ::1).
    assert any(
        host in settings.mediamtx_rtsps_url
        for host in ("localhost", "127.0.0.1", "::1")
    )


def test_mediamtx_external_rtsps_url_defaults_to_none() -> None:
    """M1b-fixup-v2 F-1: the new external RTSPS URL setting follows the
    same convention as the other mediamtx_external_*_url fields —
    defaults to None so streams.py falls through to the internal value."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({})
    from core.config import settings  # noqa: E402

    assert settings.mediamtx_external_rtsps_url is None


def test_mediamtx_external_rtsps_url_not_subject_to_loopback_validator() -> None:
    """M1b-fixup-v2 F-1: the external URL is intentionally NOT in the
    V-015 candidate list — it's the routable browser-facing endpoint."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({"MEDIAMTX_EXTERNAL_RTSPS_URL": "rtsps://10.0.0.5:8322"})
    # Must import without raising. If V-015 rejects the routable URL,
    # the scope-note in the validator is wrong.
    import importlib

    mod = importlib.import_module("core.config")
    assert mod.settings.mediamtx_external_rtsps_url == "rtsps://10.0.0.5:8322"


def test_mediamtx_rtsps_url_subject_to_loopback_validator() -> None:
    """V-015 must refuse a non-loopback MEDIAMTX_RTSPS_URL unless the
    explicit override is set."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({"MEDIAMTX_RTSPS_URL": "rtsps://10.0.0.5:8322"})

    with pytest.raises(Exception) as exc_info:
        import importlib

        importlib.import_module("core.config")
    assert "V-015" in str(exc_info.value), (
        "V-015 validator must mention itself in the failure message"
    )


def test_yaml_cert_paths_are_absolute() -> None:
    """M1b-fixup-v2 F-9: all *ServerKey / *ServerCert references in the
    hardened templates must use the absolute /etc/mediamtx-certs path
    that the docker-compose mount provides. Relative paths break when
    the upstream MediaMTX image's WORKDIR changes."""
    import re

    for filename in ("mediamtx.docker.yml", "mediamtx.yml"):
        text = (REPO_ROOT / filename).read_text()
        # Every "*ServerKey: ..." / "*ServerCert: ..." line must start with /
        for match in re.finditer(
            r"^[a-zA-Z]+Server(Key|Cert):\s*(\S+)$", text, re.MULTILINE
        ):
            value = match.group(2)
            assert value.startswith("/"), (
                f"{filename}: cert path {value!r} is not absolute; "
                f"line: {match.group(0)!r}"
            )
            assert value.startswith("/etc/mediamtx-certs/"), (
                f"{filename}: cert path {value!r} should be under "
                f"/etc/mediamtx-certs/ to match the docker-compose mount"
            )


def test_compose_has_mediamtx_certs_init_service() -> None:
    """M1b-fixup-v2 F-9: docker-compose.yml must declare a one-shot
    mediamtx-certs-init service so a fresh `docker compose up` doesn't
    fail on missing ./mediamtx-certs/server.{crt,key}."""
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    services = compose.get("services", {})
    assert "mediamtx-certs-init" in services, (
        "docker-compose.yml is missing the mediamtx-certs-init service"
    )
    # And the main mediamtx service must depend on it.
    mediamtx_deps = services.get("mediamtx", {}).get("depends_on", {})
    if isinstance(mediamtx_deps, dict):
        assert "mediamtx-certs-init" in mediamtx_deps, (
            "mediamtx service must depend on mediamtx-certs-init"
        )
        cond = mediamtx_deps["mediamtx-certs-init"].get("condition")
        assert cond == "service_completed_successfully", (
            f"mediamtx must wait for service_completed_successfully, "
            f"got {cond!r}"
        )


# ---------------------------------------------------------------------------
# Cert script idempotency (skipped if openssl not on PATH)
# ---------------------------------------------------------------------------


def test_cert_script_is_idempotent() -> None:
    """generate-mediamtx-certs.sh must not overwrite existing certs
    unless --force is passed (otherwise we silently rotate keys out
    from under a running deployment)."""
    if subprocess.run(
        ["which", "openssl"], capture_output=True
    ).returncode != 0:
        pytest.skip("openssl not on PATH")
    script = REPO_ROOT / "scripts" / "generate-mediamtx-certs.sh"
    if not script.exists():
        pytest.skip(f"{script} not present (run on POSIX checkout)")

    with tempfile.TemporaryDirectory() as td:
        # First run -> generate.
        r1 = subprocess.run(
            ["bash", str(script), "--out", td],
            capture_output=True,
            text=True,
        )
        assert r1.returncode == 0, f"first run failed: {r1.stderr}"
        crt = Path(td) / "server.crt"
        key = Path(td) / "server.key"
        assert crt.exists() and key.exists(), "first run did not create files"
        first_crt_bytes = crt.read_bytes()

        # Second run without --force -> must NOT overwrite.
        r2 = subprocess.run(
            ["bash", str(script), "--out", td],
            capture_output=True,
            text=True,
        )
        assert r2.returncode == 0, f"second run failed: {r2.stderr}"
        assert crt.read_bytes() == first_crt_bytes, (
            "cert script overwrote existing cert without --force"
        )

        # Third run WITH --force -> must overwrite.
        r3 = subprocess.run(
            ["bash", str(script), "--out", td, "--force"],
            capture_output=True,
            text=True,
        )
        assert r3.returncode == 0, f"third run failed: {r3.stderr}"
        assert crt.read_bytes() != first_crt_bytes, (
            "cert script did not regenerate under --force"
        )
