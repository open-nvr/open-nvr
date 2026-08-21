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
* ``adapter.deregistered``         — operator removed the adapter
* ``adapter.permission_granted``   — operator granted permission keys (§8)
* ``adapter.permission_revoked``   — operator revoked permission keys (§8)

Drift behaviour matches §11.3 (with the §8 approval-flow refinement for
permission additions — the adapter stays visible but stops serving):

| Field changed                        | Action                                  |
|--------------------------------------|-----------------------------------------|
| ``adapter.{name,version}``           | de-register (treat as new adapter)      |
| ``model.fingerprint``                | audit, alert, keep serving              |
| ``model.version``                    | audit, keep serving                     |
| ``permissions.*`` add new permission | BLOCKING — back to ``pending``; serving |
|                                      | stops until the operator re-approves    |
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
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from kai_c.audit import AuditEventType, AuditStore
from kai_c.contract_types import (
    CapabilitiesResponse,
    HealthResponse,
    Permissions,
)
from kai_c.metrics import MetricsRollup, parse_adapter_metrics, proxy_metrics
from kai_c.sovereignty import (
    SovereigntyViolation,
    adapter_summary_for_audit,
    check_adapter,
)

logger = logging.getLogger(__name__)


DEFAULT_POLL_INTERVAL_SECONDS: int = 60
DEFAULT_HEALTH_TIMEOUT_SECONDS: float = 2.0
DEFAULT_CAPABILITIES_TIMEOUT_SECONDS: float = 5.0
DEFAULT_METRICS_TIMEOUT_SECONDS: float = 2.0
UNAVAILABLE_THRESHOLD: int = 3  # consecutive health failures → unavailable

# Permissions that are "default-safe" — no operator approval required.
# Anything outside this set requires an explicit operator grant (§8).
# Registration is NOT blocked: the adapter registers into a *pending*
# state (stored + polled + visible) but ``proxy_infer`` and the stream
# proxy fail closed until every declared permission key is granted.
# The ``adapter.registered`` audit event carries
# ``requires_operator_approval=true`` so dashboards can surface the
# pending approval.
SAFE_PERMISSIONS: frozenset[str] = frozenset()  # nothing is default-safe; §8 is strict


# ── Permission-key model (§8 / §11 operator-approval flow) ─────────
#
# A permission "key" is a stable string identifying ONE grantable
# scope, derived from the adapter's declared ``Permissions``. The keys
# are the unit the operator grants/revokes, and the unit we compare
# declared-vs-granted to decide whether an adapter is fully approved.
#
#   gpu                              — the coarse GPU-access flag
#   host_metadata                    — the coarse host-metadata flag
#   network_egress:<host>            — one key per declared egress host
#   host_filesystem:<path>           — one key per declared fs path
#   shared_memory_paths:<path>       — one key per declared shm path
#
# An adapter is "fully approved" iff every declared key is present in
# ``granted_permissions``. An adapter that declares NO permissions has
# an empty declared-key set, which is trivially a subset of the empty
# granted set → auto-approved.


def permission_keys(perms: Permissions) -> list[str]:
    """Return the stable permission-key list for a declared
    ``Permissions`` object. Order is deterministic (gpu, host_metadata,
    then the sorted per-host / per-path keys) so callers can rely on a
    stable ordering in API responses and audit events."""
    keys: list[str] = []
    if perms.gpu:
        keys.append("gpu")
    if perms.host_metadata:
        keys.append("host_metadata")
    for host in sorted(set(perms.network_egress)):
        keys.append(f"network_egress:{host}")
    for path in sorted(set(perms.host_filesystem)):
        keys.append(f"host_filesystem:{path}")
    for path in sorted(set(perms.shared_memory_paths)):
        keys.append(f"shared_memory_paths:{path}")
    return keys


def permission_kind(key: str) -> str:
    """The scope family a permission key belongs to — the part before
    the first ``:`` (or the whole key for the flag permissions)."""
    return key.split(":", 1)[0]


def permission_label(key: str) -> str:
    """Human-facing label for a permission key, for the operator UI."""
    kind = permission_kind(key)
    detail = key.split(":", 1)[1] if ":" in key else ""
    if kind == "gpu":
        return "GPU access"
    if kind == "host_metadata":
        return "Host metadata (hostname, OS, hardware inventory)"
    if kind == "network_egress":
        return f"Network egress to {detail}"
    if kind == "host_filesystem":
        return f"Host filesystem access: {detail}"
    if kind == "shared_memory_paths":
        return f"Shared-memory path: {detail}"
    return key


