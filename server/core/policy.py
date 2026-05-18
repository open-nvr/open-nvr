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

"""Runtime policy gates for the offline-first architecture.

This module implements the M1a deployment-mode and AI-sovereignty gates
described in the founding paper (Zenodo DOI 10.5281/zenodo.17261761) and
the security architecture document under ``docs/SECURITY_ARCHITECTURE.md``.

Two gates are exported:

* :func:`require_outbound_allowed` — FastAPI dependency that 403s any route
  initiating a cloud-touching outbound call when
  ``settings.deployment_mode == "offline"``. Use on every endpoint that
  ultimately triggers an HTTP request to a non-loopback host or spawns a
  subprocess that pushes data outside the trust boundary.

* :func:`require_ai_sovereignty_allowed` — FastAPI dependency *and* plain
  helper that gates AI-inference paths. In ``local_only`` mode any path
  that would route a frame to a non-local adapter is refused. In
  ``federated`` mode raw frame data is still refused; only anonymised
  parameter exchange is allowed (the federated runtime is responsible for
  honouring that distinction).

Two additional small helpers, :func:`audit_boot_posture` and
:func:`current_posture`, are provided so callers can record the active
policy at startup and surface it to the operator via ``/system/posture``.

Design notes
------------
* The gates are intentionally *narrow*: they sit at the precise call-sites
  that initiate outbound work, not at a single global middleware. A global
  middleware would either over-block (refusing legitimate metadata reads
  in offline mode) or under-block (missing subprocess-mediated outbound
  like FFmpeg-to-RTMP). Per-callsite gating is verbose but auditable.
* Failures are logged via the audit logger so every blocked attempt is
  visible in compliance reports — that is the actual product of the
  policy, not just the refusal itself.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status

from core.config import settings
from core.logging_config import auth_logger


def _log_block(event: str, request: Request | None, detail: dict[str, Any]) -> None:
    """Audit a denied outbound attempt without leaking sensitive payload.

    We log the request path, the policy that blocked it, and the deciding
    setting value. The body is not logged because it routinely contains
    frame bytes / model parameters / credential tokens.
    """
    try:
        auth_logger.log_action(
            event,
            message=f"Policy gate refused outbound: {detail}",
            extra_data=detail,
            ip_address=request.client.host if request and request.client else None,
            user_agent=request.headers.get("user-agent") if request else None,
        )
    except Exception:
        # The gate must always fail closed even if audit logging itself
        # is broken — the refusal is the load-bearing behaviour.
        pass


def require_outbound_allowed(request: Request) -> None:
    """V-009: FastAPI dependency that blocks cloud-touching routes when
    ``settings.deployment_mode == "offline"``.

    Apply with ``Depends(require_outbound_allowed)`` on any endpoint that
    initiates an outbound call (or spawns a subprocess that does). Reads
    of stored metadata do *not* need this gate, so operators can still
    list and clean up cloud configuration even when offline.
    """
    if settings.deployment_mode == "offline":
        _log_block(
            "policy.outbound_blocked",
            request,
            {
                "path": str(request.url.path),
                "method": request.method,
                "policy": "deployment_mode=offline",
                "reason": "cloud_route_disabled_by_policy",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint reaches a non-local resource and is disabled "
                "while deployment_mode=offline. Set deployment_mode=hybrid "
                "or deployment_mode=cloud at boot to enable cloud features. "
                "See docs/SECURITY_ARCHITECTURE.md §2.4."
            ),
        )


def require_ai_sovereignty_allowed(request: Request) -> None:
    """V-022: FastAPI dependency that blocks AI-inference paths that would
    cross the customer's sovereignty boundary in ``local_only`` mode.

    This is enforced in addition to :func:`require_outbound_allowed` on
    routes that specifically initiate AI inference, so an operator who
    wants cloud recording but local-only AI can express that policy as
    ``deployment_mode=hybrid, ai_sovereignty=local_only``.
    """
    if settings.ai_sovereignty == "local_only":
        _log_block(
            "policy.ai_sovereignty_blocked",
            request,
            {
                "path": str(request.url.path),
                "method": request.method,
                "policy": "ai_sovereignty=local_only",
                "reason": "non_local_ai_route_disabled_by_policy",
            },
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "This endpoint would forward inference to a non-local "
                "adapter, which is disabled while ai_sovereignty=local_only. "
                "Set ai_sovereignty=federated for anonymised cross-org "
                "training only, or ai_sovereignty=cloud_allowed to permit "
                "vendor inference. See docs/SECURITY_ARCHITECTURE.md §2.4."
            ),
        )


def cloud_outbound_allowed() -> bool:
    """Plain-Python check (no Request) for use in service call-sites.

    Returns True iff a cloud-touching outbound call is permitted by the
    current deployment_mode. Use in defence-in-depth at the actual httpx /
    boto3 / subprocess call site, after the router-level dependency has
    already refused most attempts.
    """
    return settings.deployment_mode != "offline"


def ai_sovereignty_allows_remote() -> bool:
    """Plain-Python check (no Request) for use in service call-sites.

    Returns True iff routing inference to a non-local adapter is allowed
    by the current ai_sovereignty setting.
    """
    return settings.ai_sovereignty != "local_only"


def assert_cloud_outbound_allowed(*, reason: str) -> None:
    """Hard refusal helper for service code.

    Use instead of ``cloud_outbound_allowed()`` when the calling site has
    no graceful fallback and wants to bubble a clear error to the caller.
    """
    if not cloud_outbound_allowed():
        raise PermissionError(
            f"Refusing outbound call ({reason}) — deployment_mode=offline. "
            f"Set deployment_mode=hybrid|cloud to enable."
        )


def assert_ai_sovereignty_allows_remote(*, reason: str) -> None:
    """Hard refusal helper for AI-inference service code."""
    if not ai_sovereignty_allows_remote():
        raise PermissionError(
            f"Refusing remote AI route ({reason}) — ai_sovereignty=local_only. "
            f"Set ai_sovereignty=federated|cloud_allowed to enable."
        )


def current_posture() -> dict[str, Any]:
    """Snapshot of the policy-relevant settings, for the /system/posture
    endpoint and for the boot-time audit entry."""
    return {
        "deployment_mode": settings.deployment_mode,
        "ai_sovereignty": settings.ai_sovereignty,
        "allow_remote_mediamtx": settings.allow_remote_mediamtx,
    }


def audit_boot_posture() -> None:
    """Record the active policy at server startup so the compliance trail
    shows which posture was in effect for any given time window."""
    posture = current_posture()
    try:
        auth_logger.log_action(
            "policy.boot_posture",
            message=(
                f"Boot policy: deployment_mode={posture['deployment_mode']} "
                f"ai_sovereignty={posture['ai_sovereignty']} "
                f"allow_remote_mediamtx={posture['allow_remote_mediamtx']}"
            ),
            extra_data=posture,
        )
    except Exception:
        # Boot must continue even if logging fails — the policy itself
        # is enforced by the validators and the gates above.
        pass
