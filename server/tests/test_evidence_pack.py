# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""scripts/evidence_pack.py — the compliance evidence pack, built from
canned API answers through the injected fetcher."""
from __future__ import annotations

import importlib.util
import json
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("evidence_pack", REPO_ROOT / "scripts" / "evidence_pack.py")
ep = importlib.util.module_from_spec(_spec)
sys.modules["evidence_pack"] = ep
_spec.loader.exec_module(ep)

ANSWERS = {
    "/health": {"status": "ok", "version": "0.2.0"},
    "/api/v1/system/posture": {"deployment_mode": "offline", "ai_sovereignty": "local_only",
                               "mediamtx_allow_plaintext_outputs": False},
    "/api/v1/compliance/summary": {"total_cameras": 2, "recording_enabled": 2},
    "/api/v1/compliance/security-check": {
        "posture": "attention",
        "summary": {"cameras": 2, "covered_vendor": 0, "internet_exposed": 0, "plaintext_stream": 1,
                    "weak_credentials": 0},
        "cameras": [{"id": 1, "name": "Gate", "flags": [{"code": "plaintext_stream"}]},
                    {"id": 2, "name": "Yard", "flags": []}]},
    "/api/v1/compliance/recording-coverage?days=30": {
        "days": 30, "coverage": [{"camera_id": 1, "total_duration_hours": 700.0},
                                 {"camera_id": 2, "total_duration_hours": 720.0}]},
    "/api/v1/compliance/access-audit?days=30&limit=500": {"entries": []},
    "/api/v1/ai-models/capabilities": {"adapters": {
        "yolov8": {"url": "http://y", "capabilities": {"model": {"fingerprint": "sha256:aa"}, "network_egress": []}},
        "cloudy": {"url": "http://c", "capabilities": {"model": {"fingerprint": "sha256:bb"},
                                                       "network_egress": ["api.vendor.example"]}}}},
    "/api/v1/apps": [
        {"id": "alert-notifier", "egress": {"enforced": ["api.telegram.org"], "denied": [],
                                            "declared": ["api.telegram.org"], "allow": []},
         "config": {"webhook_token": "s3cret", "webhook_url": "https://bob:pw@hook.example/x"}},
        {"id": "loitering-detection", "egress": {"enforced": [], "denied": [{"host": "evil.example", "port": 443}]}},
    ],
    "/api/v1/apps/index": {"apps": [{"id": "alert-notifier", "signed_by": "OpenNVR CI"},
                                    {"id": "loitering-detection", "signed_by": None}]},
    "/api/v1/cameras/": {"cameras": [{"id": 1, "name": "Gate", "rtsp_url": "rtsp://admin:hunter2@10.0.0.1/s",
                                      "password": "x"}]},
}
AUDIT = [{"id": i, "action": "policy.boot_posture" if i == 1 else "camera.created", "timestamp": "t"}
         for i in range(1, 8)]


def fake_fetch(method, path):
    assert method == "GET"
    if path.startswith("/api/v1/audit-logs/"):
        skip = int(path.split("skip=")[1].split("&")[0])
        page = AUDIT[skip:skip + 500]
        return 200, json.dumps({"logs": page, "total": len(AUDIT)}).encode(), "application/json"
    if path.startswith("/api/v1/compliance/export"):
        return 200, b"camera,date,hours\nGate,2026-09-01,24\n", "text/csv"
    if path in ANSWERS:
        return 200, json.dumps(ANSWERS[path]).encode(), "application/json"
    if path.startswith("/api/v1/network/"):
        return 0, b"connection refused", "text/plain"
    return 404, b'{"detail":"nope"}', "application/json"


def test_redaction():
    assert ep.redact("rtsp://admin:hunter2@10.0.0.1/s") == "rtsp://<redacted>@10.0.0.1/s"
    assert ep.redact({"password": "x", "api_key": "y", "url": "https://u:p@h/", "n": 1}) == \
        {"password": "<redacted>", "api_key": "<redacted>", "url": "https://<redacted>@h/", "n": 1}
    assert ep.redact([{"nested": {"license_key": "k"}}]) == [{"nested": {"license_key": "<redacted>"}}]


def test_pack_contents_findings_and_manifest(tmp_path):
    out = tmp_path / "pack.zip"
    manifest = ep.build_pack(fake_fetch, url="https://nvr.test", days=30, out_path=str(out),
                             operator="admin", now=datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc))
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert {"EVIDENCE.md", "manifest.json", "core/posture.json", "compliance/security-check.json",
                "compliance/coverage.csv", "apps/installed.json", "audit/audit-logs.json",
                "audit/by-action.json", "cameras/cameras.json"} <= names
        assert "network/uplink.json" not in names                      # unreachable → missing, not fatal
        report = zf.read("EVIDENCE.md").decode()
        installed = zf.read("apps/installed.json").decode()
        cameras = zf.read("cameras/cameras.json").decode()
        audit = json.loads(zf.read("audit/audit-logs.json"))
        stored_manifest = json.loads(zf.read("manifest.json"))
    # Redaction reached every artefact.
    assert "hunter2" not in cameras and "s3cret" not in installed and "bob:pw" not in installed
    assert '"password": "<redacted>"' in cameras
    # Paged audit log, counted by action.
    assert len(audit) == 7 and stored_manifest["files"]["audit/by-action.json"]
    # Findings: pass / attention / unknown as the canned data dictates.
    by_check = {f["check"]: f for f in manifest["findings"]}
    assert by_check["Deployment mode is offline (cloud routes return 403)"]["status"] == "PASS"
    assert by_check["No plaintext (RTSP without TLS) camera streams"]["status"] == "ATTENTION"
    assert by_check["No AI adapter declares network egress"]["status"] == "ATTENTION"
    assert "cloudy" in by_check["No AI adapter declares network egress"]["detail"]
    assert by_check["Every installed app has a known image signer"]["detail"] == "loitering-detection"
    assert by_check["No app has refused egress attempts on record"]["status"] == "ATTENTION"
    cov = by_check["Recording coverage ≥ 95 % of expected hours over the period"]
    assert cov["status"] == "PASS" and cov["detail"].startswith("98.6%")
    assert by_check["Boot posture recorded in the audit log"]["status"] == "PASS"
    # The report reads the same story and names what was not collected.
    assert "⚠️ ATTENTION | No plaintext" in report and "network/uplink.json" in report
    assert "## Control mapping" in report and "FCC §889" in report
    assert manifest["missing"]["network/uplink.json"].startswith("unreachable")
    assert manifest["core_version"] == "0.2.0" and manifest["generated_at"] == "2026-09-06T12:00:00Z"
    # Hashes match the bytes in the zip.
    import hashlib
    with zipfile.ZipFile(out) as zf:
        for name, meta in stored_manifest["files"].items():
            assert hashlib.sha256(zf.read(name)).hexdigest() == meta["sha256"]


def test_everything_missing_still_produces_a_pack(tmp_path):
    out = tmp_path / "empty.zip"
    manifest = ep.build_pack(lambda m, p: (0, b"down", "text/plain"), url="http://x", days=7, out_path=str(out))
    assert set(manifest["files"]) == {"EVIDENCE.md"}
    with zipfile.ZipFile(out) as zf:
        assert set(zf.namelist()) == {"EVIDENCE.md", "manifest.json"}
    assert all(f["status"] == "UNKNOWN" for f in manifest["findings"])
    assert len(manifest["missing"]) == len(ep.COLLECT) + 1


def test_cli_arguments(capsys):
    assert ep.main(["--url", "http://x", "--days", "500", "--token", "t"]) == 2
    assert ep.main(["--url", "http://x"]) == 2
    assert "--user" in capsys.readouterr().err