def permission_sovereignty_conflict(key: str, sovereignty_mode: str) -> bool:
    """True when granting this key would conflict with the active
    sovereignty policy — surfaced in the UI so an operator can't
    silently approve a scope the sovereignty layer will refuse anyway.

    Under ``local_only`` any ``network_egress:*`` key is a conflict
    (the adapter is a cloud-proxy; sovereignty refuses it outright).
    ``federated`` / ``cloud_allowed`` impose no per-key conflict here."""
    if sovereignty_mode == "local_only" and permission_kind(key) == "network_egress":
        return True
    return False


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
    if [c.model_dump() for c in old.capabilities] != [c.model_dump() for c in new.capabilities]:
        # v1.1 descriptors are self-descriptive metadata — benign drift,
        # but it belongs in the audit trail like any other change.
        diff["capabilities"] = "changed"
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

    def declared_keys(self) -> list[str]:
        """The permission keys this adapter declares in /capabilities."""
        return permission_keys(self.capabilities.permissions)

    def pending_keys(self) -> list[str]:
        """Declared keys that have NOT yet been granted by an operator."""
        granted = self.granted_permissions
        return [k for k in self.declared_keys() if k not in granted]

    @property
    def approval_status(self) -> str:
        """``"approved"`` iff every declared key is granted (an adapter
        that declares no permission is trivially approved); otherwise
        ``"pending"``. Derived — never stored — so it can't drift out of
        sync with ``granted_permissions``."""
        return "approved" if not self.pending_keys() else "pending"

    @property
    def is_serving_allowed(self) -> bool:
        """Fail-closed serving gate: only a fully-approved adapter may
        serve inference (§8 / §11)."""
        return self.approval_status == "approved"


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


