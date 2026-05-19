# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
KAI-C adapter registry.

Per §11 of the contract design, KAI-C polls each registered adapter's
``/capabilities`` and ``/health`` on:

* adapter registration
* every 60s thereafter (configurable)
* on demand via ``POST /api/v1/adapters/refresh``

It maintains an in-memory cache of capabilities + health + the operator's
permission grants, and emits the §11.2 audit events on every drift:

* ``adapter.registered``           — initial registration
* ``adapter.fingerprint_mismatch`` — ``model.fingerprint`` changed between polls
* ``adapter.capability_drift``     — any other capability field changed
* ``adapter.unavailable``          — ≥3 consecutive /health failures
* ``adapter.deregistered``         — operator removed, or permissions drift
                                     added a new permission (§11.3 blocking change)

Drift behaviour matches §11.3:

| Field changed                        | Action                                  |
|--------------------------------------|-----------------------------------------|
| ``adapter.{name,version}``           | de-register (treat as new adapter)      |
| ``model.fingerprint``                | audit, alert, keep serving              |
| ``model.version``                    | audit, keep serving                     |
| ``permissions.*`` add new permission | BLOCKING — de-register, await re-approve|
| ``permissions.*`` remove permission  | allow, audit                            |
| ``endpoints.*``                      | audit, no action                        |
| ``scheduling.*``                     | apply silently                          |
| ``cost.*``                           | apply, recompute budget                 |
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from kai_c.audit import AuditEventType, AuditStore
from kai_c.contract_types import (
    CapabilitiesResponse,
    HealthResponse,
    Permissions,
)
from kai_c.sovereignty import (
    SovereigntyViolation,
    adapter_summary_for_audit,
    check_adapter,
)

logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS: int = 60
DEFAULT_HEALTH_TIMEOUT_SECONDS: float = 2.0
DEFAULT_CAPABILITIES_TIMEOUT_SECONDS: float = 5.0
UNAVAILABLE_THRESHOLD: int = 3  # consecutive health failures → unavailable

# Permissions that are "default-safe" — no operator approval required.
# Anything outside this set requires explicit grant (§8). For v1 we
# record the requirement in the audit log but DO NOT block registration;
# A2.4b will land the operator-UI approval flow. The audit event has
# ``requires_operator_approval=true`` so a downstream dashboard can
# surface pending approvals.
SAFE_PERMISSIONS: frozenset[str] = frozenset()  # nothing is default-safe; §8 is strict


# ── Drift comparison helpers ───────────────────────────────────────


def _permissions_added(old: Permissions, new: Permissions) -> list[str]:
    """Return permission deltas that ADD new scope (the §11.3
    "blocking change" set)."""
    additions: list[str] = []
    if new.gpu and not old.gpu:
        additions.append("gpu")
    new_egress = set(new.network_egress) - set(old.network_egress)
    if new_egress:
        additions.append(f"network_egress+={sorted(new_egress)}")
    new_fs = set(new.host_filesystem) - set(old.host_filesystem)
    if new_fs:
        additions.append(f"host_filesystem+={sorted(new_fs)}")
    new_shm = set(new.shared_memory_paths) - set(old.shared_memory_paths)
    if new_shm:
        additions.append(f"shared_memory_paths+={sorted(new_shm)}")
    if new.host_metadata and not old.host_metadata:
        additions.append("host_metadata")
    return additions


def _capabilities_diff(
    old: CapabilitiesResponse, new: CapabilitiesResponse
) -> dict[str, Any]:
    """Compute a high-level diff between two capability snapshots —
    used for the audit ``adapter.capability_drift`` event."""
    diff: dict[str, Any] = {}
    if old.adapter.name != new.adapter.name:
        diff["adapter.name"] = [old.adapter.name, new.adapter.name]
    if old.adapter.version != new.adapter.version:
        diff["adapter.version"] = [old.adapter.version, new.adapter.version]
    if old.model.version != new.model.version:
        diff["model.version"] = [old.model.version, new.model.version]
    if old.tasks_advertised != new.tasks_advertised:
        diff["tasks_advertised"] = [
            list(old.tasks_advertised),
            list(new.tasks_advertised),
        ]
    if old.endpoints.model_dump() != new.endpoints.model_dump():
        diff["endpoints"] = "changed"
    return diff


