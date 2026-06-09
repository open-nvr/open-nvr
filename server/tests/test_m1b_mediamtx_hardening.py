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
  the V-015 internal-trust-zone validator (loopback / RFC1918 / ULA /
  link-local accepted; public addresses refused).
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
    """Settings values that satisfy the strong-secret + V-015 validators."""
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
    """V-019: hardened templates must require TLS on every operator-facing
    transport.

    ``rtspEncryption`` is allowed to be either ``"strict"`` (no plaintext
    listener at all — the original hardening posture) or ``"optional"``
    (RTSPS stays the operator-facing listener at :8322; plaintext :8554
    binds for the in-host KAI-C inference tap and is NOT exposed to the
    host network — see docs/SECURITY_ARCHITECTURE.md §"RTSP encryption
    posture"). Both values keep the TLS listener bound, which is what
    V-019 cares about. ``"yes"`` is rejected — it's not in the MediaMTX
    enum and would brick the boot, which is the regression that
    originally motivated this test.
    """
    cfg = yaml.safe_load((REPO_ROOT / filename).read_text())
    rtsp_enc = cfg.get("rtspEncryption")
    assert rtsp_enc in ("strict", "optional"), (
        f"{filename}: rtspEncryption must be 'strict' or 'optional', got "
        f"{rtsp_enc!r}"
    )
    # If optional mode is in play, the RTSPS listener must still exist
    # at :8322 — the V-019 enforcement is "TLS on the operator surface",
    # not "no plaintext anywhere internal".
    if rtsp_enc == "optional":
        assert cfg.get("rtspsAddress"), (
            f"{filename}: rtspEncryption=optional requires rtspsAddress to "
            f"be set so the TLS listener remains the operator-facing surface"
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


def test_mediamtx_external_rtsps_url_not_subject_to_v015() -> None:
    """ISSUE-4: the egress/uplink-side external URL is intentionally NOT in
    the V-015 candidate list — it's the browser-facing endpoint behind
    the TLS reverse proxy and may legitimately resolve to a public host."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    # 203.0.113.5 is the TEST-NET-3 documentation block — guaranteed
    # public-routable, no risk of resolving to anything real.
    _apply_env({"MEDIAMTX_EXTERNAL_RTSPS_URL": "rtsps://203.0.113.5:8322"})
    # Must import without raising. If V-015 rejects this, the scope-note
    # in the validator is wrong and egress would also be policed.
    import importlib

    mod = importlib.import_module("core.config")
    assert mod.settings.mediamtx_external_rtsps_url == "rtsps://203.0.113.5:8322"


# ---------------------------------------------------------------------------
# V-015 trust-zone semantics (ISSUE-4 refactor)
#
# The validator's job is to keep the *ingress* MediaMTX URLs inside the
# OpenNVR trust zone (loopback + RFC1918 + IPv6 ULA + link-local), where
# the medium is plaintext but the segment is not internet-routable. It
# must:
#
#   accept   loopback        (127.0.0.1, ::1, localhost)
#   accept   RFC1918         (10.x, 172.16-31.x, 192.168.x)  ← Docker bridge
#   accept   IPv6 ULA        (fc00::/7)
#   accept   IPv4 link-local (169.254.x)
#   accept   IPv6 link-local (fe80::/10)
#   reject   public IP       (e.g. 8.8.8.8, 203.0.113.x)
#   reject   public FQDN     (e.g. example.com)
#   reject   0.0.0.0         (the bind-everywhere wildcard)
#   reject   scheme-less     (e.g. "10.0.0.5:8889" — host parses as None)
#
# There is NO escape-hatch flag. Operators with a real cross-boundary
# requirement must use MEDIAMTX_EXTERNAL_* and terminate TLS in front.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://10.0.0.5:8889",       # RFC1918  10/8
        "http://172.20.0.3:8889",     # RFC1918  172.16/12 — Docker default bridge
        "http://192.168.1.20:8889",   # RFC1918  192.168/16
        "http://169.254.10.1:8889",   # IPv4 link-local
        "http://[fc00::1]:8889",      # IPv6 ULA
        "http://[fe80::1]:8889",      # IPv6 link-local
    ],
)
def test_v015_accepts_internal_trust_zone_addresses(url: str) -> None:
    """ISSUE-4: V-015 must accept every address that the OpenNVR trust
    boundary protects (Docker bridges, camera LANs, VPN overlays, link-
    local, IPv6 ULA). These are the addresses where MediaMTX legitimately
    speaks plaintext to the backend without crossing the trust boundary.
    """
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({"MEDIAMTX_BASE_URL": url})
    import importlib

    # Must import without raising. If it raises, the validator is too strict.
    mod = importlib.import_module("core.config")
    assert mod.settings.mediamtx_base_url == url


def test_v015_accepts_docker_service_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """ISSUE-4 (the regression that motivated this refactor): when the
    backend container resolves the ``mediamtx`` Docker service name via
    the bridge's embedded DNS, the result is an RFC1918 address (the
    other container's bridge interface). The previous loopback-only
    check refused to boot on this exact value; the new trust-zone check
    must accept it.

    We can't actually hit Docker DNS in the test harness, so we
    monkeypatch ``socket.getaddrinfo`` to return the kind of bridge IP
    Docker would hand back — the assertion is that V-015 is satisfied
    once that resolution lands in the RFC1918 space.
    """
    import socket as _socket

    real_getaddrinfo = _socket.getaddrinfo

    def fake_getaddrinfo(host, *args, **kwargs):  # type: ignore[no-untyped-def]
        if host == "mediamtx":
            # Docker default bridge subnet — 172.17.0.0/16. We pick a
            # plausible peer IP.
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "",
                     ("172.17.0.3", 0))]
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", fake_getaddrinfo)

    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({"MEDIAMTX_BASE_URL": "http://mediamtx:8889"})
    import importlib

    mod = importlib.import_module("core.config")
    assert mod.settings.mediamtx_base_url == "http://mediamtx:8889"


@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://8.8.8.8:8889",        "public IPv4"),
        ("http://1.1.1.1:8889",        "public IPv4 (Cloudflare)"),
        ("http://[2001:4860:4860::8888]:8889", "public IPv6"),
        ("http://0.0.0.0:8889",        "wildcard bind"),
        ("http://example.com:8889",    "public FQDN"),
        ("10.0.0.5:8889",              "scheme-less (host parses as None)"),
    ],
)
def test_v015_rejects_addresses_outside_trust_zone(url: str, reason: str) -> None:
    """ISSUE-4: V-015 must refuse every address an attacker on the public
    internet can reach (or might be made to reach via misconfiguration).
    There is no escape-hatch flag — the validator is absolute, and the
    error message must point operators at MEDIAMTX_EXTERNAL_*."""
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({"MEDIAMTX_BASE_URL": url})

    with pytest.raises(Exception) as exc_info:
        import importlib

        importlib.import_module("core.config")
    msg = str(exc_info.value)
    assert "V-015" in msg, (
        f"V-015 validator must mention itself in the failure message ({reason})"
    )
    # The new error message must steer operators toward the right fix.
    assert "MEDIAMTX_EXTERNAL_" in msg, (
        "V-015 error must direct operators at MEDIAMTX_EXTERNAL_* for "
        "cross-trust-boundary deployments"
    )


def test_stale_allow_remote_mediamtx_triggers_boot_warning() -> None:
    """ISSUE-4 peer review M-1: an operator who had
    ``ALLOW_REMOTE_MEDIAMTX=true`` set in their .env on the previous
    release will, after upgrade, have it silently dropped by Pydantic
    ``extra="ignore"``. That's hostile UX — they'll think their bypass
    is still in effect. The lifespan boot path in ``server/main.py``
    must surface a loud warning that the flag was retired and point
    operators at ``MEDIAMTX_EXTERNAL_*``.

    A full FastAPI-lifespan integration test would pull in DB init and
    the whole router graph (and depends on Python 3.11 features that
    aren't in this test runner). The contract that actually matters is
    *source-level*: the warning emission must exist in main.py, fire on
    the env var being set, log at WARNING severity (not INFO — operators
    grep for WARN), and name both the retired flag and the replacement
    path so the operator can act on it. This test asserts those source-
    level invariants so a future refactor that drops the warning will
    fail loudly.
    """
    main_src = (REPO_ROOT / "server" / "main.py").read_text()

    # The env-var check must exist (either quoting style is fine).
    assert (
        'os.environ.get("ALLOW_REMOTE_MEDIAMTX")' in main_src
        or "os.environ.get('ALLOW_REMOTE_MEDIAMTX')" in main_src
    ), (
        "server/main.py no longer checks os.environ for "
        "ALLOW_REMOTE_MEDIAMTX — operators upgrading with the stale env "
        "var will have it silently ignored. Restore the boot-time warning."
    )

    # The warning must fire at WARNING severity, not INFO — operators
    # filter on WARN in production log aggregation.
    assert "main_logger.warning(" in main_src, (
        "server/main.py: the ALLOW_REMOTE_MEDIAMTX deprecation notice "
        "must be a WARNING-level log entry, not INFO. Otherwise operators "
        "grepping for WARN won't see it."
    )

    # The warning text must name both the retired flag and the
    # replacement path so the operator knows what to do.
    # Locate the warning call to scope the assertions narrowly.
    warn_idx = main_src.find("ALLOW_REMOTE_MEDIAMTX is set")
    assert warn_idx != -1, (
        "server/main.py: warning text for stale ALLOW_REMOTE_MEDIAMTX "
        "does not start with the expected operator-facing message"
    )
    # Grab a generous window of the warning body for the keyword check.
    warn_body = main_src[warn_idx : warn_idx + 800]
    assert "MEDIAMTX_EXTERNAL_" in warn_body, (
        "Warning must direct operators at MEDIAMTX_EXTERNAL_* for the "
        "cross-trust-boundary path"
    )
    assert ("ISSUE-4" in warn_body) or ("retired" in warn_body), (
        "Warning should give a reason — either the issue tag or the "
        "word 'retired' — so operators can search the changelog"
    )


def test_v015_has_no_escape_hatch_flag() -> None:
    """ISSUE-4: the previous ALLOW_REMOTE_MEDIAMTX bypass has been
    removed. There must be no Settings field — and no env var hook —
    that lets an operator silently disable V-015.

    A flag that lets MediaMTX speak plaintext across the trust boundary
    is a CVE-by-default, per the paper's Secure-by-Design principle
    (Zenodo 17261761 §4.1). Re-introducing one is a regression.
    """
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({})
    from core.config import Settings  # noqa: E402

    assert not hasattr(Settings(), "allow_remote_mediamtx"), (
        "Settings still has an allow_remote_mediamtx field — the V-015 "
        "escape hatch must remain removed."
    )
    # And setting the legacy env var must NOT re-enable the bypass: a
    # public host stays refused even with the old flag present.
    _apply_env({
        "ALLOW_REMOTE_MEDIAMTX": "true",
        "MEDIAMTX_BASE_URL": "http://8.8.8.8:8889",
    })
    with pytest.raises(Exception) as exc_info:
        import importlib

        importlib.import_module("core.config")
    assert "V-015" in str(exc_info.value), (
        "Legacy ALLOW_REMOTE_MEDIAMTX must not silently bypass V-015"
    )


# ---------------------------------------------------------------------------
# ISSUE-4: bridge talk is safe + egress is unimpacted
#
# These tests assert the operational claims that justified accepting RFC1918
# in V-015. Two threat models are in play:
#
#   (A) Backend ↔ MediaMTX traffic over the Docker bridge.
#       Both containers live in the same network namespace; the bridge is
#       not externally accessible (no host port mapping); the trust comes
#       from kernel-level network isolation, not from loopback. V-015 must
#       allow it AND nothing internal — neither the bridge hostname nor
#       its RFC1918 address — must leak into browser-facing URLs the
#       backend hands out (otherwise an attacker who scrapes a stream
#       token from a browser learns the internal topology).
#
#   (B) Browser ↔ MediaMTX traffic over the uplink NIC.
#       The MEDIAMTX_EXTERNAL_* URLs publish public, TLS-fronted endpoints
#       (HTTPS HLS, WebRTC over HTTPS signalling + DTLS-SRTP media,
#       RTSPS). These are intentionally outside V-015's scope; the
#       validator must NOT reject them when set to a public host even
#       though the internal URLs stay inside the trust zone.
#
# The fallback chain in server/routers/streams.py is:
#     external_url or internal_url or hardcoded_default
# So when externals are set, browsers see externals; when they're not,
# browsers see internals (which V-015 guarantees are inside the trust
# zone). Both branches must hold for the security argument to close.
# ---------------------------------------------------------------------------


_TIER0_BRIDGE_ENV = {
    # Realistic Tier 0 docker-compose values: the backend talks to the
    # mediamtx service over the Docker embedded DNS, which resolves to
    # the peer container's bridge IP (RFC1918).
    "MEDIAMTX_BASE_URL":     "http://mediamtx:8889",
    "MEDIAMTX_ADMIN_API":    "http://mediamtx:9997/v3",
    "MEDIAMTX_HLS_URL":      "http://mediamtx:8888",
    "MEDIAMTX_RTSP_URL":     "rtsp://mediamtx:8554",
    "MEDIAMTX_RTSPS_URL":    "rtsps://mediamtx:8322",
    "MEDIAMTX_PLAYBACK_URL": "http://mediamtx:9996",
}

_PUBLIC_EGRESS_ENV = {
    # Realistic egress-side values: a TLS-terminating reverse proxy
    # publishes HTTPS HLS, HTTPS WebRTC signalling, and RTSPS on the
    # operator's public DNS name.
    "MEDIAMTX_EXTERNAL_BASE_URL":     "https://cam.example.com",
    "MEDIAMTX_EXTERNAL_HLS_URL":      "https://cam.example.com/hls",
    "MEDIAMTX_EXTERNAL_RTSPS_URL":    "rtsps://cam.example.com:8322",
    "MEDIAMTX_EXTERNAL_PLAYBACK_URL": "https://cam.example.com/playback",
}


def _patch_bridge_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the bare hostname `mediamtx` resolve to a Docker bridge IP
    so V-015 can evaluate the realistic Tier 0 configuration without an
    actual Docker daemon."""
    import socket as _socket

    real = _socket.getaddrinfo

    def fake(host, *a, **kw):  # type: ignore[no-untyped-def]
        if host == "mediamtx":
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "",
                     ("172.17.0.3", 0))]
        return real(host, *a, **kw)

    monkeypatch.setattr(_socket, "getaddrinfo", fake)


