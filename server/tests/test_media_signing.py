# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-112: signed media URLs.

* tokens verify only unmodified, unexpired, with a live key; rotating once
  keeps old URLs working, rotating twice revokes them;
* signing needs the kind's permission and the camera (a token also needs
  the camera in its allow-list);
* fetching needs no login, and the signer is re-checked every time: a
  revoked API token, a disabled user or a lost camera kill the URL;
* the device firewall lets only /api/v1/media/s/ through;
* fetches are audited, at most once per URL per 10 minutes.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest

from services import media_signing as ms
from tests.test_api_tokens import _as, _mint, env  # noqa: F401 - shared fixture

JPEG = b"\xff\xd8\xff\xe0fakejpeg\xff\xd9"


def test_sign_verify_tamper_expire_rotate(env):  # noqa: F811
    s = env.Session()
    try:
        tok, exp = ms.sign(s, {"k": "event", "i": 1, "u": 1}, ttl_s=120)
        assert ms.verify(s, tok)["i"] == 1 and exp > time.time()
        prefix, body, sig = tok.split(".")
        forged = json.loads(ms._unb64(body))
        forged["i"] = 2
        with pytest.raises(ms.BadToken):
            ms.verify(s, f"{prefix}.{ms._b64(json.dumps(forged).encode())}.{sig}")
        with pytest.raises(ms.BadToken):
            ms.verify(s, "garbage")
        old, _ = ms.sign(s, {"k": "event", "i": 1, "u": 1}, ttl_s=120)
        s.expire_all()
        ms.rotate_keys(s)
        assert ms.verify(s, old)           # previous key still verifies
        ms.rotate_keys(s)
        with pytest.raises(ms.BadToken):
            ms.verify(s, old)              # two rotations: gone
    finally:
        s.close()


def test_expiry(env, monkeypatch):  # noqa: F811
    s = env.Session()
    try:
        tok, _ = ms.sign(s, {"k": "event", "i": 1, "u": 1}, ttl_s=60)
        monkeypatch.setattr(ms.time, "time", lambda: 10**10)
        with pytest.raises(ms.BadToken):
            ms.verify(s, tok)
    finally:
        s.close()


# ── routes ────────────────────────────────────────────────────────────


@pytest.fixture()
def media(env, monkeypatch, tmp_path):  # noqa: F811
    from services import evidence_store
    from services.timeline_service import record_track_visit

    f = tmp_path / "e.jpg"
    f.write_bytes(JPEG)
    monkeypatch.setattr(evidence_store, "resolve_evidence", lambda rel: f if rel else None)
    ms._audited.clear()
    s = env.Session()
    ev = record_track_visit(s, camera_id=1, label="person",
                            started_at=datetime.now(UTC), evidence_path="ab/e.jpg")
    alert = env.models.AppAlert(alert_id="a-1", fired_at=datetime.now(UTC), severity="high",
                                title="t", camera_id="cam1",
                                images=json.dumps({"face": "ab/f.jpg"}))
    s.add(alert)
    s.commit()
    ids = {"event": ev.id, "alert": alert.id}
    s.close()
    return ids


def _sign(env, headers, **body):  # noqa: F811
    return env.client.post("/api/v1/media/sign", headers=headers, json=body)


def test_sign_then_fetch_without_login(env, media):  # noqa: F811
    r = _sign(env, env.jwt("admin"), kind="event", id=media["event"])
    assert r.status_code == 200, r.text
    url = r.json()["url"]
    got = env.client.get(url)                      # no Authorization header
    assert got.status_code == 200 and got.content == JPEG
    assert "private" in got.headers["cache-control"]
    assert env.client.get(url[:-3] + "xyz").status_code == 403
    a = _sign(env, env.jwt("admin"), kind="alert_image", id=media["alert"], name="face")
    assert env.client.get(a.json()["url"]).content == JPEG
    assert _sign(env, env.jwt("admin"), kind="alert_image", id=media["alert"],
                 name="nope").status_code == 404
    assert _sign(env, env.jwt("admin"), kind="event", id=media["event"],
                 name="../etc").status_code == 422


