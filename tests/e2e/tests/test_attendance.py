# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Attendance, from the timetable to the marks, on a real stack.

What a bare stack can prove, and what it cannot. There is no attendance app
and no face adapter here, so nothing looks at a camera. This test plays the
app's part on the bus — it publishes ``attendance.attempt.v1`` the way the app
does — and everything after that is core, for real: the scheduler turning a
timetable period into a running session, ``/internal/app/attendance/active``
telling an app what to watch, the consumer storing the attempt and its
sighting, the finalizer deciding PRESENT and ABSENT, and the report.

What it does NOT prove: that a face in a frame is recognised. That is the
adapter's job and its own tests' (ai-adapter, face_identify).

It takes about four minutes, because the session has to really start and
really end on core's clock. It runs only in the ``attendance`` tier.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from harness.budgets import BUDGETS
from harness.waiting import eventually

pytestmark = pytest.mark.attendance

A = "/attendance"


def _publish_attempt(config, camera_id: int, payload: dict) -> None:
    """What the attendance app does after each look."""
    import nats

    async def go():
        nc = await nats.connect(config.nats, token=config.internal_key or None, connect_timeout=5)
        try:
            envelope = {"id": f"evt_{uuid.uuid4().hex[:12]}", "schema": "attendance.attempt.v1",
                        "camera_id": f"cam{camera_id}", "ts": datetime.now(UTC).timestamp(),
                        "producer": "app:attendance-e2e", "payload": payload}
            await nc.publish(f"opennvr.events.attendance.attempt.v1.cam{camera_id}",
                             json.dumps(envelope).encode())
            await nc.flush()
        finally:
            await nc.close()

    asyncio.run(go())


def test_a_timetabled_session_runs_takes_a_sighting_and_marks_people(client, sandbox, config):
    now = datetime.now(UTC)
    start = now.replace(second=0, microsecond=0)
    end = start + timedelta(minutes=2)
    if end.date() != start.date():
        pytest.skip("the 2-minute period would cross midnight UTC")

    camera = client.create_camera(
        label="attendance",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('attendance')}",
        ip_address=config.fakecam_ip,
    )
    seen = client.post(f"{A}/people", json_body={"display_name": sandbox.name("seen"),
                                                 "external_ref": sandbox.name("r1")}).json()
    absent = client.post(f"{A}/people", json_body={"display_name": sandbox.name("absent"),
                                                   "external_ref": sandbox.name("r2")}).json()
    for p in (seen, absent):
        sandbox.track(f"attendance person {p['id']}",
                      lambda pid=p["id"]: client.delete(f"{A}/people/{pid}", expect=None))
    # 1-minute buckets and the shortest finalize grace (1 min).
    group = client.post(f"{A}/groups", json_body={
        "name": sandbox.name("group"), "timezone": "UTC",
        "policy": {"bucket_minutes": 1, "attempt_interval_s": 30, "finalize_grace_minutes": 1},
    }).json()
    # A group with history can only be deactivated, never deleted.
    sandbox.track(f"attendance group {group['id']}", lambda: client.patch(
        f"{A}/groups/{group['id']}", json_body={"active": False}, expect=None))
    client.put(f"{A}/groups/{group['id']}/members",
               json_body={"person_ids": [seen["id"], absent["id"]]})
    client.post(f"{A}/timetable", json_body={
        "group_id": group["id"], "weekday": start.weekday(),
        "start_time": start.strftime("%H:%M"), "end_time": end.strftime("%H:%M"),
        "camera_ids": [camera["id"]]})

    # The scheduler materialises and starts it.
    session = eventually(
        lambda: [s for s in client.json(f"{A}/sessions", params={"group_id": group["id"]})["sessions"]],
        until=lambda rows: bool(rows) and rows[0]["status"] == "running",
        budget=BUDGETS.ATTENDANCE_SESSION_RUNNING,
        describe="the timetabled period to become a running session",
    )[0]

    # An app asking what to watch is told: this camera, these people.
    active = client.json("/internal/app/attendance/active", internal=True)["sessions"]
    mine = next(s for s in active if s["session_id"] == session["id"])
    assert mine["camera_ids"] == [camera["id"]]
    assert set(mine["person_ids"]) == {seen["uuid"], absent["uuid"]}

    # The app's look: one recognised member, and a stranger core must ignore.
    _publish_attempt(config, camera["id"], {
        "at": datetime.now(UTC).isoformat(), "ok": True, "faces_detected": 2,
        "matches": [{"person_id": seen["uuid"], "similarity": 0.8, "bbox": [1, 2, 3, 4]},
                    {"person_id": str(uuid.uuid4()), "similarity": 0.99}],
        "session_ids": [session["id"]]})
    eventually(
        lambda: client.json(f"{A}/sessions/{session['id']}"),
        until=lambda d: any(p["sightings"] for p in d["people"]),
        budget=BUDGETS.QUICK,
        describe="the published attempt to become a sighting",
    )

    final = eventually(
        lambda: client.json(f"{A}/sessions/{session['id']}"),
        until=lambda d: d["status"] == "final",
        budget=BUDGETS.ATTENDANCE_SESSION_FINAL,
        describe="the session to end and be marked",
    )
    marks = {p["person_id"]: p["status"] for p in final["people"]}
    assert marks == {seen["id"]: "PRESENT", absent["id"]: "ABSENT"}, final
    assert final["no_coverage"] is False

    # The report says the same, and a person can correct it.
    day = start.date().isoformat()
    report = client.json(f"{A}/report", params={"group_id": group["id"], "from": day, "to": day})
    rows = {r["person_id"]: r for r in report["people"]}
    assert rows[seen["id"]]["present"] == 1 and rows[absent["id"]]["absent"] == 1
    client.patch(f"{A}/sessions/{session['id']}/marks/{absent['id']}",
                 json_body={"status": "EXCUSED", "note": "e2e"})
    again = client.json(f"{A}/sessions/{session['id']}")
    assert {p["person_id"]: p["status"] for p in again["people"]}[absent["id"]] == "EXCUSED"
