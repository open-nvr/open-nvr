#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Generate a compliance evidence pack from a running OpenNVR.

An auditor, a procurement officer or a CISO asks the same questions of
every deployment: what posture is it running in, does anything leave
the site, which cameras are covered-list or exposed, which models are
loaded and were they tampered with, what apps are installed and what
may they reach, is recording actually happening, and who did what.
This script asks the deployment's own API those questions and writes
the answers — raw JSON, a CSV, and a readable ``EVIDENCE.md`` that
maps each answer to the control frameworks in ``docs/COMPLIANCE.md`` —
into one zip with a manifest of SHA-256 hashes, so the pack itself is
tamper-evident.

    python3 scripts/evidence_pack.py --url https://nvr.example.org --user admin
    #   password from $OPENNVR_PASSWORD (or prompted); or --token <jwt>
    python3 scripts/evidence_pack.py --url http://localhost:8000 --user admin --days 90 --out site-a.zip

Read-only: every call is a GET (plus the login). Camera stream URLs
are redacted of embedded credentials before they are written. Nothing
is sent anywhere but the deployment you name. A route that fails or is
unavailable is recorded in the manifest as missing; the pack is still
produced — an incomplete pack that says so beats no pack.

Standard library only. The fetcher is injectable for tests.
"""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import io
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from typing import Any, Callable

TOOL_VERSION = "1.0"
API = "/api/v1"

#: (method, path) → (status, body bytes, content-type)
Fetcher = Callable[[str, str], tuple[int, bytes, str]]

_CRED_IN_URL = re.compile(r"(?i)(rtsps?|https?|onvif)://([^/@\s:]+)(:[^/@\s]*)?@")


def redact(obj: Any) -> Any:
    """Strip ``user:pass@`` from any URL-shaped string, recursively, and
    drop fields that are plainly secrets."""
    if isinstance(obj, dict):
        return {k: ("<redacted>" if _secret_key(k) else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    if isinstance(obj, str):
        return _CRED_IN_URL.sub(r"\1://<redacted>@", obj)
    return obj


def _secret_key(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in ("password", "secret", "token", "api_key", "apikey", "license_key"))


# ── fetching ────────────────────────────────────────────────────────


def http_fetcher(base_url: str, token: str | None, *, insecure: bool = False) -> Fetcher:
    base = base_url.rstrip("/")
    ctx = ssl._create_unverified_context() if insecure else None  # noqa: S323 — operator opt-in

    def fetch(method: str, path: str) -> tuple[int, bytes, str]:
        req = urllib.request.Request(base + path, method=method,
                                     headers={"Accept": "application/json, text/csv, */*"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:  # noqa: S310
                return resp.status, resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read() or b"", exc.headers.get("Content-Type", "") if exc.headers else ""
        except Exception as exc:  # noqa: BLE001
            return 0, str(exc).encode(), "text/plain"
    return fetch


def login(base_url: str, username: str, password: str, *, insecure: bool = False) -> str:
    ctx = ssl._create_unverified_context() if insecure else None  # noqa: S323
    body = json.dumps({"username": username, "password": password}).encode()
    req = urllib.request.Request(base_url.rstrip("/") + f"{API}/auth/login-json", data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:  # noqa: S310
        data = json.loads(resp.read().decode())
    token = data.get("access_token")
    if not token:
        raise SystemExit("login succeeded but no access_token in the response")
    return token


# ── the pack ────────────────────────────────────────────────────────

#: What is collected: (name in the zip, path, kind). ``json`` files are
#: parsed, redacted and pretty-printed; ``raw`` are stored as received.
COLLECT: list[tuple[str, str, str]] = [
    ("core/health.json", "/health", "json"),
    ("core/posture.json", f"{API}/system/posture", "json"),
    ("core/resources.json", f"{API}/system/resources", "json"),
    ("compliance/summary.json", f"{API}/compliance/summary", "json"),
    ("compliance/security-check.json", f"{API}/compliance/security-check", "json"),
    ("compliance/recording-coverage.json", f"{API}/compliance/recording-coverage?days={{days}}", "json"),
    ("compliance/access-audit.json", f"{API}/compliance/access-audit?days={{days}}&limit=500", "json"),
    ("compliance/coverage.csv", f"{API}/compliance/export?days={{days}}", "raw"),
    ("ai/capabilities.json", f"{API}/ai-models/capabilities", "json"),
    ("ai/health.json", f"{API}/ai-models/health", "json"),
    ("ai/tier0-gate.json", f"{API}/ai-models/tier0-gate", "json"),
    ("apps/installed.json", f"{API}/apps", "json"),
    ("apps/index.json", f"{API}/apps/index", "json"),
    ("cameras/cameras.json", f"{API}/cameras/", "json"),
    ("network/camera-lan.json", f"{API}/network/camera-lan", "json"),
    ("network/uplink.json", f"{API}/network/uplink", "json"),
    ("security/firewall-rules.json", f"{API}/security/firewall/rules", "json"),
    ("security/retention.json", f"{API}/security/settings/recordings_retention", "json"),
]
AUDIT_PAGE = 500
AUDIT_MAX = 20000


def collect(fetch: Fetcher, *, days: int) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Every artefact as bytes, plus the parsed JSON ones for the report."""
    files: dict[str, bytes] = {}
    parsed: dict[str, Any] = {}
    missing: dict[str, str] = {}
    for name, path, kind in COLLECT:
        status, body, _ctype = fetch("GET", path.replace("{days}", str(days)))
        if status != 200:
            missing[name] = f"HTTP {status}" if status else f"unreachable: {body[:120].decode(errors='replace')}"
            continue
        if kind == "json":
            try:
                data = redact(json.loads(body.decode() or "null"))
            except ValueError:
                missing[name] = "not JSON"
                continue
            parsed[name] = data
            files[name] = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
        else:
            files[name] = body
    # Audit log: paged, bounded, newest first as the API returns it.
    since = datetime.now(timezone.utc).replace(microsecond=0)
    start = since.timestamp() - days * 86400
    start_iso = datetime.fromtimestamp(start, timezone.utc).isoformat().replace("+00:00", "Z")
    entries: list[Any] = []
    skip = 0
    while skip < AUDIT_MAX:
        status, body, _ = fetch("GET", f"{API}/audit-logs/?skip={skip}&limit={AUDIT_PAGE}&start={urllib.parse.quote(start_iso)}")
        if status != 200:
            missing["audit/audit-logs.json"] = f"HTTP {status}" if status else "unreachable"
            break
        try:
            page = json.loads(body.decode() or "{}")
        except ValueError:
            missing["audit/audit-logs.json"] = "not JSON"
            break
        items = page.get("items") if isinstance(page, dict) else page
        if not isinstance(items, list):
            items = page.get("logs") if isinstance(page, dict) else None
        if not items:
            break
        entries.extend(items)
        if len(items) < AUDIT_PAGE:
            break
        skip += AUDIT_PAGE
    if "audit/audit-logs.json" not in missing:
        entries = redact(entries)
        parsed["audit/audit-logs.json"] = entries
        files["audit/audit-logs.json"] = (json.dumps(entries, indent=2, sort_keys=True) + "\n").encode()
        by_action: dict[str, int] = {}
        for e in entries:
            if isinstance(e, dict):
                by_action[str(e.get("action"))] = by_action.get(str(e.get("action")), 0) + 1
        parsed["audit/by-action.json"] = by_action
        files["audit/by-action.json"] = (json.dumps(by_action, indent=2, sort_keys=True) + "\n").encode()
    parsed["_missing"] = missing
    return files, parsed