def test_signing_needs_permission_and_camera(env, media):  # noqa: F811
    # vera sees camera 3 only.
    assert _sign(env, env.jwt("vera"), kind="event", id=media["event"]).status_code == 404
    tok = _mint(env, scopes=["cameras.view"], camera_ids=[1])["token"]
    assert _sign(env, _as(tok), kind="event", id=media["event"]).status_code == 403
    tok2 = _mint(env, name="rv", scopes=["cameras.view", "recordings.view"],
                 camera_ids=[2])["token"]
    assert _sign(env, _as(tok2), kind="event", id=media["event"]).status_code == 404
    assert _sign(env, _as(tok2), kind="clip", camera_id=1,
                 start=datetime.now(UTC).isoformat(), duration_s=10).status_code == 403


def test_the_url_dies_with_the_signers_access(env, media):  # noqa: F811
    out = _mint(env, scopes=["cameras.view", "recordings.view"], camera_ids=[1])
    url = _sign(env, _as(out["token"]), kind="event", id=media["event"]).json()["url"]
    assert env.client.get(url).status_code == 200
    env.client.delete(f"/api/v1/api-tokens/{out['id']}", headers=env.jwt("admin"))
    assert env.client.get(url).status_code == 403

    # A user who loses the camera loses the URL.
    s = env.Session()
    s.get(env.models.Camera, 3).owner_id = env.ids["admin"]
    s.commit()
    ev3 = env.models.TimelineEvent(camera_id=3, source="tier0", event_type="track",
                                   label="car", started_at=datetime.now(UTC),
                                   evidence_path="x/y.jpg")
    s.add(ev3)
    s.commit()
    ev3_id = ev3.id
    s.close()
    url = _sign(env, env.jwt("vera"), kind="event", id=ev3_id).json()["url"]
    assert env.client.get(url).status_code == 200
    s = env.Session()
    s.query(env.models.CameraPermission).delete()
    s.commit()
    s.close()
    assert env.client.get(url).status_code == 403

    # A disabled user's URLs stop.
    url = _sign(env, env.jwt("admin"), kind="event", id=media["event"]).json()["url"]
    s = env.Session()
    s.get(env.models.User, env.ids["admin"]).is_active = False
    s.commit()
    s.close()
    assert env.client.get(url).status_code == 403


def test_clip_urls_stream_the_signed_range(env, media, monkeypatch):  # noqa: F811
    from fastapi.responses import Response

    import routers.recordings as rec

    calls = []

    async def fake_stream(path, start, duration, filename, *, inline=False):
        calls.append((path, start, duration, inline))
        return Response(b"mp4", media_type="video/mp4")

    monkeypatch.setattr(rec, "stream_playback_clip", fake_stream)
    start = datetime(2026, 9, 18, 10, 0, 5, tzinfo=UTC)
    r = _sign(env, env.jwt("admin"), kind="clip", camera_id=1,
              start=start.isoformat(), duration_s=12.5)
    assert r.status_code == 200, r.text
    assert env.client.get(r.json()["url"]).content == b"mp4"
    assert calls[0][1:] == ("2026-09-18T10:00:05.000000Z", 12.5, True)
    assert _sign(env, env.jwt("admin"), kind="clip", camera_id=1,
                 start=start.isoformat(), duration_s=ms.MAX_CLIP_S + 1).status_code == 422


def test_fetches_are_audited_but_rate_limited(env, media):  # noqa: F811
    url = _sign(env, env.jwt("admin"), kind="event", id=media["event"]).json()["url"]
    for _ in range(3):
        env.client.get(url)
    s = env.Session()
    rows = s.query(env.models.AuditLog).filter_by(action="media.fetch").all()
    signs = s.query(env.models.AuditLog).filter_by(action="media.sign").count()
    s.close()
    assert len(rows) == 1 and signs == 1
    assert json.loads(rows[0].details)["actor"] == "user:admin"


def test_only_signed_media_bypasses_the_device_firewall():
    from middleware.device_firewall import _is_open

    assert _is_open("/api/v1/media/s/m1.abc.def")
    assert not _is_open("/api/v1/media/sign")
    assert not _is_open("/api/v1/media/keys/rotate")