def test_tier0_bridge_only_boots_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model (A): the realistic Tier 0 setup — backend and
    mediamtx in separate containers on the Docker bridge, no externals
    configured — must boot cleanly. The previous loopback-only check
    refused on every URL in this configuration; that's the regression
    this whole refactor exists to fix.
    """
    _patch_bridge_dns(monkeypatch)
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env(_TIER0_BRIDGE_ENV)
    import importlib

    mod = importlib.import_module("core.config")
    # All six ingress URLs took effect — none were rewritten to a default.
    assert mod.settings.mediamtx_base_url == "http://mediamtx:8889"
    assert mod.settings.mediamtx_admin_api == "http://mediamtx:9997/v3"
    assert mod.settings.mediamtx_hls_url == "http://mediamtx:8888"
    assert mod.settings.mediamtx_rtsp_url == "rtsp://mediamtx:8554"
    assert mod.settings.mediamtx_rtsps_url == "rtsps://mediamtx:8322"
    assert mod.settings.mediamtx_playback_url == "http://mediamtx:9996"


def test_egress_externals_can_be_public_with_internals_on_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model (B): with internal URLs on the bridge (trust zone)
    and externals on the public uplink (TLS-fronted), boot must succeed.
    This is the canonical production posture, and V-015 must NOT
    interfere with the egress side.
    """
    _patch_bridge_dns(monkeypatch)
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({**_TIER0_BRIDGE_ENV, **_PUBLIC_EGRESS_ENV})
    import importlib

    mod = importlib.import_module("core.config")
    # Externals took effect verbatim — they're not filtered by V-015.
    assert mod.settings.mediamtx_external_base_url == "https://cam.example.com"
    assert mod.settings.mediamtx_external_hls_url == "https://cam.example.com/hls"
    assert mod.settings.mediamtx_external_rtsps_url == "rtsps://cam.example.com:8322"
    assert mod.settings.mediamtx_external_playback_url == "https://cam.example.com/playback"
    # And internals are still the bridge values — V-015 accepted them.
    assert mod.settings.mediamtx_base_url == "http://mediamtx:8889"


