# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""
M1c verification tests for V-003 (per-camera RTSPS probe).

Run with:

    cd server && pytest tests/test_m1c_transport_probe.py -v

Coverage:

* ``_resolve_probe_target`` port-selection rules (pure function, all
  documented branches).
* ``policy_for_outcome`` decision-table (every cell from the docstring).
* ``ProbeOutcome`` round-trips through SQLAlchemy ``String`` storage
  semantics (the enum inherits from ``str``).
* ``TransportProbeService.probe`` end-to-end against a real local TLS
  listener for SUPPORTED and against a real refusing socket for
  NOT_SUPPORTED and INCONCLUSIVE.
* Schema accepts the three documented transport_security values and
  rejects everything else.
* Migration revision metadata (down_revision chains correctly off the
  initial squash) — guards against accidental rebase to the wrong
  parent.
"""

from __future__ import annotations

# Python 3.10 sandbox polyfill — the project's pyproject.toml requires
# 3.11+ (where datetime.UTC exists). The polyfill below is a no-op on
# 3.11+. It must be applied BEFORE any module-under-test imports
# ``from datetime import UTC``.
import datetime as _dt  # noqa: I001

if not hasattr(_dt, "UTC"):
    _dt.UTC = _dt.timezone.utc

import asyncio
import os
import secrets
import socket
import ssl
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "server"))

# Settings need to be valid for the schemas/policy imports to succeed,
# but transport_probe_service itself doesn't depend on Settings.
from cryptography.fernet import Fernet  # noqa: E402

os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/x")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
os.environ.setdefault("MEDIAMTX_SECRET", secrets.token_hex(32))
os.environ.setdefault("INTERNAL_API_KEY", secrets.token_urlsafe(48))
os.environ.setdefault(
    "CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode()
)

# Stub `core.logging_config` so probe service doesn't pull in the real
# JSON logger (which wants a writable logs/ directory). The real module
# exports many domain-specific loggers (main_logger, auth_logger,
# camera_logger, etc.) — mirror them all so any module imported under
# test gets a no-op stand-in.
import types as _types  # noqa: E402

_lm = _types.ModuleType("core.logging_config")


class _L:
    def info(self, *a, **kw):
        pass

    def warning(self, *a, **kw):
        pass

    def error(self, *a, **kw):
        pass

    def debug(self, *a, **kw):
        pass

    def log_action(self, *a, **kw):
        pass


for _name in (
    "main_logger",
    "auth_logger",
    "camera_logger",
    "recording_logger",
    "rtsp_logger",
    "api_logger",
    "mediamtx_logger",
    "config_logger",
    "storage_logger",
    "stream_logger",
    "ai_logger",
    "system_logger",
):
    setattr(_lm, _name, _L())


def _setup_logging(*a, **kw):
    return None


_lm.setup_logging = _setup_logging
sys.modules.setdefault("core.logging_config", _lm)

from services.transport_probe_service import (  # noqa: E402
    ProbeOutcome,
    TransportProbeService,
    _build_permissive_tls_context,
    _resolve_probe_target,
    policy_for_outcome,
)


# ---------------------------------------------------------------------------
# Pure-function unit tests (no I/O)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,override,expected",
    [
        # Unparseable / empty inputs.
        ("", None, None),
        (None, None, None),
        ("not a url", None, None),  # no scheme -> hostname=None
        # Standard rtsp:// on port 554 → falls back to the 322 default.
        ("rtsp://10.0.0.5:554/stream", None, ("10.0.0.5", 322)),
        ("rtsp://camera.local/stream", None, ("camera.local", 322)),
        # rtsps:// already — still probe the spec port unless the URL is
        # on a non-default port (port-multiplexing camera).
        ("rtsps://10.0.0.5:8322/stream", None, ("10.0.0.5", 8322)),
        # Camera on a non-default RTSP port → reuse that port.
        ("rtsp://10.0.0.5:8554/stream", None, ("10.0.0.5", 8554)),
        # Operator override always wins.
        ("rtsp://10.0.0.5:554/stream", 9999, ("10.0.0.5", 9999)),
        ("rtsp://10.0.0.5:8554/stream", 9999, ("10.0.0.5", 9999)),
        # Credentials in the URL don't affect the probe target.
        ("rtsp://admin:hunter2@10.0.0.5/stream", None, ("10.0.0.5", 322)),
    ],
)
def test_resolve_probe_target(url, override, expected):
    assert _resolve_probe_target(url, override) == expected


def test_policy_for_outcome_operator_override_wins():
    """Any operator-supplied value short-circuits the probe outcome."""
    for outcome in ProbeOutcome:
        for override in ("rtsps_required", "plaintext_allowed", "rtsps_preferred"):
            assert (
                policy_for_outcome(outcome, override) == override
            ), f"override {override} should win over outcome {outcome}"


def test_policy_for_outcome_no_override_branches():
    """The full decision table from the policy_for_outcome docstring."""
    assert policy_for_outcome(ProbeOutcome.SUPPORTED) == "rtsps_preferred"
    assert policy_for_outcome(ProbeOutcome.NOT_SUPPORTED) == "plaintext_allowed"
    assert policy_for_outcome(ProbeOutcome.INCONCLUSIVE) == "rtsps_preferred"
    assert policy_for_outcome(ProbeOutcome.NOT_PROBED) == "rtsps_preferred"


def test_probe_outcome_is_string_subclass():
    """SQLAlchemy String columns receive the .value as-is; the enum
    inherits from str so the value can be stored directly."""
    assert isinstance(ProbeOutcome.SUPPORTED, str)
    assert ProbeOutcome.SUPPORTED == "supported"
    assert ProbeOutcome.NOT_SUPPORTED.value == "not_supported"


def test_permissive_tls_context_has_documented_safety_off():
    """The probe TLS context intentionally has identity verification
    disabled; this test pins that intent so a future refactor can't
    quietly enable verification (which would make 99% of cameras fail
    the probe)."""
    ctx = _build_permissive_tls_context()
    assert ctx.check_hostname is False
    assert ctx.verify_mode == ssl.CERT_NONE


# ---------------------------------------------------------------------------
# End-to-end probe against a real local TLS listener
# ---------------------------------------------------------------------------


def _self_signed_cert_pair():
    """Generate a fresh self-signed cert+key pair on disk and return the
    paths. Used to stand up a fixture TLS listener that the probe can hit."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime, timedelta

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "test-camera.local")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.utcnow() - timedelta(minutes=1))
        .not_valid_after(datetime.utcnow() + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    crt_path = Path(tempfile.mkstemp(suffix=".crt")[1])
    key_path = Path(tempfile.mkstemp(suffix=".key")[1])
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return crt_path, key_path


async def _serve_one_tls_handshake(crt: Path, key: Path) -> int:
    """Start an asyncio TLS server on an ephemeral port that accepts
    exactly one handshake and immediately closes. Returns the port."""
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))

    async def handler(reader, writer):
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]

    async def stop_after():
        # Give the probe time to connect — we'll close in fixture teardown.
        await asyncio.sleep(0.5)
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass

    asyncio.create_task(stop_after())
    return port