# ── the readable report ─────────────────────────────────────────────


def _g(parsed: dict[str, Any], name: str, *keys: str, default: Any = None) -> Any:
    cur = parsed.get(name)
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
    return default if cur is None else cur


def findings(parsed: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(status, check, detail) rows: status is PASS / ATTENTION / UNKNOWN."""
    rows: list[tuple[str, str, str]] = []

    def row(ok: bool | None, check: str, detail: str) -> None:
        rows.append(("PASS" if ok else "ATTENTION" if ok is False else "UNKNOWN", check, detail))

    mode = _g(parsed, "core/posture.json", "deployment_mode")
    row(None if mode is None else mode == "offline", "Deployment mode is offline (cloud routes return 403)",
        f"deployment_mode={mode!r}")
    ai = _g(parsed, "core/posture.json", "ai_sovereignty")
    row(None if ai is None else ai == "local_only", "AI sovereignty is local_only (adapters with network egress refused)",
        f"ai_sovereignty={ai!r}")
    pt = _g(parsed, "core/posture.json", "mediamtx_allow_plaintext_outputs")
    row(None if pt is None else not pt, "Plaintext media outputs disabled", f"mediamtx_allow_plaintext_outputs={pt!r}")

    sc = parsed.get("compliance/security-check.json")
    if isinstance(sc, dict):
        summary = sc.get("summary") if isinstance(sc.get("summary"), dict) else {}
        n = summary.get("cameras", len(sc.get("cameras") or []))
        covered = int(summary.get("covered_vendor") or 0)
        row(not covered, "No FCC Covered List (§889) camera vendors in the inventory",
            f"{covered} flagged of {n} active camera(s)")
        exposed = int(summary.get("internet_exposed") or 0)
        row(not exposed, "No camera on a public, internet-routable IP", f"{exposed} flagged")
        plain = int(summary.get("plaintext_stream") or 0)
        row(not plain, "No plaintext (RTSP without TLS) camera streams", f"{plain} flagged")
        weak = int(summary.get("weak_credentials") or 0)
        row(not weak, "No default or blank camera usernames", f"{weak} flagged")
    else:
        row(None, "Camera security check", "compliance/security-check.json missing")

    caps = parsed.get("ai/capabilities.json")
    if isinstance(caps, dict):
        adapters = caps.get("adapters") if isinstance(caps.get("adapters"), dict) else {}
        egress, fingerprinted = [], []
        for name, a in adapters.items():
            if not isinstance(a, dict):
                continue
            contract = a.get("capabilities") if isinstance(a.get("capabilities"), dict) else a
            if contract.get("network_egress"):
                egress.append(name)
            model = contract.get("model") if isinstance(contract.get("model"), dict) else {}
            if contract.get("model_fingerprint") or model.get("fingerprint"):
                fingerprinted.append(name)
        row(not egress, "No AI adapter declares network egress",
            ", ".join(egress) or f"{len(adapters)} adapter(s), none")
        row(None if not adapters else len(fingerprinted) == len(adapters),
            "Every AI adapter reports a model fingerprint (drift is detectable)",
            f"{len(fingerprinted)} of {len(adapters)}")
    else:
        row(None, "AI adapter posture", "ai/capabilities.json missing")

    apps = parsed.get("apps/installed.json")
    index = parsed.get("apps/index.json")
    if isinstance(apps, list):
        signed = {}
        if isinstance(index, dict) and isinstance(index.get("apps"), list):
            signed = {a.get("id"): a.get("signed_by") for a in index["apps"] if isinstance(a, dict)}
        unsigned = [a.get("id") for a in apps if isinstance(a, dict) and not signed.get(a.get("id"))]
        row(not unsigned, "Every installed app has a known image signer",
            ", ".join(map(str, unsigned)) or f"{len(apps)} app(s)")
        denied = [a.get("id") for a in apps if isinstance(a, dict) and (a.get("egress") or {}).get("denied")]
        row(not denied, "No app has refused egress attempts on record", ", ".join(map(str, denied)) or "none")
        reaching = [f"{a.get('id')} → {', '.join((a.get('egress') or {}).get('enforced') or [])}"
                    for a in apps if isinstance(a, dict) and (a.get("egress") or {}).get("enforced")]
        row(True, "Apps that may reach outside the stack (declared or operator-allowed)",
            "; ".join(reaching) or "none — every app is confined to the stack")
    else:
        row(None, "Installed apps", "apps/installed.json missing")

    cov = parsed.get("compliance/recording-coverage.json")
    if isinstance(cov, dict) and isinstance(cov.get("coverage"), list):
        days_n = int(cov.get("days") or 1)
        by_cam: dict[Any, float] = {}
        for r in cov["coverage"]:
            if isinstance(r, dict):
                by_cam[r.get("camera_id")] = by_cam.get(r.get("camera_id"), 0.0) + float(r.get("total_duration_hours") or 0)
        enabled = int(_g(parsed, "compliance/summary.json", "recording_enabled", default=0) or 0)
        if by_cam or enabled:
            expected = max(enabled, len(by_cam)) * days_n * 24.0
            pct = round(100.0 * sum(by_cam.values()) / expected, 1) if expected else 0.0
            row(pct >= 95.0, "Recording coverage ≥ 95 % of expected hours over the period",
                f"{pct}% — {round(sum(by_cam.values()), 1)} h recorded across {len(by_cam)} camera(s), "
                f"{enabled} with recording enabled, {days_n} day(s)")
        else:
            row(None, "Recording coverage", "no cameras with recording enabled and no recordings in the period")
    else:
        row(None, "Recording coverage", "compliance/recording-coverage.json missing")

    ret = parsed.get("security/retention.json")
    ret_days = None
    if isinstance(ret, dict):
        value = ret.get("value") if isinstance(ret.get("value"), dict) else ret
        ret_days = value.get("retention_days") if isinstance(value, dict) else None
    row(True if ret_days else None, "Recording retention is an explicit setting",
        f"retention_days={ret_days}" if ret_days else "default (30 days) or not readable")

    audit = parsed.get("audit/audit-logs.json")
    row(None if audit is None else len(audit) > 0, "Audit log carries events for the period",
        f"{len(audit)} event(s)" if isinstance(audit, list) else "audit/audit-logs.json missing")
    by = parsed.get("audit/by-action.json") or {}
    boot = by.get("policy.boot_posture", 0)
    row(True if boot else None, "Boot posture recorded in the audit log", f"{boot} boot(s) in the period")
    return rows


CONTROL_MAP = [
    ("CISA Secure-by-Design", "default-deny posture, no shipped password, TLS-by-default",
     "core/posture.json, compliance/security-check.json"),
    ("NIST CSF 2.0 — Identify / Protect / Detect", "inventory, RBAC + TLS, fingerprint drift + correlation ids",
     "cameras/cameras.json, ai/capabilities.json, audit/audit-logs.json"),
    ("ISO/IEC 27001:2022 A.8 / A.5", "access control, logging, secure configuration",
     "audit/audit-logs.json, compliance/access-audit.json, security/firewall-rules.json"),
    ("NIST AI RMF 1.0", "model provenance, AI sovereignty, declared adapter permissions",
     "ai/capabilities.json, core/posture.json"),
    ("FCC §889 / NDAA covered vendors", "no Covered List cameras; local, auditable alternative",
     "compliance/security-check.json"),
    ("GDPR Art. 5/25/30 · India DPDP", "local processing, operator-managed keys, retention, records of processing",
     "core/posture.json, compliance/recording-coverage.json, apps/installed.json (egress)"),
    ("Supply chain (SLSA-style)", "digest-pinned, signed catalog images; built from source under the org",
     "apps/index.json (signed_by, image_digest)"),
]


def render_report(parsed: dict[str, Any], *, url: str, days: int, generated_at: str,
                  operator: str | None) -> str:
    version = _g(parsed, "core/health.json", "version", default="unknown")
    rows = findings(parsed)
    counts = {s: sum(1 for r in rows if r[0] == s) for s in ("PASS", "ATTENTION", "UNKNOWN")}
    out = [
        "# OpenNVR compliance evidence pack",
        "",
        f"Deployment: `{url}` · core {version} · generated {generated_at}"
        + (f" · by `{operator}`" if operator else "") + f" · period: last {days} days",
        "",
        "Every file in this pack was read from the deployment's own API at the time above; "
        "`manifest.json` lists each with its SHA-256. Stream URLs are redacted of embedded "
        "credentials. Nothing here was edited by hand.",
        "",
        "## Posture at a glance",
        "",
        f"**{counts['PASS']} pass · {counts['ATTENTION']} need attention · {counts['UNKNOWN']} could not be determined**",
        "",
        "| Status | Check | Evidence |",
        "|---|---|---|",
    ]
    for status, check, detail in rows:
        mark = {"PASS": "✅ PASS", "ATTENTION": "⚠️ ATTENTION", "UNKNOWN": "❔ UNKNOWN"}[status]
        out.append(f"| {mark} | {check} | {detail} |")
    out += ["", "## Control mapping", "",
            "Where each framework's evidence lives in this pack (the implementation behind each row is "
            "documented in `docs/COMPLIANCE.md` and `docs/SECURITY_ARCHITECTURE.md`).", "",
            "| Framework | Controls evidenced | Files |", "|---|---|---|"]
    for fw, controls, files in CONTROL_MAP:
        out.append(f"| {fw} | {controls} | `{files}` |")
    missing = parsed.get("_missing") or {}
    out += ["", "## Contents", ""]
    for name in sorted(n for n in parsed if not n.startswith("_")):
        out.append(f"* `{name}`")
    if missing:
        out += ["", "## Not collected", "",
                "These routes did not answer; the pack is incomplete to that extent and says so.", ""]
        for name, why in sorted(missing.items()):
            out.append(f"* `{name}` — {why}")
    out += ["", "## What this pack is not", "",
            "It is the deployment's own account of itself, produced by a read-only tool. It is not a "
            "penetration test, not a §889 attestation of the camera hardware (OpenNVR Scout is that), "
            "and not a statement about the physical perimeter, the operator's network outside the "
            "stack, or the cameras' firmware — the residual risks `docs/COMPLIANCE.md` lists remain "
            "the operator's. A framework audit uses this pack as evidence, not as the audit.", ""]
    return "\n".join(out)


def build_pack(fetch: Fetcher, *, url: str, days: int, out_path: str,
               operator: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Collect, render, zip. Returns the manifest."""
    now = now or datetime.now(timezone.utc)
    generated_at = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    files, parsed = collect(fetch, days=days)
    files["EVIDENCE.md"] = render_report(parsed, url=url, days=days, generated_at=generated_at,
                                         operator=operator).encode()
    manifest = {
        "tool": "opennvr-evidence-pack", "tool_version": TOOL_VERSION,
        "generated_at": generated_at, "deployment": url, "operator": operator, "period_days": days,
        "core_version": _g(parsed, "core/health.json", "version"),
        # Every artefact but the manifest itself (it cannot hash itself).
        "files": {name: {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                  for name, data in sorted(files.items())},
        "missing": parsed.get("_missing") or {},
        "findings": [{"status": s, "check": c, "detail": d} for s, c, d in findings(parsed)],
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            zf.writestr(name, files[name])
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", required=True, help="the deployment, e.g. https://nvr.example.org")
    ap.add_argument("--user", help="superuser to log in as (password from $OPENNVR_PASSWORD or a prompt)")
    ap.add_argument("--token", help="a superuser JWT instead of logging in")
    ap.add_argument("--days", type=int, default=30, help="period for coverage / access audit / audit log (1–90)")
    ap.add_argument("--out", help="zip path (default: opennvr-evidence-<host>-<date>.zip)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed lab deployments)")
    args = ap.parse_args(argv)
    if not 1 <= args.days <= 90:
        print("error: --days must be 1..90", file=sys.stderr)
        return 2
    token = args.token
    if not token:
        if not args.user:
            print("error: --user (with $OPENNVR_PASSWORD) or --token is required", file=sys.stderr)
            return 2
        password = os.environ.get("OPENNVR_PASSWORD") or getpass.getpass(f"password for {args.user}: ")
        try:
            token = login(args.url, args.user, password, insecure=args.insecure)
        except Exception as exc:  # noqa: BLE001
            print(f"error: login failed: {exc}", file=sys.stderr)
            return 2
    host = urllib.parse.urlsplit(args.url).hostname or "deployment"
    out = args.out or f"opennvr-evidence-{host}-{datetime.now(timezone.utc):%Y%m%d}.zip"
    manifest = build_pack(http_fetcher(args.url, token, insecure=args.insecure), url=args.url,
                          days=args.days, out_path=out, operator=args.user)
    counts = {s: sum(1 for f in manifest["findings"] if f["status"] == s) for s in ("PASS", "ATTENTION", "UNKNOWN")}
    print(f"wrote {out}: {len(manifest['files'])} file(s); {counts['PASS']} pass, "
          f"{counts['ATTENTION']} attention, {counts['UNKNOWN']} unknown"
          + (f"; {len(manifest['missing'])} route(s) not collected" if manifest["missing"] else ""))
    for f in manifest["findings"]:
        if f["status"] == "ATTENTION":
            print(f"  ! {f['check']} — {f['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
