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

Two FastAPI dependencies gate outbound work at the call sites that initiate it
(narrow by design, not a global middleware, so nothing over- or under-blocks):

* require_outbound_allowed — 403s cloud-touching routes when deployment_mode is
  "offline". See V-009.
* require_ai_sovereignty_allowed — 403s AI-inference routes to non-local
  adapters when ai_sovereignty is "local_only". See V-022.

audit_boot_posture / current_posture record and surface the active policy
(/system/posture). Blocked attempts are audit-logged.
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
    """403 cloud-touching routes when deployment_mode is "offline". Apply to any
    endpoint that initiates an outbound call; metadata reads don't need it.
    See V-009.
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
    """403 AI-inference routes that would forward frames to a non-local adapter
    when ai_sovereignty is "local_only". Stacks with require_outbound_allowed.
    See V-022.
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
        # Operator's acknowledgement of plaintext MediaMTX outputs, surfaced
        # so the deviation is auditable. See V-019.
        "mediamtx_allow_plaintext_outputs": settings.mediamtx_allow_plaintext_outputs,
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
                f"mediamtx_allow_plaintext_outputs="
                f"{posture['mediamtx_allow_plaintext_outputs']}"
            ),
            extra_data=posture,
        )
    except Exception:
        # Boot must continue even if logging fails — the policy itself
        # is enforced by the validators and the gates above.
        pass
