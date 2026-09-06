# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""What an app may reach outside the stack — the egress policy.

Apps run on the ``opennvr_apps`` compose network, which is *internal*:
no route to the LAN or the internet. The only way out is the
``egress-proxy`` service (``scripts/egress-proxy``), and the proxy
asks core, per connection, whether this app may open this host. Core
answers from two lists:

* the hosts the app's **catalog listing declared** (``network_egress``
  in ``server/config/apps_index.yml`` — reviewed with the app, shown on
  its card before install), and
* the hosts the **operator allowed** for this install
  (``installed_apps.egress_allow`` — the Home Assistant box, a camera's
  snapshot URL, a self-hosted ntfy), set from the catalog after a
  denial surfaced in the inbox.

Everything else is refused, logged, counted here, and — once per app
and destination per hour — raised as an inbox alert that names the
host, so the operator sees exactly what the app tried and can allow
it in one click or leave it blocked.

Identity is not a secret the app carries: the proxy reports the
client's IP, and core matches it against the address each registered
app's contract URL resolves to — the same address core already polls
for ``/health``. An app can't claim to be another app.

Rules are hosts, not URLs: ``api.telegram.org``, ``*.ntfy.sh``,
``192.168.1.50``, ``192.168.1.0/24``, optionally ``:port``. Free-text
entries in a listing's ``network_egress`` ("the webhook URL the
operator configures") are notes for the card and never become rules.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: A host rule: hostname (optionally ``*.`` wildcard), IPv4 address or
#: CIDR; optional ``:port``. Deliberately narrow — a rule that fails
#: this grammar is a note, not a wall.
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?"
_HOST_RULE_RE = re.compile(
    rf"^(?P<host>(?:\*\.)?{_LABEL}(?:\.{_LABEL})*|\d{{1,3}}(?:\.\d{{1,3}}){{3}}(?:/\d{{1,2}})?)"
    r"(?::(?P<port>\d{1,5}))?$"
)
MAX_RULES = 64
MAX_RULE_LEN = 260

#: Denials remembered per app (most recent destinations first).
MAX_DENIALS_PER_APP = 50
#: One inbox alert per (app, destination) per this many seconds.
ALERT_EVERY_S = 3600.0
#: DNS answers for contract hosts are cached this long.
RESOLVE_TTL_S = 60.0