def test_rotate_is_superuser_only(env):  # noqa: F811
    assert env.client.post("/api/v1/media/keys/rotate",
                           headers=env.jwt("vera")).status_code == 403
    assert env.client.post("/api/v1/media/keys/rotate",
                           headers=env.jwt("admin")).status_code == 200



def test_losing_the_kinds_permission_kills_the_url(env, media):  # noqa: F811
    """M1 review: fetch re-checked the camera but not recordings.view."""
    url = _sign(env, env.jwt("vera"), kind="event", id=_vera_event(env)).json()["url"]
    assert env.client.get(url).status_code == 200
    s = env.Session()
    rec = s.query(env.models.Permission).filter_by(name="recordings.view").one()
    s.query(env.models.RolePermission).filter_by(
        role_id=env.ids["viewer_role"], permission_id=rec.id).delete()
    s.commit()
    s.close()
    assert env.client.get(url).status_code == 403


def _vera_event(env):  # noqa: F811
    s = env.Session()
    ev = env.models.TimelineEvent(camera_id=3, source="tier0", event_type="track",
                                  label="car", started_at=datetime.now(UTC),
                                  evidence_path="x/z.jpg")
    s.add(ev)
    s.commit()
    eid = ev.id
    s.close()
    return eid


# ── review fixes ──────────────────────────────────────────────────────────


def test_a_malformed_token_is_a_403_never_a_500(env, media):  # noqa: F811
    """The payload is attacker JSON on a route with no login. A list (or
    dict) for the key id is unhashable, and a non-ASCII signature makes
    compare_digest raise; both used to escape verify as TypeError and
    turn into a 500 instead of the 'bad link' 403."""
    s = env.Session()
    try:
        good, _ = ms.sign(s, {"k": "event", "i": media["event"], "u": 1}, ttl_s=120)
        prefix, body, sig = good.split(".")
        keys = ms._keys(s)
        secret = keys["keys"][keys["current"]]
    finally:
        s.close()
    claims = json.loads(ms._unb64(body))
    for bad_kid in (["a"], {"a": 1}, 7, None):
        forged = ms._b64(json.dumps({**claims, "v": bad_kid}).encode())
        # Signed with the real key so only the key-id shape is at fault.
        tok = f"{prefix}.{forged}.{ms._mac(secret, forged)}"
        s = env.Session()
        try:
            with pytest.raises(ms.BadToken):
                ms.verify(s, tok)
        finally:
            s.close()
        assert env.client.get(f"/api/v1/media/s/{tok}").status_code == 403
    for bad_sig in ("é" * 8, sig[:-1] + "ÿ"):
        s = env.Session()
        try:
            with pytest.raises(ms.BadToken):
                ms.verify(s, f"{prefix}.{body}.{bad_sig}")
        finally:
            s.close()
        assert env.client.get(f"/api/v1/media/s/{prefix}.{body}.{bad_sig}").status_code == 403
    assert env.client.get(f"/api/v1/media/s/{good}").status_code == 200


def test_the_request_log_never_holds_a_signed_media_token(env, media, monkeypatch):  # noqa: F811
    """The signed URL is the credential and the request log used to print
    it in full (path, url and message) on every fetch; a 403 from the
    route goes through the same middleware."""
    import middleware.request_logging as rl

    records: list = []

    class _Capture:
        def log_action(self, action, **kw):
            records.append((action, kw))

        def error(self, msg, **kw):
            records.append(("error", {"message": msg, **kw}))

    monkeypatch.setattr(rl, "api_logger", _Capture())
    url = _sign(env, env.jwt("admin"), kind="event", id=media["event"]).json()["url"]
    token = url.rsplit("/", 1)[1]
    assert env.client.get(url).status_code == 200
    assert env.client.get(url[:-3] + "xyz").status_code == 403
    fetches = [r for r in records if "/media/s/" in r[1].get("message", "")
               or "/media/s/" in str(r[1].get("extra_data", {}).get("path"))]
    assert len(fetches) == 4, records                     # start + complete, twice
    blob = repr(fetches)
    assert token not in blob and token[:20] not in blob and "xyz" not in blob
    assert "/api/v1/media/s/<redacted>" in blob
    # Other paths are untouched.
    assert any("/api/v1/media/sign" in repr(r) for r in records)
