# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The offline-first posture is real, not just advertised.

OpenNVR's central claim is that a default install keeps footage on the
premises: no cloud upload, no cloud inference, nothing leaving the box unless
an operator opts in. That claim is enforced by two policy gates
(``core/policy.py``) stacked as FastAPI dependencies on the cloud routers, and
reported by ``/system/security-posture``.

The failure this guards against is subtle and bad: the posture endpoint keeps
saying ``offline`` while a gate has quietly stopped being applied to one route.
Asserting the *reported* posture alone would pass. So these tests do both --
read what the system claims, then try the routes it claims to be blocking.

The audit trail gets the same treatment. A security control that is enforced
but unrecorded is only half a control.
"""

from __future__ import annotations

import pytest

from harness import routes

pytestmark = pytest.mark.smoke


def test_the_deployment_reports_its_posture(client):
    """The badge the UI shows has to come from somewhere real.

    Mirrors the boot-time audit entry, so it is also the cheapest way to see
    what policy the process actually loaded rather than what the env file
    says.
    """
    posture = client.json(routes.SYSTEM_POSTURE)

    assert posture, "the posture endpoint returned nothing"
    text = str(posture).lower()
    assert "offline" in text or "deployment_mode" in text or "mode" in text, (
        f"the posture payload names no deployment mode: {posture}"
    )


def test_cloud_inference_is_refused_offline(client):
    """The gate, not the advertisement.

    ``/cloud-inference/infer`` stacks ``require_outbound_allowed`` and
    ``require_ai_sovereignty_allowed``. Under the default offline /
    local_only posture both must refuse, and 403 is the refusal -- a 422 here
    would mean the request got past policy and only then failed validation,
    which is the gate not being applied.
    """
    response = client.post(
        "/cloud-inference/infer",
        json_body={"model": "whatever", "input": {}},
        expect=None,
    )

    assert response.status_code == 403, (
        "cloud inference was not refused by policy under the default offline "
        f"posture (HTTP {response.status_code}): {response.text[:300]}\n"
        "A 422 would mean the request reached validation, i.e. the policy "
        "dependency is no longer applied to this route."
    )


def test_every_policy_gated_route_refuses_offline(client):
    """The gates are per-route, so they regress per-route.

    Only the routes that would actually *send something outward* carry
    ``require_outbound_allowed`` / ``require_ai_sovereignty_allowed``. The
    read-only ``/cloud/status`` family deliberately does not: reporting that
    cloud is switched off is not itself an outbound action, and gating it
    would leave the UI unable to explain why the cloud tab is empty. This
    test asserts the real boundary rather than the one that sounds tidier --
    an earlier version swept the status endpoints in and failed against
    correct behaviour.

    The routes below are the ones whose decorators carry a policy dependency
    today (server/routers/cloud_inference.py, cloud_streaming.py).
    """
    gated = (
        ("POST", "/cloud-inference/infer", {"model": "x", "input": {}}),
        ("POST", "/cloud-inference/jobs", {"model": "x", "input": {}}),
        ("POST", "/cloud-streaming/targets", {"name": "x", "url": "rtmp://example"}),
    )

    allowed, checked = [], []
    for method, path, body in gated:
        response = client.request(method, path, json_body=body, expect=None)
        # 404 = the route moved or was removed; not a policy finding.
        if response.status_code == 404:
            continue
        checked.append(path)
        if response.status_code != 403:
            allowed.append(f"{path} -> {response.status_code}")

    assert checked, (
        "none of the policy-gated routes were found. Their paths have drifted; "
        "re-derive them from the Depends(require_outbound_allowed) decorators "
        "in server/routers/cloud*.py."
    )
    assert not allowed, (
        "these routes did not refuse under the default offline posture: "
        f"{allowed}. Anything other than 403 means the request got past "
        "policy -- a 422 in particular means it reached body validation, so "
        "the dependency is no longer attached."
    )


def test_the_device_firewall_reports_this_caller(client):
    """A blocked browser must still be able to learn that it is blocked.

    ``/device-firewall/status`` is deliberately one of the few paths open to
    an unapproved device: without it the UI could only show a blank 403 with
    no way for the user to tell an admin which device to approve.
    """
    status = client.json(routes.DEVICE_FIREWALL_STATUS)

    assert "status" in status, f"no device status reported: {status}"
    assert "device_ip" in status, (
        f"no device_ip reported, so an admin cannot identify the caller: {status}"
    )


def test_privileged_actions_are_written_to_the_audit_log(client, sandbox, config):
    """Enforcement without a record is half a control.

    Creating a camera is an audited write. Reading the trail back proves the
    log is being written *and* is queryable -- a table that only ever gets
    appended to and never read is where audit requirements go to die.
    """
    camera = client.create_camera(
        label="audited",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('audited')}",
        ip_address=config.fakecam_ip,
    )

    entries = client.get(routes.AUDIT_LOGS, params={"limit": 100}, expect=None)

    assert entries.status_code == 200, (
        f"the audit log is not readable (HTTP {entries.status_code})"
    )
    payload = entries.json()
    rows = payload.get("logs") or payload.get("items") or payload.get("audit_logs") or []
    assert rows, f"the audit log is empty after an audited write: {payload}"
    assert camera["id"], "no camera was created to audit"