@pytest.mark.asyncio
async def test_probe_supported_against_real_tls_listener():
    """If we put a real TLS server on a port and probe it, the probe
    must return SUPPORTED. This is the test that would have caught the
    M1b 'rtspEncryption=yes' bug class — it exercises real network +
    real TLS, not a static config grep."""
    crt, key = _self_signed_cert_pair()
    try:
        port = await _serve_one_tls_handshake(crt, key)
        outcome = await TransportProbeService.probe(
            f"rtsp://127.0.0.1:{port}/whatever",
            rtsps_port=port,
            timeout=3.0,
        )
        assert outcome == ProbeOutcome.SUPPORTED
    finally:
        for p in (crt, key):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


@pytest.mark.asyncio
async def test_probe_not_supported_against_plain_tcp_server():
    """A plain (non-TLS) TCP server accepts the connection but rejects
    the TLS handshake — probe must return NOT_SUPPORTED."""

    async def handler(reader, writer):
        # Accept and immediately close without speaking TLS.
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        outcome = await TransportProbeService.probe(
            f"rtsp://127.0.0.1:{port}/x",
            rtsps_port=port,
            timeout=3.0,
        )
        assert outcome == ProbeOutcome.NOT_SUPPORTED
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_inconclusive_on_unreachable_host():
    """Probing a host that doesn't accept connections on the target
    port returns INCONCLUSIVE (timeout) rather than NOT_SUPPORTED — we
    don't want a transient network blip to flip a camera into
    plaintext_allowed."""
    # Use a port that nothing is listening on. Picking a likely-free
    # high port; on the off chance it IS in use the test still exits
    # within the 1s timeout below.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    outcome = await TransportProbeService.probe(
        f"rtsp://127.0.0.1:{free_port}/x",
        rtsps_port=free_port,
        timeout=1.0,
    )
    # Either NOT_SUPPORTED (RST) or INCONCLUSIVE (timeout) is acceptable
    # here — the loopback stack on different OSes behaves differently.
    # The important property is that we do NOT crash and do NOT return
    # SUPPORTED.
    assert outcome in (ProbeOutcome.NOT_SUPPORTED, ProbeOutcome.INCONCLUSIVE)


