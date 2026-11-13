# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""Selling an app — `entitlement`, `verify_license`, `Entitlement`.

Demonstrates: `pricing`, `price_note`, `entitlement="license_key"`,
`ContractMixin.verify_license`, `Entitlement`,
`ContractMixin.on_entitlement_update`, `UserContext`, `current_user`.

OpenNVR takes no fee. The platform gives you the hook and stays out of
the transaction: the administrator enters a key in the catalog, core
asks YOUR code whether it is valid, and refuses to enable the app until
you say yes. Core stores the key encrypted and re-delivers both the key
and your verdict on the live config poll. What you sell, how you price
it and who you sell to are yours.

See sdk/opennvr-app-sdk/LICENSING.md for why the SDK's Apache-2.0 licence
lets you ship this closed if you want to.
"""
import hashlib
import hmac
from typing import Any

from opennvr_app_sdk import (
    AppManifest, Detector, Entitlement, Param, current_user,
)

MANIFEST = AppManifest(
    id="anpr-pro",
    name="ANPR Pro",
    version="2.0.0",
    category="vehicle",
    summary="Plate recognition with a site-tuned model.",
    params=[Param("max_cameras", int, default=2,
                  description="Cameras the free tier covers.")],
    pricing="paid",
    price_note="$29 / camera / year — 2 cameras free",
    entitlement="license_key",           # ← the gate
)

_SECRET = b"your-signing-secret-never-in-git"


class AnprPro(Detector):
    manifest = MANIFEST

    def setup(self) -> None:
        self.plan = "free"
        self.camera_limit = self.cfg.max_cameras

    def on_detections(self, camera_id, detections, event):
        return []

    # ── The licence gate ───────────────────────────────────────────

    def verify_license(self, license_key: str) -> Entitlement:
        """Core calls this (POST /entitlement/verify) when an
        administrator enters a key. The verdict is entirely yours —
        check a signature offline like this, or call home if you
        prefer; nothing in the platform inspects the key.

        Return `Entitlement(valid=False, message=...)` to refuse: the
        message is shown to the administrator, so make it actionable."""
        try:
            payload, signature = license_key.rsplit(".", 1)
            plan, expires, cameras = payload.split(":")
        except ValueError:
            return Entitlement(valid=False, message="Malformed licence key.")

        expected = hmac.new(_SECRET, payload.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expected, signature):
            return Entitlement(valid=False,
                               message="This key is not valid for ANPR Pro.")
        return Entitlement(
            valid=True,
            plan=plan,
            expires_at=expires,
            message=f"{plan.title()} plan, {cameras} cameras.",
            # `limits` is displayed by the catalog beside the plan.
            limits={"cameras": int(cameras)},
        )

    def on_entitlement_update(self, entitlement: dict[str, Any]) -> None:
        """Called when core (re-)delivers a verdict — at boot and on
        every config poll. Apply the plan live; it must be idempotent,
        because the first call usually re-states what boot already
        knew."""
        self.plan = entitlement.get("plan") or "free"
        limits = entitlement.get("limits") or {}
        self.camera_limit = int(limits.get("cameras", self.cfg.max_cameras))

    # ── Who is asking ──────────────────────────────────────────────

    def on_action(self, name: str, params: dict[str, Any]) -> Any:
        """Actions are operator verbs: core's proxy is user-JWT only, so
        `current_user()` is a real person, never a service. Use it to
        scope what an action may touch."""
        user = current_user()                     # -> UserContext | None
        if name == "export":
            if user is None:
                raise ValueError("this action requires a signed-in operator")
            # The operator's own per-camera permissions, enforced here.
            cameras = user.visible(params.get("cameras") or [])
            return {"exported": len(cameras), "by": user.username}
        raise KeyError(name)