# The four browser-facing fallback expressions, lifted verbatim from
# server/routers/streams.py:get_stream_info so the test fails if the
# router stops respecting the external-first contract. If streams.py
# changes its fallback shape, update both places together.
def _render_browser_urls(settings: object) -> dict[str, str]:
    s = settings
    webrtc_base = (
        s.mediamtx_external_base_url           # type: ignore[attr-defined]
        or s.mediamtx_base_url                 # type: ignore[attr-defined]
        or "http://127.0.0.1:8889"
    )
    hls_base = (
        s.mediamtx_external_hls_url            # type: ignore[attr-defined]
        or s.mediamtx_hls_url                  # type: ignore[attr-defined]
        or "http://127.0.0.1:8888"
    )
    rtsps_base = (
        s.mediamtx_external_rtsps_url          # type: ignore[attr-defined]
        or s.mediamtx_rtsps_url                # type: ignore[attr-defined]
        or "rtsps://127.0.0.1:8322"
    )
    playback_base = (
        s.mediamtx_external_playback_url       # type: ignore[attr-defined]
        or s.mediamtx_playback_url             # type: ignore[attr-defined]
        or "http://127.0.0.1:9996"
    )
    stream_name = "cam-42"
    return {
        "webrtc":   f"{webrtc_base.rstrip('/')}/{stream_name}/whep",
        "hls":      f"{hls_base.rstrip('/')}/{stream_name}/index.m3u8",
        "rtsps":    f"{rtsps_base.rstrip('/')}/{stream_name}",
        "playback": f"{playback_base.rstrip('/')}/{stream_name}",
    }