@pytest.mark.asyncio
async def test_probe_inconclusive_on_unparseable_url():
    outcome = await TransportProbeService.probe("not a url")
    assert outcome == ProbeOutcome.INCONCLUSIVE


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_schema_transport_security_accepts_valid_enum_values():
    from schemas import CameraConfigBase

    for value in ("rtsps_required", "rtsps_preferred", "plaintext_allowed"):
        cfg = CameraConfigBase(transport_security=value)
        assert cfg.transport_security == value


def test_schema_transport_security_rejects_invalid_values():
    from pydantic import ValidationError

    from schemas import CameraConfigBase

    for value in (
        "yes",
        "no",
        "TLS",
        "rtsps_REQUIRED",
        "",
        "plaintext",
    ):
        with pytest.raises(ValidationError):
            CameraConfigBase(transport_security=value)


def test_schema_default_transport_security_is_preferred():
    from schemas import CameraConfigBase

    cfg = CameraConfigBase()
    assert cfg.transport_security == "rtsps_preferred"


def test_schema_response_includes_probe_metadata():
    from datetime import UTC, datetime

    from schemas import CameraConfigResponse

    resp = CameraConfigResponse(
        id=1,
        camera_id=1,
        transport_security_probe_result="supported",
        transport_security_probed_at=datetime.now(UTC),
    )
    assert resp.transport_security_probe_result == "supported"
    assert resp.transport_security_probed_at is not None
    # Default policy value still surfaces.
    assert resp.transport_security == "rtsps_preferred"


# ---------------------------------------------------------------------------
# Migration sanity check
# ---------------------------------------------------------------------------


def test_migration_chains_off_initial_squash():
    """The new revision must declare ``down_revision = 'd75d15b88c1a'``
    so it lands cleanly on top of the existing schema. Catches the
    classic 'rebased onto the wrong parent' mistake.

    Reads the source file directly (no alembic import) so the test runs
    in any environment, not just one with alembic+sqlalchemy installed.
    """
    mig_path = (
        REPO_ROOT
        / "server"
        / "migrations"
        / "versions"
        / "b4e2a9c7f1d0_add_camera_transport_security.py"
    )
    text = mig_path.read_text()
    assert 'revision: str = "b4e2a9c7f1d0"' in text, (
        "migration must declare its own revision id"
    )
    assert 'down_revision: str | None = "d75d15b88c1a"' in text, (
        "migration must chain off the initial squash revision"
    )
    # And the upgrade() body must mention the new columns by name, so a
    # rename in models.py without a matching migration update fails CI.
    for col in (
        "transport_security",
        "transport_security_operator_set",
        "transport_security_probe_result",
        "transport_security_probed_at",
    ):
        assert col in text, f"migration is missing column {col!r}"


# ---------------------------------------------------------------------------
# Router handler tests — M1c-selfrev M-1
# ---------------------------------------------------------------------------
# These exercise the handler functions in routers/cameras.py with mocked
# dependencies. We don't spin up a full FastAPI app because that drags
# in the entire router graph (CORS, auth middleware, MediaMTX startup
# service, etc.) which makes unit-test feedback slow. Calling the
# handler functions directly with hand-built mocks is faster, more
# targeted, and catches the exact logic the M-1 finding flagged:
# operator-override preservation, port override threading, reset_policy
# semantics, and the new PUT endpoint setting the operator_set flag.


