# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Per-camera RTSPS reachability probe.

Detects whether a camera supports RTSPS (TLS RTSP) via an async TLS handshake
and reports SUPPORTED / NOT_SUPPORTED / INCONCLUSIVE, so the operator can pick a
per-camera transport-security policy. The policy decision itself lives in
camera_service. See V-003 and DESIGN_NOTES: transport-security probe.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import socket
import ssl
from urllib.parse import urlparse

from core.logging_config import main_logger

logger = main_logger if hasattr(main_logger, "info") else logging.getLogger(__name__)


class ProbeOutcome(str, enum.Enum):
    """Outcome of an RTSPS reachability probe.

    Inherits from ``str`` so SQLAlchemy ``String`` columns can take the
    value directly without conversion; the schema enum still validates.
    """

    SUPPORTED = "supported"
    NOT_SUPPORTED = "not_supported"
    INCONCLUSIVE = "inconclusive"
    NOT_PROBED = "not_probed"


# Standard RTSPS port per RFC 2326 §3.1 and IANA registration.
_DEFAULT_RTSPS_PORT = 322
# Common plaintext RTSP port (RFC 7826) — used as a heuristic in port
# selection.
_PLAIN_RTSP_PORT = 554
# Default per-probe timeout. Cameras on a busy LAN routinely take 1-2s
# to complete a TLS handshake; 5s gives enough room without blocking
# the camera-create UX for long.
_DEFAULT_TIMEOUT_SECONDS = 5.0


def _resolve_probe_target(
    rtsp_url: str, override_port: int | None
) -> tuple[str, int] | None:
    """Decide ``(host, rtsps_port)`` for the probe, returning ``None`` if
    the URL is unparseable.

    Pure function — no I/O — so it's trivially testable.
    """
    if not rtsp_url:
        return None
    try:
        parsed = urlparse(rtsp_url)
    except (ValueError, TypeError):
        return None
    if not parsed.hostname:
        return None

    host = parsed.hostname
    if override_port is not None:
        return (host, override_port)

    explicit_port = parsed.port
    if explicit_port is not None and explicit_port != _PLAIN_RTSP_PORT:
        # Camera is already on a non-default RTSP port — most likely
        # the operator wants the probe to use that same port (e.g.,
        # cameras that multiplex RTSP and RTSPS on a single high port).
        return (host, explicit_port)

    return (host, _DEFAULT_RTSPS_PORT)