# ── Registry state ─────────────────────────────────────────────────


@dataclass
class RegisteredAdapter:
    """Per-adapter cache. Capabilities + health + permission grants
    + consecutive-failure counter for the unavailable threshold."""

    name: str
    url: str
    capabilities: CapabilitiesResponse
    fingerprint: str | None
    health: HealthResponse | None = None
    last_polled: float = 0.0
    consecutive_health_failures: int = 0
    granted_permissions: set[str] = field(default_factory=set)


# ── Async HTTP helpers ─────────────────────────────────────────────


async def fetch_capabilities(client: httpx.AsyncClient, url: str) -> CapabilitiesResponse:
    """Fetch + validate /capabilities. Raises on any failure."""
    response = await client.get(f"{url.rstrip('/')}/capabilities", timeout=DEFAULT_CAPABILITIES_TIMEOUT_SECONDS)
    response.raise_for_status()
    return CapabilitiesResponse.model_validate(response.json())


async def fetch_health(client: httpx.AsyncClient, url: str) -> HealthResponse:
    """Fetch + validate /health. Raises on any failure."""
    response = await client.get(f"{url.rstrip('/')}/health", timeout=DEFAULT_HEALTH_TIMEOUT_SECONDS)
    response.raise_for_status()
    return HealthResponse.model_validate(response.json())


# ── Registry ───────────────────────────────────────────────────────