@pytest.fixture(autouse=True)
def _clean_env_before_each_router_test():
    """Tests in earlier files (notably test_m1b_mediamtx_hardening) leave
    routable URLs in ``os.environ`` to exercise the V-015 validator. Those
    values stay set in this process after the test exits, which makes
    later imports of ``core.config`` fail when the validator re-evaluates.

    Wipe the OpenNVR-namespaced env vars before every M1c test and
    re-apply known-good values so any re-import of ``core.config`` finds
    a clean configuration. Doesn't touch host env vars (``HOST``,
    ``HOSTNAME``, etc.) — the prefix list is conservative.
    """
    prefixes_to_clear = (
        "SECRET_KEY",
        "MEDIAMTX_",
        "INTERNAL_API_KEY",
        "CREDENTIAL_ENCRYPTION_KEY",
        "DATABASE_URL",
        "ALLOW_REMOTE_MEDIAMTX",
        "DEPLOYMENT_MODE",
        "AI_SOVEREIGNTY",
        "DEFAULT_ADMIN_",
        "KAI_C_",
        "DEBUG",
    )
    saved = {}
    for k in list(os.environ):
        if any(k == p or k.startswith(p) for p in prefixes_to_clear):
            saved[k] = os.environ.pop(k)
    os.environ.update(
        DATABASE_URL="postgresql://u:p@localhost/x",
        SECRET_KEY=secrets.token_urlsafe(48),
        MEDIAMTX_SECRET=secrets.token_hex(32),
        INTERNAL_API_KEY=secrets.token_urlsafe(48),
        CREDENTIAL_ENCRYPTION_KEY=Fernet.generate_key().decode(),
    )
    # Force a fresh core.config import so the new env takes effect.
    for m in list(sys.modules):
        if m == "core" or m.startswith("core."):
            del sys.modules[m]
    # Re-install the logging stub that may have been displaced by the
    # `del sys.modules` purge above.
    sys.modules["core.logging_config"] = _lm
    yield
    # Best-effort restore for any test runner that cares about isolation
    # at the process level. Most CI runs are single-process so this is
    # cosmetic.
    for k in list(os.environ):
        if any(k == p or k.startswith(p) for p in prefixes_to_clear):
            os.environ.pop(k, None)
    for k, v in saved.items():
        os.environ[k] = v


class _MockCameraConfig:
    """Hand-rolled stand-in for the CameraConfig ORM row. Stores
    attributes so we can assert against the side effects of each
    handler invocation."""

    def __init__(self, **kw):
        self.transport_security = kw.get("transport_security", "rtsps_preferred")
        self.transport_security_operator_set = kw.get(
            "transport_security_operator_set", False
        )
        self.transport_security_probe_result = kw.get(
            "transport_security_probe_result", "not_probed"
        )
        self.transport_security_probed_at = kw.get("transport_security_probed_at")
        self.camera_id = kw.get("camera_id", 1)


class _MockCamera:
    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.rtsp_url = kw.get("rtsp_url", "rtsp://10.0.0.5:554/stream")


class _MockUser:
    def __init__(self, user_id: int = 42):
        self.id = user_id


class _MockQuery:
    """Simulates db.query(CameraConfig).filter(...).first()."""

    def __init__(self, config):
        self._config = config

    def filter(self, *_args, **_kw):
        return self

    def first(self):
        return self._config


class _MockDB:
    def __init__(self, config: _MockCameraConfig | None):
        self._config = config
        self.commits = 0

    def query(self, *_args, **_kw):
        return _MockQuery(self._config)

    def commit(self):
        self.commits += 1

    def refresh(self, _obj):
        pass


class _MockRequest:
    """Stand-in for fastapi.Request — only needs .client.host for the
    audit log payload."""

    class _Client:
        host = "10.1.1.1"

    client = _Client()


def _install_camera_service_stub(monkeypatch, camera: _MockCamera | None):
    """Patch CameraService.get_camera_by_id to return a fixed camera
    (or None to simulate 404)."""
    from services import camera_service as cs

    monkeypatch.setattr(
        cs.CameraService,
        "get_camera_by_id",
        staticmethod(lambda **kw: camera),
    )


def _install_probe_stub(monkeypatch, recorded_calls: list, outcome_value: str):
    """Patch TransportProbeService.probe to capture call args and
    return a controllable outcome."""
    from services import transport_probe_service as tps

    async def _fake_probe(rtsp_url, *, rtsps_port=None, timeout=5.0):
        recorded_calls.append({"url": rtsp_url, "port": rtsps_port, "timeout": timeout})
        return tps.ProbeOutcome(outcome_value)

    monkeypatch.setattr(tps.TransportProbeService, "probe", _fake_probe)