async def fetch_metrics_text(client: httpx.AsyncClient, url: str) -> str:
    """Fetch the adapter's Prometheus /metrics exposition text.
    Raises on any failure — callers treat metrics as best-effort."""
    response = await client.get(f"{url.rstrip('/')}/metrics", timeout=DEFAULT_METRICS_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.text


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
        auth_token: str | None = None,
    ) -> None:
        self._sovereignty_mode = sovereignty_mode.lower()
        self._audit = audit
        self._poll_interval = poll_interval_seconds
        self._adapters: dict[str, RegisteredAdapter] = {}
        self._lock = threading.Lock()
        # §05 observability — bounded per-adapter rollups fed by the
        # /metrics scrape on the same 60s poll (see refresh()).
        self._metrics = MetricsRollup()
        # ``trust_env=False`` so HTTP_PROXY etc. don't redirect our
        # adapter probes through some operator-side proxy (same logic
        # as the conformance kit).
        #
        # ``auth_token`` is attached as ``Authorization: Bearer`` on every
        # probe. Without it, /capabilities + /hardware/evaluation polls
        # succeed only during the adapter's 5-minute registration grace
        # window and then 401 forever (the SDK enforces the token once the
        # window closes). The /infer path already sends this same token.
        self._owns_client = http_client is None
        if http_client is not None:
            self._client: httpx.AsyncClient = http_client
        else:
            headers = (
                {"Authorization": f"Bearer {auth_token}"} if auth_token else None
            )
            self._client = httpx.AsyncClient(trust_env=False, headers=headers)
        self._poll_task: asyncio.Task | None = None
        self._stop_flag = asyncio.Event()

    @property
    def sovereignty_mode(self) -> str:
        return self._sovereignty_mode

    @property
    def metrics(self) -> MetricsRollup:
        """The §05 per-adapter metrics rollup store."""
        return self._metrics

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
        # §8 / §11 approval gate: do NOT auto-grant. An adapter that
        # declares any permission starts with an EMPTY granted set →
        # approval_status "pending" → it is stored + visible but MUST
        # NOT serve inference until an operator grants. An adapter that
        # declares no permission is trivially approved (∅ ⊆ ∅).
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
            declared_permission_keys=adapter.declared_keys(),
            approval_status=adapter.approval_status,
            contract_version=caps.adapter.supported_contract_versions,
            requires_operator_approval=self._requires_approval(caps.permissions),
        )
        logger.info(
            "adapter registered: %s @ %s (approval_status=%s)",
            name, url, adapter.approval_status,
        )
        return adapter

    async def deregister(self, name: str, *, reason: str = "operator_action") -> None:
        with self._lock:
            adapter = self._adapters.pop(name, None)
        if adapter is None:
            return
        # Drop the metrics series so the rollup store stays bounded by
        # the number of LIVE adapters.
        self._metrics.forget(name)
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

        # Metrics scrape (§05) — same poll, best-effort. A missing or
        # broken /metrics endpoint never disturbs the health/drift
        # story; the rollup simply gets no sample this cycle.
        try:
            metrics_text = await fetch_metrics_text(self._client, adapter.url)
            self._metrics.record_sample(name, parse_adapter_metrics(metrics_text))
        except Exception as exc:
            logger.debug("metrics poll failed for %s: %s", name, exc)

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
            # §05 — the drift timeline in the metrics rollup ("the
            # weights changed under you", per the decision view).
            self._metrics.record_fingerprint_change(name)
            adapter.fingerprint = new_caps.model.fingerprint

        # Permissions ADDED — §11.3 blocking change. Rather than making
        # the adapter VANISH (the old behaviour de-registered it), we
        # KEEP it but move it back to ``pending`` so the operator UI can
        # show "new permission requested, re-approve" (§8 / §11 intent).
        # Concretely: apply the fresh capabilities, then drop any granted
        # key that is no longer declared or was newly added, so
        # approval_status re-derives to "pending" and serving stops until
        # the operator re-grants.
        added = _permissions_added(old_caps.permissions, new_caps.permissions)
        if added:
            with self._lock:
                # Re-derive granted against the NEW declared set: keep
                # only keys that were both already granted AND still in
                # the OLD declared set (i.e. not newly-added scope). Any
                # newly-added key is left ungranted → pending.
                new_declared = set(permission_keys(new_caps.permissions))
                old_declared = set(permission_keys(old_caps.permissions))
                previously_granted = adapter.granted_permissions
                adapter.granted_permissions = {
                    k
                    for k in previously_granted
                    if k in new_declared and k in old_declared
                }
                adapter.capabilities = new_caps
                status = adapter.approval_status
            self._audit.emit(
                AuditEventType.ADAPTER_CAPABILITY_DRIFT,
                adapter=name,
                field_path="permissions",
                action_taken="moved to pending (re-approval required)",
                added_permissions=added,
                approval_status=status,
            )
            adapter.last_polled = time.time()
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

        # Apply the new snapshot. §11.3: permission REMOVALS are allowed
        # (scope narrowed) — but any grant for a key the adapter no
        # longer declares must be dropped, otherwise a later re-add of
        # the same key would silently inherit the old grant instead of
        # going back through operator approval. The system revocation is
        # audited like an operator one ("operator or system revokes").
        with self._lock:
            adapter.capabilities = new_caps
            stale_grants = sorted(
                adapter.granted_permissions - set(permission_keys(new_caps.permissions))
            )
            adapter.granted_permissions.difference_update(stale_grants)
            status_after = adapter.approval_status
        if stale_grants:
            self._emit_grant_event(
                AuditEventType.ADAPTER_PERMISSION_REVOKED,
                adapter=name,
                keys=stale_grants,
                actor="system:permission_no_longer_declared",
                approval_status=status_after,
            )
        adapter.last_polled = time.time()

    # ── Permission grants (§8 / §11 operator-approval flow) ─────────

    def _emit_grant_event(
        self,
        event_type: AuditEventType,
        *,
        adapter: str,
        keys: list[str],
        actor: str,
        approval_status: str,
    ) -> str:
        """Emit a grant/revoke audit event with a fresh
        ``adapter_grant_id`` (uuid) capturing {adapter, keys, actor, ts}.
        Returns the grant_id so the HTTP layer can echo it back and the
        server-side audit log can cross-reference it."""
        grant_id = uuid.uuid4().hex
        self._audit.emit(
            event_type,
            adapter=adapter,
            adapter_grant_id=grant_id,
            keys=keys,
            actor=actor,
            approval_status=approval_status,
        )
        return grant_id

    def grant_permissions(
        self, name: str, keys: list[str], actor: str
    ) -> tuple[RegisteredAdapter, str]:
        """Grant one or more declared permission keys for an adapter.

        Only DECLARED keys can be granted — granting a key the adapter
        never asked for is a no-op for that key (it can never affect
        approval_status, since approval compares against declared keys).
        Recomputes approval_status (derived) and emits
        ``adapter.permission_granted`` with an ``adapter_grant_id``.
        Returns ``(adapter, grant_id)``. Thread-safe. Raises ``KeyError``
        for an unknown adapter."""
        with self._lock:
            adapter = self._adapters.get(name)
            if adapter is None:
                raise KeyError(name)
            declared = set(adapter.declared_keys())
            granted_now = sorted(set(keys) & declared)
            adapter.granted_permissions.update(granted_now)
            status = adapter.approval_status
        grant_id = self._emit_grant_event(
            AuditEventType.ADAPTER_PERMISSION_GRANTED,
            adapter=name,
            keys=granted_now,
            actor=actor,
            approval_status=status,
        )
        logger.info(
            "adapter %s: granted %s by %s (approval_status=%s)",
            name, granted_now, actor, status,
        )
        return adapter, grant_id

    def revoke_permissions(
        self, name: str, keys: list[str], actor: str
    ) -> tuple[RegisteredAdapter, str]:
        """Revoke one or more previously-granted permission keys.

        Revoking any declared key that was granted flips the adapter
        back to ``pending`` (which immediately stops it serving, since
        serving is gated on approval_status). Recomputes approval_status
        and emits ``adapter.permission_revoked`` with an
        ``adapter_grant_id``. Returns ``(adapter, grant_id)``.
        Thread-safe. Raises ``KeyError`` for an unknown adapter."""
        with self._lock:
            adapter = self._adapters.get(name)
            if adapter is None:
                raise KeyError(name)
            revoked_now = sorted(set(keys) & adapter.granted_permissions)
            adapter.granted_permissions.difference_update(revoked_now)
            status = adapter.approval_status
        grant_id = self._emit_grant_event(
            AuditEventType.ADAPTER_PERMISSION_REVOKED,
            adapter=name,
            keys=revoked_now,
            actor=actor,
            approval_status=status,
        )
        logger.info(
            "adapter %s: revoked %s by %s (approval_status=%s)",
            name, revoked_now, actor, status,
        )
        return adapter, grant_id

    def approve_all(self, name: str, actor: str) -> tuple[RegisteredAdapter, str]:
        """Grant every currently-declared permission key in one action
        — the operator-UI "approve" button. Emits
        ``adapter.permission_granted`` with an ``adapter_grant_id``
        capturing the full declared-key set. Returns
        ``(adapter, grant_id)``. Thread-safe. Raises ``KeyError`` for an
        unknown adapter."""
        with self._lock:
            adapter = self._adapters.get(name)
            if adapter is None:
                raise KeyError(name)
            declared = adapter.declared_keys()
            adapter.granted_permissions.update(declared)
            status = adapter.approval_status
        grant_id = self._emit_grant_event(
            AuditEventType.ADAPTER_PERMISSION_GRANTED,
            adapter=name,
            keys=declared,
            actor=actor,
            approval_status=status,
        )
        logger.info(
            "adapter %s: approve-all (%s keys) by %s (approval_status=%s)",
            name, len(declared), actor, status,
        )
        return adapter, grant_id

    def permissions_view(self, name: str) -> dict[str, Any] | None:
        """The §11 permission view for one adapter — declared keys with
        labels/kind/sovereignty-conflict flags, granted keys, pending
        keys, and the derived approval_status. Returns ``None`` for an
        unknown adapter so the HTTP layer can map that to a 404."""
        with self._lock:
            adapter = self._adapters.get(name)
            if adapter is None:
                return None
            granted = sorted(adapter.granted_permissions)
            declared = [
                {
                    "key": key,
                    "label": permission_label(key),
                    "kind": permission_kind(key),
                    "sovereignty_conflict": permission_sovereignty_conflict(
                        key, self._sovereignty_mode
                    ),
                }
                for key in adapter.declared_keys()
            ]
            return {
                "adapter": name,
                "approval_status": adapter.approval_status,
                "declared": declared,
                "granted": granted,
                "pending": adapter.pending_keys(),
            }

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
                    "approval_status": a.approval_status,
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

        # §8 / §11 approval gate — fail closed. A pending / not-fully-
        # approved adapter never reaches the model. We audit the refusal
        # (inference.refused_permission) and raise PermissionError so the
        # HTTP route can map it to a clear 403; the caller does NOT emit
        # its own refusal event for this case (the registry owns it,
        # because only the registry knows the pending-key set).
        if not adapter.is_serving_allowed:
            pending = adapter.pending_keys()
            self._audit.emit(
                AuditEventType.INFERENCE_REFUSED_PERMISSION,
                correlation_id=correlation_id,
                adapter=adapter_name,
                approval_status=adapter.approval_status,
                pending_permissions=pending,
                reason=(
                    "adapter is not fully approved; "
                    f"{len(pending)} permission(s) awaiting operator grant"
                ),
            )
            proxy_metrics.record(adapter_name, "refused", None)
            raise PermissionError(
                f"adapter {adapter_name!r} is {adapter.approval_status}: "
                f"{len(pending)} declared permission(s) await operator "
                f"approval before it may serve inference"
            )

        # Client-observed timing: network hop + adapter queue + inference —
        # what CALLERS experience, complementing the adapter's self-reported
        # histogram (and still recorded when the adapter can't self-report).
        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{adapter.url}/infer",
                json=payload,
                headers={"X-Correlation-Id": correlation_id},
                timeout=30.0,
            )
        except Exception:
            proxy_metrics.record(
                adapter_name, "exception", time.monotonic() - started)
            raise
        latency = time.monotonic() - started
        proxy_metrics.record(
            adapter_name,
            "ok" if response.status_code < 400 else "http_error",
            latency,
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