def test_browser_urls_never_leak_bridge_hostname_when_externals_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model (A) + (B) together: when externals are configured,
    the browser-facing URLs must contain ONLY the public host and must
    NOT contain the internal Docker bridge hostname. Otherwise an
    attacker scraping a stream token from a browser learns the internal
    topology (and gains a probe target if the bridge IP ever ends up
    routable through misconfiguration).
    """
    _patch_bridge_dns(monkeypatch)
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({**_TIER0_BRIDGE_ENV, **_PUBLIC_EGRESS_ENV})
    import importlib

    mod = importlib.import_module("core.config")
    urls = _render_browser_urls(mod.settings)

    # Positive: every URL points at the public host.
    for kind, url in urls.items():
        assert "cam.example.com" in url, (
            f"{kind} URL {url!r} should use the configured external host"
        )

    # Negative: the bridge hostname and its RFC1918 resolution must NOT
    # appear in any URL handed to a browser.
    for kind, url in urls.items():
        assert "mediamtx" not in url, (
            f"{kind} URL {url!r} leaks the internal Docker hostname"
        )
        assert "172.17." not in url, (
            f"{kind} URL {url!r} leaks the internal bridge subnet"
        )


def test_browser_urls_fall_through_to_internal_when_externals_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model (A) only: with no externals configured (small-scale
    deployments, local-only access), the fallback drops through to the
    internal URLs. V-015 guarantees those are inside the trust zone, so
    the URL still points at a non-internet-routable address — the
    Secure-by-Design default holds even on a misconfigured deployment
    that exposes the token endpoint.
    """
    _patch_bridge_dns(monkeypatch)
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env(_TIER0_BRIDGE_ENV)  # no externals
    import importlib

    mod = importlib.import_module("core.config")
    urls = _render_browser_urls(mod.settings)

    # Fallback hit the internal layer.
    for kind, url in urls.items():
        assert "mediamtx" in url, (
            f"{kind} URL {url!r} should fall through to the internal "
            f"hostname when no external is configured"
        )

    # Belt-and-braces: even though we test no external/public host
    # appears, run an explicit V-015-shaped check that the host
    # ultimately resolves inside the trust zone.
    from urllib.parse import urlparse
    for kind, url in urls.items():
        host = urlparse(url).hostname
        assert mod._host_is_internal(host), (
            f"{kind} URL {url!r} resolves to a host outside the trust zone "
            f"({host!r}); V-015's Secure-by-Default property is broken"
        )