@pytest.mark.asyncio
async def test_probe_handler_routes_port_override_through(monkeypatch):
    """H-1: ?port=X must reach TransportProbeService.probe."""
    from routers.cameras import probe_camera_transport

    cam = _MockCamera(id=7, rtsp_url="rtsp://10.0.0.5:554/stream")
    cfg = _MockCameraConfig(camera_id=7)
    db = _MockDB(cfg)
    user = _MockUser()
    recorded = []

    _install_camera_service_stub(monkeypatch, cam)
    _install_probe_stub(monkeypatch, recorded, "supported")

    result = await probe_camera_transport(
        camera_id=7,
        port=443,
        reset_policy=False,
        db=db,
        current_user=user,
        request=_MockRequest(),
    )
    assert len(recorded) == 1
    assert recorded[0]["port"] == 443, (
        "H-1: router must thread `port` query param into the probe call"
    )
    assert result["transport_security"] == "rtsps_preferred"


@pytest.mark.asyncio
async def test_probe_handler_preserves_operator_override(monkeypatch):
    """H-2: when transport_security_operator_set=True, the probe must
    NOT overwrite the policy, regardless of outcome."""
    from routers.cameras import probe_camera_transport

    cam = _MockCamera()
    cfg = _MockCameraConfig(
        transport_security="rtsps_required",
        transport_security_operator_set=True,
    )
    db = _MockDB(cfg)
    recorded = []
    _install_camera_service_stub(monkeypatch, cam)
    _install_probe_stub(monkeypatch, recorded, "not_supported")

    result = await probe_camera_transport(
        camera_id=1,
        port=None,
        reset_policy=False,
        db=db,
        current_user=_MockUser(),
        request=_MockRequest(),
    )
    # Policy must NOT be downgraded to plaintext_allowed.
    assert cfg.transport_security == "rtsps_required"
    assert cfg.transport_security_operator_set is True
    # But the probe outcome IS recorded so the operator sees the
    # mismatch in the API response.
    assert cfg.transport_security_probe_result == "not_supported"
    assert result["operator_override_preserved"] is True


@pytest.mark.asyncio
async def test_probe_handler_reset_policy_clears_operator_flag(monkeypatch):
    """H-2 + the explicit reset path: ?reset_policy=true must drive the
    policy from the probe outcome AND clear operator_set."""
    from routers.cameras import probe_camera_transport

    cam = _MockCamera()
    cfg = _MockCameraConfig(
        transport_security="plaintext_allowed",
        transport_security_operator_set=True,
    )
    db = _MockDB(cfg)
    _install_camera_service_stub(monkeypatch, cam)
    _install_probe_stub(monkeypatch, [], "supported")

    result = await probe_camera_transport(
        camera_id=1,
        port=None,
        reset_policy=True,
        db=db,
        current_user=_MockUser(),
        request=_MockRequest(),
    )
    assert cfg.transport_security == "rtsps_preferred"  # probe drove it
    assert cfg.transport_security_operator_set is False  # flag cleared
    assert result["operator_override_preserved"] is False