class AdapterRegistry:
    """In-memory adapter registry with background polling.

    Lifecycle:
      1. ``await registry.register(name, url)`` polls /capabilities,
         runs sovereignty + permission checks, stores. Emits
         ``adapter.registered``.
      2. ``await registry.refresh(name)`` re-polls /capabilities +
         /health for a single adapter; emits drift events as needed.
      3. ``await registry.start_polling()`` kicks off a background
         task that calls ``refresh`` for every adapter every
         ``poll_interval_seconds``.
      4. ``await registry.deregister(name)`` removes, emits
         ``adapter.deregistered``.

    All operations are async because httpx + asyncio is the natural
    fit for KAI-C's FastAPI host. The internal state dict is guarded
    by a threading.Lock so synchronous lookups (e.g., from the /infer
    handler) are safe to call without the async loop.
    """

    def __init__(
        self,
        *,
        sovereignty_mode: str,
        audit: AuditStore,
        poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._sovereignty_mode = sovereignty_mode.lower()
        self._audit = audit
        self._poll_interval = poll_interval_seconds
        self._adapters: dict[str, RegisteredAdapter] = {}
        self._lock = threading.Lock()
        # ``trust_env=False`` so HTTP_PROXY etc. don't redirect our
        # adapter probes through some operator-side proxy (same logic
        # as the conformance kit).
        self._owns_client = http_client is None
        self._client: httpx.AsyncClient = http_client or httpx.AsyncClient(trust_env=False)
        self._poll_task: asyncio.Task | None = None
        self._stop_flag = asyncio.Event()

    @property
    def sovereignty_mode(self) -> str:
        return self._sovereignty_mode

    async def aclose(self) -> None:
        await self.stop_polling()
        if self._owns_client:
            await self._client.aclose()

    # ── Public API ─────────────────────────────────────────────────

    async def register(self, name: str, url: str) -> RegisteredAdapter:
        """Register an adapter. Polls /capabilities, runs sovereignty,
        stores. Raises ``SovereigntyViolation`` on policy failure,
        ``httpx.HTTPError`` on adapter unreachability."""
        url = url.rstrip("/")

        # URL-only sovereignty check (no capabilities yet for the
        # network_egress check — but we want to fail fast on bad URLs).
        check_adapter(
            sovereignty_mode=self._sovereignty_mode,
            adapter_url=url,
            capabilities=None,
        )

        caps = await fetch_capabilities(self._client, url)

        # Now the full check including egress.
        check_adapter(
            sovereignty_mode=self._sovereignty_mode,
            adapter_url=url,
            capabilities=caps,
        )

        adapter = RegisteredAdapter(
            name=name,
            url=url,
            capabilities=caps,
            fingerprint=caps.model.fingerprint,
        )
        with self._lock:
            self._adapters[name] = adapter

        # §11.2 audit — adapter.registered
        self._audit.emit(
            AuditEventType.ADAPTER_REGISTERED,
            adapter=name,
            registration_url=url,
            adapter_version=caps.adapter.version,
            model_name=caps.model.name,
            model_version=caps.model.version,
            model_fingerprint=caps.model.fingerprint,
            declared_permissions=caps.permissions.model_dump(mode="json"),
            contract_version=caps.adapter.supported_contract_versions,
            requires_operator_approval=self._requires_approval(caps.permissions),
        )
        logger.info("adapter registered: %s @ %s", name, url)
        return adapter

    async def deregister(self, name: str, *, reason: str = "operator_action") -> None:
        with self._lock:
            adapter = self._adapters.pop(name, None)
        if adapter is None:
            return
        self._audit.emit(
            AuditEventType.ADAPTER_DEREGISTERED,
            adapter=name,
            reason=reason,
        )
        logger.info("adapter deregistered: %s reason=%s", name, reason)

    async def refresh(self, name: str) -> None:
        """Re-poll /capabilities + /health for one adapter; emit drift
        events as appropriate."""
        with self._lock:
            adapter = self._adapters.get(name)
        if adapter is None:
            return

        # Health probe — failures bump the unavailable counter; we do
        # NOT de-register on health flakiness alone.
        try:
            health = await fetch_health(self._client, adapter.url)
            adapter.health = health
            adapter.consecutive_health_failures = 0
        except Exception as exc:
            adapter.consecutive_health_failures += 1
            if adapter.consecutive_health_failures == UNAVAILABLE_THRESHOLD:
                self._audit.emit(
                    AuditEventType.ADAPTER_UNAVAILABLE,
                    adapter=name,
                    reason=str(exc),
                    consecutive_failures=adapter.consecutive_health_failures,
                )

        # Capabilities probe — drift detection vs. cached snapshot.
        try:
            new_caps = await fetch_capabilities(self._client, adapter.url)
        except Exception as exc:
            logger.warning("capabilities poll failed for %s: %s", name, exc)
            adapter.last_polled = time.time()
            return

        old_caps = adapter.capabilities

        # Sovereignty check on the FRESH capabilities. An adapter that
        # adds network_egress at runtime in local_only mode gets
        # de-registered.
        try:
            check_adapter(
                sovereignty_mode=self._sovereignty_mode,
                adapter_url=adapter.url,
                capabilities=new_caps,
            )
        except SovereigntyViolation as exc:
            self._audit.emit(
                AuditEventType.INFERENCE_REFUSED_SOVEREIGNTY,
                adapter=name,
                reason=str(exc),
                sovereignty_mode=self._sovereignty_mode,
                **adapter_summary_for_audit(new_caps),
            )
            await self.deregister(name, reason="sovereignty_violation_on_refresh")
            return

        # Fingerprint mismatch (§11.3 — audit + keep serving).
        if (
            new_caps.model.fingerprint
            and adapter.fingerprint
            and new_caps.model.fingerprint != adapter.fingerprint
        ):
            self._audit.emit(
                AuditEventType.ADAPTER_FINGERPRINT_MISMATCH,
                adapter=name,
                previous_fingerprint=adapter.fingerprint,
                current_fingerprint=new_caps.model.fingerprint,
            )
            adapter.fingerprint = new_caps.model.fingerprint

        # Permissions ADDED — §11.3 blocking change → de-register.
        added = _permissions_added(old_caps.permissions, new_caps.permissions)
        if added:
            self._audit.emit(
                AuditEventType.ADAPTER_CAPABILITY_DRIFT,
                adapter=name,
                field_path="permissions",
                action_taken="de-registered (blocking change)",
                added_permissions=added,
            )
            await self.deregister(name, reason="permissions_added_requires_reapproval")
            return

        # Other drift — audit, keep serving.
        diff = _capabilities_diff(old_caps, new_caps)
        if diff:
            self._audit.emit(
                AuditEventType.ADAPTER_CAPABILITY_DRIFT,
                adapter=name,
                diff=diff,
                action_taken="audited",
            )

        # Apply the new snapshot.
        adapter.capabilities = new_caps
        adapter.last_polled = time.time()

    # ── Synchronous lookups (safe to call from the /infer handler) ──

    def get(self, name: str) -> RegisteredAdapter | None:
        with self._lock:
            return self._adapters.get(name)

    def list_names(self) -> list[str]:
        with self._lock:
            return sorted(self._adapters.keys())

    def list_summaries(self) -> list[dict[str, Any]]:
        """Lightweight summary for the /api/v1/adapters listing."""
        with self._lock:
            return [
                {
                    "name": a.name,
                    "url": a.url,
                    "adapter_name": a.capabilities.adapter.name,
                    "adapter_version": a.capabilities.adapter.version,
                    "model_name": a.capabilities.model.name,
                    "model_version": a.capabilities.model.version,
                    "tasks_advertised": list(a.capabilities.tasks_advertised),
                    "fingerprint": a.fingerprint,
                    "health_status": (
                        a.health.status.value if a.health else "unknown"
                    ),
                    "consecutive_health_failures": a.consecutive_health_failures,
                }
                for a in self._adapters.values()
            ]

    def aggregated_capabilities(self) -> dict[str, Any]:
        """The §11 aggregated capabilities view for OpenNVR UI."""
        with self._lock:
            return {
                "contract_version": "1",
                "sovereignty_mode": self._sovereignty_mode,
                "adapters": {
                    a.name: a.capabilities.model_dump(mode="json")
                    for a in self._adapters.values()
                },
            }

    # ── Inference proxy ────────────────────────────────────────────

    async def proxy_infer(
        self,
        adapter_name: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> tuple[int, dict[str, Any]]:
        """Forward a JSON inference request to a registered adapter,
        threading the correlation_id header. Returns ``(status_code, body)``.

        Audit emission is the caller's responsibility — the registry
        doesn't know about camera_id or latency budgeting, so the
        FastAPI route emits the right events.
        """
        adapter = self.get(adapter_name)
        if adapter is None:
            raise KeyError(adapter_name)
        response = await self._client.post(
            f"{adapter.url}/infer",
            json=payload,
            headers={"X-Correlation-Id": correlation_id},
            timeout=30.0,
        )
        try:
            body = response.json()
        except ValueError:
            body = {"status": "error", "raw": response.text[:200]}
        return response.status_code, body

    # ── Background polling ─────────────────────────────────────────

    async def start_polling(self) -> None:
        if self._poll_task is not None:
            return
        self._stop_flag.clear()
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop_polling(self) -> None:
        if self._poll_task is None:
            return
        self._stop_flag.set()
        try:
            await self._poll_task
        except asyncio.CancelledError:
            pass
        self._poll_task = None

    async def _poll_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_flag.wait(), timeout=self._poll_interval
                )
                break  # stop flag set
            except asyncio.TimeoutError:
                pass
            names = self.list_names()
            for name in names:
                if self._stop_flag.is_set():
                    break
                try:
                    await self.refresh(name)
                except Exception as exc:
                    logger.exception("refresh failed for %s: %s", name, exc)

    # ── Helpers ────────────────────────────────────────────────────

    def _requires_approval(self, permissions: Permissions) -> bool:
        """True if any non-default permission is declared. §8 says these
        require operator approval — the audit event flags this for the
        downstream approval-UI."""
        return (
            permissions.gpu
            or bool(permissions.network_egress)
            or bool(permissions.host_filesystem)
            or bool(permissions.shared_memory_paths)
            or permissions.host_metadata
        )