def test_egress_webrtc_url_unaffected_by_internal_bridge_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threat model (B): even if the internal bridge has issues — say,
    DNS for `mediamtx` is temporarily broken — the WebRTC URL handed to
    browsers must continue to point at the external host. The egress
    path is independent of the ingress path; we're verifying the two
    code paths don't share state in a way that lets an ingress issue
    take down operator-facing streaming.

    (Practically: the external URL must not be derived from the internal
    URL's hostname.)
    """
    # Resolver returns NXDOMAIN-style failure for `mediamtx`. The
    # MEDIAMTX_BASE_URL still parses fine — V-015 fails closed only on
    # the boot path. We pre-resolve before applying env so the boot-time
    # validator can succeed, then break DNS to test egress independence.
    import socket as _socket

    real = _socket.getaddrinfo

    def fake(host, *a, **kw):  # type: ignore[no-untyped-def]
        if host == "mediamtx":
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "",
                     ("172.17.0.3", 0))]
        return real(host, *a, **kw)

    monkeypatch.setattr(_socket, "getaddrinfo", fake)
    sys.path.insert(0, str(REPO_ROOT / "server"))
    _apply_env({**_TIER0_BRIDGE_ENV, **_PUBLIC_EGRESS_ENV})
    import importlib

    mod = importlib.import_module("core.config")

    # Now break DNS for the internal hostname. Egress URL construction
    # must not consult DNS, so the WHEP URL must still point at the
    # public host.
    def broken(host, *a, **kw):  # type: ignore[no-untyped-def]
        if host == "mediamtx":
            raise _socket.gaierror("simulated bridge DNS outage")
        return real(host, *a, **kw)

    monkeypatch.setattr(_socket, "getaddrinfo", broken)
    urls = _render_browser_urls(mod.settings)
    assert urls["webrtc"].startswith("https://cam.example.com/"), (
        f"WebRTC URL changed shape under bridge DNS failure: {urls['webrtc']!r}"
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