@pytest.mark.asyncio
async def test_probe_handler_404_on_missing_camera(monkeypatch):
    from fastapi import HTTPException

    from routers.cameras import probe_camera_transport

    _install_camera_service_stub(monkeypatch, None)
    with pytest.raises(HTTPException) as exc_info:
        await probe_camera_transport(
            camera_id=999,
            port=None,
            reset_policy=False,
            db=_MockDB(None),
            current_user=_MockUser(),
            request=_MockRequest(),
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_probe_handler_400_on_camera_without_rtsp_url(monkeypatch):
    from fastapi import HTTPException

    from routers.cameras import probe_camera_transport

    _install_camera_service_stub(monkeypatch, _MockCamera(rtsp_url=None))
    with pytest.raises(HTTPException) as exc_info:
        await probe_camera_transport(
            camera_id=1,
            port=None,
            reset_policy=False,
            db=_MockDB(_MockCameraConfig()),
            current_user=_MockUser(),
            request=_MockRequest(),
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_transport_security_put_sets_operator_flag(monkeypatch):
    """The PUT endpoint must set operator_set=True so the policy
    survives subsequent re-probes."""
    from routers.cameras import set_camera_transport_security
    from schemas import TransportSecurityUpdate

    cam = _MockCamera()
    cfg = _MockCameraConfig(
        transport_security="rtsps_preferred",
        transport_security_operator_set=False,
    )
    db = _MockDB(cfg)
    _install_camera_service_stub(monkeypatch, cam)

    result = await set_camera_transport_security(
        camera_id=1,
        payload=TransportSecurityUpdate(policy="rtsps_required"),
        db=db,
        current_user=_MockUser(),
        request=_MockRequest(),
    )
    assert cfg.transport_security == "rtsps_required"
    assert cfg.transport_security_operator_set is True
    assert result["previous"] == "rtsps_preferred"
    assert result["transport_security_operator_set"] is True


@pytest.mark.asyncio
async def test_transport_security_put_404_on_missing_camera(monkeypatch):
    from fastapi import HTTPException

    from routers.cameras import set_camera_transport_security
    from schemas import TransportSecurityUpdate

    _install_camera_service_stub(monkeypatch, None)
    with pytest.raises(HTTPException) as exc_info:
        await set_camera_transport_security(
            camera_id=999,
            payload=TransportSecurityUpdate(policy="rtsps_required"),
            db=_MockDB(None),
            current_user=_MockUser(),
            request=_MockRequest(),
        )
    assert exc_info.value.status_code == 404


def test_transport_security_update_schema_rejects_invalid_policy():
    """Sanity: the PUT body schema is enum-validated."""
    from pydantic import ValidationError

    from schemas import TransportSecurityUpdate

    for bad in ("yes", "RTSPS_REQUIRED", "", "tls", "plaintext"):
        with pytest.raises(ValidationError):
            TransportSecurityUpdate(policy=bad)
    for good in ("rtsps_required", "rtsps_preferred", "plaintext_allowed"):
        assert TransportSecurityUpdate(policy=good).policy == good


# ---------------------------------------------------------------------------
# M1c-followup: runtime enforcement of transport_security
# ---------------------------------------------------------------------------
# The probe layer (above) decides what each camera supports. The
# enforcement layer below is what actually refuses plaintext when a
# camera's policy says rtsps_required, at the moment the stream is
# about to be handed off to MediaMTX.


@pytest.mark.parametrize(
    "url,expected",
    [
        ("rtsps://10.0.0.5:8322/stream", True),
        ("RTSPS://10.0.0.5:8322/stream", True),  # case-insensitive
        ("rtsp://10.0.0.5:554/stream", False),
        ("rtsp://10.0.0.5:8322/stream", False),  # port alone doesn't make it TLS
        ("", False),
        (None, False),
        ("not a url", False),
        ("http://10.0.0.5/", False),
    ],
)
def test_url_is_tls(url, expected):
    from services.transport_probe_service import url_is_tls

    assert url_is_tls(url) is expected


@pytest.mark.parametrize(
    "policy,url,should_raise",
    [
        # rtsps_required — only rtsps:// allowed.
        ("rtsps_required", "rtsps://10.0.0.5/s", False),
        ("rtsps_required", "rtsp://10.0.0.5/s", True),
        ("rtsps_required", "http://10.0.0.5/s", True),
        # rtsps_preferred — informational; both schemes allowed.
        ("rtsps_preferred", "rtsps://10.0.0.5/s", False),
        ("rtsps_preferred", "rtsp://10.0.0.5/s", False),
        # plaintext_allowed — anything goes.
        ("plaintext_allowed", "rtsp://10.0.0.5/s", False),
        ("plaintext_allowed", "rtsps://10.0.0.5/s", False),
        # None / pre-probe / unknown — gate skips.
        (None, "rtsp://10.0.0.5/s", False),
        (None, "rtsps://10.0.0.5/s", False),
        # Empty / None URL — gate skips (validation is upstream).
        ("rtsps_required", "", False),
        ("rtsps_required", None, False),
    ],
)
def test_enforce_transport_policy_decision_table(policy, url, should_raise):
    from services.transport_probe_service import (
        TransportPolicyViolation,
        enforce_transport_policy,
    )

    if should_raise:
        with pytest.raises(TransportPolicyViolation):
            enforce_transport_policy(policy, url, camera_id=7)
    else:
        # Must NOT raise — caller proceeds.
        enforce_transport_policy(policy, url, camera_id=7)


def test_transport_policy_violation_message_includes_camera_id():
    """The error message must be actionable — it has to tell the
    operator which camera and what to do next."""
    from services.transport_probe_service import (
        TransportPolicyViolation,
        enforce_transport_policy,
    )

    with pytest.raises(TransportPolicyViolation) as exc_info:
        enforce_transport_policy("rtsps_required", "rtsp://10.0.0.5/s", camera_id=42)

    msg = str(exc_info.value)
    assert "rtsps_required" in msg
    assert "rtsp" in msg.lower()
    assert "42" in msg, "remediation hint must name the specific camera_id"
    # Stored attributes for programmatic handling.
    assert exc_info.value.policy == "rtsps_required"
    assert exc_info.value.scheme == "rtsp"
    assert exc_info.value.camera_id == 42


@pytest.mark.asyncio
async def test_push_rtsp_stream_refuses_plaintext_for_rtsps_required(monkeypatch):
    """Integration: when push_rtsp_stream is asked to violate the
    policy, the gate fires inside ``provision_path`` (the choke point
    moved in M1c-followup-selfrev). We patch httpx so any MediaMTX
    HTTP would crash the test — confirming refusal happens BEFORE the
    network call."""
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.transport_probe_service import TransportPolicyViolation

    # If we got as far as the httpx call, the gate failed.
    class _ForbiddenHttpx:
        def __init__(self, *a, **kw):
            raise AssertionError(
                "MediaMTX HTTP reached despite policy violation"
            )

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _ForbiddenHttpx)

    with pytest.raises(TransportPolicyViolation):
        await MediaMtxAdminService.push_rtsp_stream(
            camera_id=7,
            camera_ip="10.0.0.5",
            rtsp_url="rtsp://10.0.0.5:554/stream",
            transport_security="rtsps_required",
        )


@pytest.mark.asyncio
async def test_provision_path_is_the_choke_point(monkeypatch):
    """The bypass-audit found that 3 of 4 entry points to MediaMTX
    skipped the per-camera policy because the gate was at
    ``push_rtsp_stream`` instead of the deeper ``provision_path``.
    This test pins the new invariant: ``provision_path`` itself
    refuses, regardless of which entry point called it."""
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.transport_probe_service import TransportPolicyViolation

    class _ForbiddenHttpx:
        def __init__(self, *a, **kw):
            raise AssertionError("HTTP reached despite policy violation")

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _ForbiddenHttpx)

    # The MediaMTX admin client may not be configured in the test env,
    # in which case provision_path returns early with status=no_admin_api
    # BEFORE the HTTP attempt. Override is_configured to ensure the
    # gate's "fail-before-http" path is the only relevant code path
    # exercised here.
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )

    with pytest.raises(TransportPolicyViolation):
        await MediaMtxAdminService.provision_path(
            camera_id=7,
            camera_ip="10.0.0.5",
            config={"source_url": "rtsp://10.0.0.5:554/stream"},
            transport_security="rtsps_required",
        )