def _build_permissive_tls_context() -> ssl.SSLContext:
    """Permissive TLS context for probing only (camera certs are self-signed).
    Checks a TLS server is listening, not its identity. Never reuse this for a
    link that exchanges credentials. See V-018.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class TransportProbeService:
    """Stateless namespace for RTSPS probing. No instance state."""

    @staticmethod
    async def probe(
        rtsp_url: str,
        *,
        rtsps_port: int | None = None,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> ProbeOutcome:
        """Probe a single camera for RTSPS support.

        Args:
            rtsp_url: The camera's existing ``rtsp://`` or ``rtsps://``
                URL (only the host is used; credentials and path are
                ignored).
            rtsps_port: Optional override for the RTSPS port. See module
                docstring for port-selection rules when omitted.
            timeout: Maximum seconds to wait for TCP connect + TLS
                handshake combined.

        Returns:
            A :class:`ProbeOutcome` value. Callers must NOT use the
            outcome to mutate the operator's ``transport_security``
            field directly — let :mod:`services.camera_service` apply
            the policy translation (the probe is informational).
        """
        target = _resolve_probe_target(rtsp_url, rtsps_port)
        if target is None:
            logger.info(
                "transport_probe: unparseable RTSP URL, skipping (url=%r)",
                rtsp_url,
            )
            return ProbeOutcome.INCONCLUSIVE
        host, port = target
        try:
            outcome = await asyncio.wait_for(
                _attempt_tls_handshake(host, port),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.info(
                "transport_probe: timeout after %.1fs (host=%s port=%d)",
                timeout,
                host,
                port,
            )
            return ProbeOutcome.INCONCLUSIVE
        except (socket.gaierror, OSError) as exc:
            logger.info(
                "transport_probe: resolver/socket error (host=%s port=%d): %s",
                host,
                port,
                exc,
            )
            return ProbeOutcome.INCONCLUSIVE
        return outcome


async def _attempt_tls_handshake(host: str, port: int) -> ProbeOutcome:
    """Open a TCP socket, wrap with TLS, close cleanly. Internal use.

    Returns SUPPORTED on a clean handshake, NOT_SUPPORTED on TLS-layer
    rejection (TCP connected but no TLS server / handshake failed).
    Lets :func:`TransportProbeService.probe` translate timeouts and
    DNS failures into INCONCLUSIVE.
    """
    ctx = _build_permissive_tls_context()
    try:
        reader, writer = await asyncio.open_connection(
            host=host, port=port, ssl=ctx
        )
    except ConnectionRefusedError:
        # TCP got an explicit RST — no TLS listener here. Definitively
        # not supported on this port (could be on another port the
        # operator can specify via override).
        return ProbeOutcome.NOT_SUPPORTED
    except ssl.SSLError:
        # TCP connected but TLS layer rejected the handshake (cipher
        # mismatch / not actually TLS / etc). Treat as not-supported
        # because a real RTSPS endpoint would have completed the
        # handshake with our permissive context.
        return ProbeOutcome.NOT_SUPPORTED
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return ProbeOutcome.NOT_SUPPORTED

    # Connection succeeded — close cleanly so we don't leak. We never
    # send RTSP frames; TLS handshake completion is the signal we need.
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        # A best-effort close; the probe outcome is already determined.
        pass
    return ProbeOutcome.SUPPORTED


class TransportPolicyViolation(ValueError):
    """Raised by :func:`enforce_transport_policy` when a stream-start
    attempt would violate the camera's ``transport_security`` policy.

    Carries the policy, the offending URL scheme, and a remediation
    hint so callers can surface a 4xx to the operator without having
    to reconstruct context.
    """

    def __init__(self, policy: str, scheme: str, *, camera_id: int | None = None):
        self.policy = policy
        self.scheme = scheme
        self.camera_id = camera_id
        super().__init__(
            f"Refusing stream start: policy={policy} but camera URL is "
            f"{scheme}://. Either update the camera's RTSP URL to rtsps://, "
            f"or set transport_security to rtsps_preferred / "
            f"plaintext_allowed via PUT /api/v1/cameras/"
            f"{camera_id if camera_id is not None else '{id}'}/transport-security."
        )


def url_is_tls(url: str | None) -> bool:
    """True iff the URL uses an explicitly-TLS scheme (rtsps:// today).

    Conservative: we only treat ``rtsps://`` as TLS-confirmed at the
    URL level. ``rtsp://`` on a non-default port that *happens* to do
    TLS via STARTTLS-style upgrade is rare and not assumed here — the
    operator can express that via the probe + policy combination.
    """
    if not url:
        return False
    try:
        scheme = urlparse(url).scheme.lower()
    except (ValueError, TypeError):
        return False
    return scheme == "rtsps"


def enforce_transport_policy(
    policy: str | None,
    rtsp_url: str | None,
    *,
    camera_id: int | None = None,
) -> None:
    """Refuse (raise) if the camera's transport_security policy is incompatible
    with the URL about to be handed to MediaMTX. Unknown policy values fail
    closed. See V-003 and DESIGN_NOTES: transport-security probe.
    """
    if not policy or policy == "plaintext_allowed":
        return

    # Fail closed on unknown policy values: a hand-edited DB row or a typo
    # (e.g. "rtsp_required") must not silently default to allow.
    _KNOWN_POLICIES = ("rtsps_required", "rtsps_preferred")
    if policy not in _KNOWN_POLICIES:
        try:
            scheme = urlparse(rtsp_url).scheme.lower() if rtsp_url else "?"
        except (ValueError, TypeError):
            scheme = "?"
        logger.error(
            "transport_policy: refusing unknown policy value %r "
            "(camera_id=%s scheme=%s). Known values: %s + plaintext_allowed.",
            policy,
            camera_id,
            scheme,
            _KNOWN_POLICIES,
        )
        raise TransportPolicyViolation(
            policy, scheme, camera_id=camera_id
        )

    if not rtsp_url:
        # Nothing to enforce against. Treat as out-of-scope (caller
        # presumably has a separate validation path for empty URLs).
        return

    try:
        scheme = urlparse(rtsp_url).scheme.lower()
    except (ValueError, TypeError):
        scheme = ""

    if policy == "rtsps_required" and scheme != "rtsps":
        raise TransportPolicyViolation(policy, scheme or "?", camera_id=camera_id)

    if policy == "rtsps_preferred" and scheme != "rtsps":
        logger.warning(
            "transport_policy: camera_id=%s URL is %s:// but policy is "
            "rtsps_preferred (the probe said RTSPS works for this "
            "camera); operator can update the URL to upgrade",
            camera_id,
            scheme or "<unknown>",
        )


def policy_for_outcome(
    outcome: ProbeOutcome, operator_override: str | None = None
) -> str:
    """Translate a probe outcome into a transport_security policy value,
    honouring an explicit operator override.

    Decision table:

    +-----------------+--------------------+----------------------+
    | operator_override| outcome            | result               |
    +=================+====================+======================+
    | not None        | (any)              | operator_override    |
    +-----------------+--------------------+----------------------+
    | None            | SUPPORTED          | "rtsps_preferred"    |
    +-----------------+--------------------+----------------------+
    | None            | NOT_SUPPORTED      | "plaintext_allowed"  |
    +-----------------+--------------------+----------------------+
    | None            | INCONCLUSIVE       | "rtsps_preferred"    |
    +-----------------+--------------------+----------------------+
    | None            | NOT_PROBED         | "rtsps_preferred"    |
    +-----------------+--------------------+----------------------+

    Rationale: a verified-no-TLS camera gets the explicit
    ``plaintext_allowed`` marker (so the operator sees it in the UI and
    can choose to replace the camera). Inconclusive probes keep the
    default — we don't make a security regression based on a transient
    DNS error.
    """
    if operator_override is not None:
        return operator_override
    if outcome == ProbeOutcome.NOT_SUPPORTED:
        return "plaintext_allowed"
    return "rtsps_preferred"