@dataclass(frozen=True)
class HostRule:
    """One parsed rule. ``host`` is lower-case; ``port`` None = any."""

    host: str
    port: int | None
    source: str  # "listing" | "operator"

    def matches(self, host: str, port: int) -> bool:
        if self.port is not None and self.port != port:
            return False
        h = host.lower().rstrip(".")
        if "/" in self.host:
            try:
                return ipaddress.ip_address(h) in ipaddress.ip_network(self.host, strict=False)
            except ValueError:
                return False
        if self.host.startswith("*."):
            suffix = self.host[1:]
            return h.endswith(suffix) and len(h) > len(suffix)
        return h == self.host

    def text(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


def parse_rule(value: Any, *, source: str = "operator") -> HostRule | None:
    """A rule from one entry, or ``None`` when the entry is prose, a
    URL, or otherwise not a host."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or len(text) > MAX_RULE_LEN or "://" in text:
        return None  # prose, a URL, or nothing — not a host rule
    m = _HOST_RULE_RE.match(text)
    if not m:
        return None
    host, port = m.group("host"), m.group("port")
    if "/" in host:
        try:
            ipaddress.ip_network(host, strict=False)
        except ValueError:
            return None
    elif host[0].isdigit() and host.replace(".", "").isdigit():
        try:
            ipaddress.ip_address(host)
        except ValueError:
            return None
    if port is not None and not (0 < int(port) < 65536):
        return None
    return HostRule(host=host, port=int(port) if port else None, source=source)


def rules_from(entries: Iterable[Any] | None, *, source: str) -> list[HostRule]:
    out: list[HostRule] = []
    for entry in entries or []:
        rule = parse_rule(entry, source=source)
        if rule is not None and rule not in out:
            out.append(rule)
    return out


def validate_operator_rules(entries: Any) -> list[str]:
    """Normalise an operator's allow list; ``ValueError`` with a readable
    message on the first bad entry."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError("allow must be a list of hosts")
    if len(entries) > MAX_RULES:
        raise ValueError(f"at most {MAX_RULES} hosts")
    out: list[str] = []
    for entry in entries:
        rule = parse_rule(entry, source="operator")
        if rule is None:
            raise ValueError(
                f"{entry!r} is not a host rule — use a host name, *.domain, "
                "an IPv4 address or a CIDR, optionally with :port"
            )
        if rule.text() not in out:
            out.append(rule.text())
    return out


# ── Which app is calling ────────────────────────────────────────────

_resolve_cache: dict[str, tuple[float, frozenset[str]]] = {}
_resolve_lock = threading.Lock()


def _resolve(host: str) -> frozenset[str]:
    """Every address ``host`` resolves to right now (cached)."""
    now = time.monotonic()
    with _resolve_lock:
        hit = _resolve_cache.get(host)
        if hit and hit[0] > now:
            return hit[1]
    addrs: set[str] = set()
    try:
        ipaddress.ip_address(host)
        addrs.add(host)
    except ValueError:
        try:
            for info in socket.getaddrinfo(host, None):
                addrs.add(info[4][0])
        except (socket.gaierror, OSError):
            pass
    result = frozenset(addrs)
    with _resolve_lock:
        _resolve_cache[host] = (now + RESOLVE_TTL_S, result)
    return result


def forget_resolution(host: str | None = None) -> None:
    with _resolve_lock:
        if host is None:
            _resolve_cache.clear()
        else:
            _resolve_cache.pop(host, None)


def contract_host(row) -> str | None:
    url = getattr(row, "url", None)
    if not url:
        return None
    try:
        return urlparse(url).hostname
    except ValueError:
        return None


def app_for_address(rows: Iterable[Any], client_ip: str) -> Any | None:
    """The registered app whose contract host resolves to ``client_ip``."""
    try:
        wanted = str(ipaddress.ip_address(client_ip))
    except ValueError:
        return None
    for row in rows:
        host = contract_host(row)
        if host and wanted in _resolve(host):
            return row
    return None


# ── Policy ──────────────────────────────────────────────────────────


def listing_egress(app_id: str) -> list[str]:
    """The ``network_egress`` entries the catalog listing declares."""
    try:
        from routers.apps import _load_apps_index

        for entry in _load_apps_index():
            if entry.id == app_id:
                return [str(x) for x in (entry.network_egress or [])]
    except Exception:  # noqa: BLE001 — a broken index must not block enforcement
        logger.debug("could not read the apps index for egress rules", exc_info=True)
    return []


def rules_for(row, *, declared: list[str] | None = None) -> list[HostRule]:
    declared = listing_egress(row.id) if declared is None else declared
    rules = rules_from(declared, source="listing")
    for rule in rules_from(getattr(row, "egress_allow", None) or [], source="operator"):
        if rule not in rules:
            rules.append(rule)
    return rules


def match(rules: Iterable[HostRule], host: str, port: int) -> HostRule | None:
    for rule in rules:
        if rule.matches(host, port):
            return rule
    return None


# ── Denials: memory, log, inbox ─────────────────────────────────────

_denials: dict[str, dict[str, dict[str, Any]]] = {}
_alerted: dict[tuple[str, str], float] = {}
_denials_lock = threading.Lock()


def _remember_denial(app_id: str, destination: str) -> bool:
    """Count one denial; ``True`` when an inbox alert is due for it."""
    now = time.time()
    with _denials_lock:
        per_app = _denials.setdefault(app_id, {})
        entry = per_app.get(destination)
        if entry is None:
            if len(per_app) >= MAX_DENIALS_PER_APP:
                oldest = min(per_app, key=lambda k: per_app[k]["last_seen"])
                per_app.pop(oldest, None)
            entry = per_app[destination] = {"count": 0, "first_seen": now, "last_seen": now}
        entry["count"] += 1
        entry["last_seen"] = now
        last_alert = _alerted.get((app_id, destination), 0.0)
        due = now - last_alert >= ALERT_EVERY_S
        if due:
            _alerted[(app_id, destination)] = now
        return due


def denials_view(app_id: str) -> list[dict[str, Any]]:
    with _denials_lock:
        per_app = dict(_denials.get(app_id, {}))
    out = []
    for destination, entry in sorted(per_app.items(), key=lambda kv: -kv[1]["last_seen"]):
        host, _, port = destination.rpartition(":")
        out.append({
            "host": host, "port": int(port) if port.isdigit() else None,
            "count": entry["count"],
            "first_seen": datetime.fromtimestamp(entry["first_seen"], UTC).isoformat(),
            "last_seen": datetime.fromtimestamp(entry["last_seen"], UTC).isoformat(),
        })
    return out


def clear_denials(app_id: str | None = None) -> None:
    with _denials_lock:
        if app_id is None:
            _denials.clear()
            _alerted.clear()
        else:
            _denials.pop(app_id, None)
            for key in [k for k in _alerted if k[0] == app_id]:
                _alerted.pop(key, None)


def _raise_inbox_alert(db, row, host: str, port: int) -> None:
    from services.alerts_inbox import apply_alert

    destination = f"{host}:{port}"
    stamp = int(time.time() // ALERT_EVERY_S)
    apply_alert({
        "alert_id": f"egress-{row.id}-{destination}-{stamp}"[:64],
        "severity": "medium",
        "title": f"{row.name or row.id} tried to reach {destination}",
        "description": (
            f"The app '{row.id}' opened a connection to {destination}, which is "
            "neither declared in its catalog listing nor allowed for this "
            "install, so the egress proxy refused it. If this is a destination "
            "you configured for the app (a Home Assistant box, a camera, a "
            "webhook host), allow it from the app's card in the App Catalog "
            "→ Network. If it is not, leave it blocked and consider reporting "
            "the app."
        ),
        "source": {"kind": "platform", "name": "egress-proxy"},
        "tags": ["egress", "denied", row.id],
        "evidence": {"app_id": row.id, "host": host, "port": port},
        "fired_at": datetime.now(UTC).isoformat(),
    }, db=db)


def check(db, client_ip: str, host: str, port: int) -> dict[str, Any]:
    """The proxy's question: may the app at ``client_ip`` open
    ``host:port``? Always answers; never raises."""
    from models import InstalledApp

    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 0
    host = (host or "").strip().lower().rstrip(".")
    rows = db.query(InstalledApp).all()
    row = app_for_address(rows, client_ip)
    if row is None:
        logger.warning("egress: refused %s:%s from %s — no registered app at that address",
                       host, port, client_ip)
        return {"allowed": False, "app_id": None, "reason": "unknown client"}
    if not host or port <= 0:
        return {"allowed": False, "app_id": row.id, "reason": "bad destination"}
    rule = match(rules_for(row), host, port)
    if rule is not None:
        return {"allowed": True, "app_id": row.id, "reason": f"{rule.source}: {rule.text()}"}
    due = _remember_denial(row.id, f"{host}:{port}")
    logger.warning("egress: DENIED %s → %s:%s (not declared, not allowed)", row.id, host, port)
    if due:
        try:
            _raise_inbox_alert(db, row, host, port)
        except Exception:  # noqa: BLE001 — the inbox must never break the decision
            logger.debug("egress inbox alert failed", exc_info=True)
        try:
            from services.audit_service import write_audit_log

            write_audit_log(db, action="app.egress.denied", entity_type="app",
                            entity_id=row.id, details={"host": host, "port": port})
        except Exception:  # noqa: BLE001
            logger.debug("egress audit write failed", exc_info=True)
    return {"allowed": False, "app_id": row.id, "reason": "not declared, not allowed"}


def egress_view(row) -> dict[str, Any]:
    """What the catalog shows: declared, allowed, enforced, denied."""
    declared = listing_egress(row.id)
    rules = rules_for(row, declared=declared)
    return {
        "declared": declared,
        "allow": list(getattr(row, "egress_allow", None) or []),
        "enforced": [r.text() for r in rules],
        "denied": denials_view(row.id),
    }