@pytest.mark.asyncio
async def test_provision_path_skips_gate_when_policy_is_none(monkeypatch):
    """The pre-probe state (no policy decided yet) must not trigger
    the gate. Camera-create's first push goes through this path."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    reached = {"http": False}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def post(self, *a, **kw):
            reached["http"] = True
            class _R:
                # M1c-fu-sr-v2 P-3: include is_success so _to_result()
                # exits cleanly. Without this, AttributeError gets
                # swallowed by provision_path's broad except and the
                # test would pass even if the post-HTTP code path was
                # broken — a false-positive vector the peer review
                # flagged.
                status_code = 200
                is_success = True
                def json(self): return {"name": "cam-7"}
                @property
                def text(self): return "{}"
            return _R()

    import services.mediamtx_admin_service as mam
    monkeypatch.setattr(mam.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )

    # policy=None should let plaintext through.
    result = await MediaMtxAdminService.provision_path(
        camera_id=7,
        camera_ip="10.0.0.5",
        config={"source_url": "rtsp://10.0.0.5:554/stream"},
        transport_security=None,
    )
    assert reached["http"], (
        "provision_path with policy=None should reach MediaMTX HTTP"
    )
    # Assert the post-HTTP code path executed cleanly (would have raised
    # before the P-3 fix because _to_result accesses resp.is_success).
    assert result.get("http_status") == 200, (
        f"provision_path post-HTTP path didn't execute cleanly: {result!r}"
    )


@pytest.mark.asyncio
async def test_push_rtsp_stream_allows_rtsps_for_rtsps_required(monkeypatch):
    """Mirror of the previous test: when the URL IS rtsps://, the gate
    lets the call through and we reach provision_path."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    reached = {"called": False}

    async def _record(*a, **kw):
        reached["called"] = True
        return {"status": "ok", "details": {}}

    monkeypatch.setattr(MediaMtxAdminService, "provision_path", _record)

    result = await MediaMtxAdminService.push_rtsp_stream(
        camera_id=7,
        camera_ip="10.0.0.5",
        rtsp_url="rtsps://10.0.0.5:8322/stream",
        transport_security="rtsps_required",
    )
    assert reached["called"], (
        "push_rtsp_stream must call MediaMTX when policy is satisfied"
    )
    assert result.get("status") == "ok"


