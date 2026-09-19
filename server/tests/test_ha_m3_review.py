# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Fixes from the M3 review of the Home Assistant work (HA-307)."""

from __future__ import annotations

from datetime import UTC, datetime

from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture


def test_tokens_never_get_camera_stream_credentials(env):  # noqa: F811
    s = env.Session()
    cam = s.get(env.models.Camera, 1)
    cam.rtsp_url = "rtsp://admin:hunter2@192.0.2.1:554/stream1"
    s.commit()
    s.close()
    tok = _mint(env)["token"]
    one = env.client.get("/api/v1/cameras/1", headers=_as(tok)).json()
    listed = env.client.get("/api/v1/cameras/", headers=_as(tok)).json()["cameras"]
    for row in [one, next(c for c in listed if c["id"] == 1)]:
        assert "hunter2" not in row["rtsp_url"] and "192.0.2.1" in row["rtsp_url"]
    # The operator (a user) still sees what they configured.
    mine = env.client.get("/api/v1/cameras/1", headers=env.jwt("admin")).json()
    assert "hunter2" in mine["rtsp_url"]


def test_a_session_token_only_reads(env):  # noqa: F811
    parent = _mint(env, scopes=["settings.view", "cameras.view", "recordings.view"])["token"]
    child = env.client.post("/api/v1/api-tokens/session", json={},
                            headers=_as(parent)).json()["token"]
    r = env.client.post("/api/v1/events/1/protect", json={"pre_s": 1, "post_s": 1},
                        headers=_as(child))
    assert r.status_code == 403 and "only read" in r.text
    # What a card needs still works: the events socket's ticket.
    assert env.client.post("/api/v1/events/ws-ticket", headers=_as(child)).status_code == 200


def test_sessions_are_rate_limited(env, monkeypatch):  # noqa: F811
    from services import api_tokens

    monkeypatch.setattr(api_tokens, "SESSION_MINTS_PER_MINUTE", 2)
    api_tokens._session_mints.clear()
    parent = _mint(env)["token"]
    codes = [env.client.post("/api/v1/api-tokens/session", json={},
                             headers=_as(parent)).status_code for _ in range(3)]
    assert codes == [201, 201, 429]


def test_signed_media_and_alert_images_are_not_gzipped():
    from middleware.compression import _is_media_path

    assert _is_media_path("/api/v1/media/s/m1.abc.def")
    assert _is_media_path("/api/v1/alerts-inbox/5/images/snapshot.jpg")
    assert not _is_media_path("/api/v1/alerts-inbox")
    assert not _is_media_path("/api/v1/alerts-inbox/actions")


def test_alert_media_ready_says_what_the_alert_was(monkeypatch):
    from services import media_ready

    sent = []
    monkeypatch.setattr(media_ready, "_schedule", lambda cam, payload, at: sent.append(payload))
    media_ready.schedule_for_alert({"id": 5, "alert_id": "a5", "severity": "high",
                                    "title": "Loitering", "source_name": "loitering",
                                    "image_names": ["snapshot.jpg"]}, 3, datetime.now(UTC))
    [payload] = sent
    assert (payload["severity"], payload["title"], payload["app"]) == (
        "high", "Loitering", "loitering")