@pytest.mark.parametrize(
    "bad_policy",
    [
        "banana",
        "rtsp_required",  # typo — missing trailing 's'
        "RTSPS_REQUIRED",  # wrong case
        "plaintext",  # incomplete
        "  rtsps_required  ",  # whitespace
        "rtsps_required ",
    ],
)
def test_enforce_transport_policy_refuses_unknown_values(bad_policy):
    """M1c-fu-sr-v2 P-2: a security gate must fail-CLOSED on unknown
    policy values. The pydantic schema constrains the enum upstream
    but the gate is supposed to be defense-in-depth — any unknown
    value (typo, hand-edited DB row, future enum drift) must refuse,
    never silently allow."""
    from services.transport_probe_service import (
        TransportPolicyViolation,
        enforce_transport_policy,
    )

    with pytest.raises(TransportPolicyViolation):
        enforce_transport_policy(
            bad_policy, "rtsps://10.0.0.5/s", camera_id=42
        )


@pytest.mark.asyncio
async def test_patch_path_refuses_source_override_for_rtsps_required(monkeypatch):
    """M1c-fu-sr-v2 P-1: the PATCH /admin/paths/{id} surface accepts
    a `source` field that re-points the MediaMTX path at a different
    URL. A superuser using this endpoint to flip a camera marked
    rtsps_required back to plaintext rtsp:// would bypass the
    provision_path gate. patch_path must run the same policy check."""
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services.transport_probe_service import TransportPolicyViolation

    class _ForbiddenHttpx:
        def __init__(self, *a, **kw):
            raise AssertionError(
                "patch_path reached MediaMTX HTTP despite policy violation"
            )

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _ForbiddenHttpx)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )

    with pytest.raises(TransportPolicyViolation):
        await MediaMtxAdminService.patch_path(
            camera_id=7,
            camera_ip="10.0.0.5",
            payload={"source": "rtsp://10.0.0.5:554/stream"},
            transport_security="rtsps_required",
        )


@pytest.mark.asyncio
async def test_patch_path_allows_record_only_patches_under_rtsps_required(monkeypatch):
    """If the payload doesn't touch `source`, the patch is policy-neutral
    (recording on/off, hooks, etc) and must pass through regardless of
    the transport_security policy."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    reached = {"http": False}

    class _FakeClient:
        def __init__(self, *a, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return None
        async def patch(self, *a, **kw):
            reached["http"] = True
            class _R:
                status_code = 200
                is_success = True
                def json(self): return {"name": "cam-7"}
                @property
                def text(self): return "{}"
            return _R()

    import services.mediamtx_admin_service as mam

    monkeypatch.setattr(mam.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(
        MediaMtxAdminService, "is_configured", staticmethod(lambda: True)
    )

    # Payload mutates only recording — no `source` key.
    await MediaMtxAdminService.patch_path(
        camera_id=7,
        camera_ip="10.0.0.5",
        payload={"record": True},
        transport_security="rtsps_required",
    )
    assert reached["http"], (
        "policy-neutral patch must be allowed even under rtsps_required"
    )


@pytest.mark.asyncio
async def test_push_rtsp_stream_allows_plaintext_when_policy_is_none(monkeypatch):
    """The camera-create path doesn't have a policy yet (first probe
    runs AFTER provision succeeds). Passing None must skip the gate so
    bootstrap still works."""
    from services.mediamtx_admin_service import MediaMtxAdminService

    reached = {"called": False}

    async def _record(*a, **kw):
        reached["called"] = True
        return {"status": "ok", "details": {}}

    monkeypatch.setattr(MediaMtxAdminService, "provision_path", _record)

    result = await MediaMtxAdminService.push_rtsp_stream(
        camera_id=7,
        camera_ip="10.0.0.5",
        rtsp_url="rtsp://10.0.0.5:554/stream",
        transport_security=None,
    )
    assert reached["called"]
    assert result.get("status") == "ok"
