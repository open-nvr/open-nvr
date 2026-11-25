# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""OpenNVR updates in the app (routers/system_update.py, services/update_status.py).

* status is read from the host's files: the update found, the run in
  progress, history, whether the runner is alive, a pending request;
* a request is only accepted for the update the host found, with updates
  on, nothing running or waiting, and no manual steps; it lands as a plain
  file for the host runner and is audited;
* the policy is validated, stored and mirrored for the host;
* seeing needs settings.view, acting needs system.update.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_api_tokens import env  # noqa: F401 - shared fixture

URL = "/api/v1/system/update"


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The two hand-off folders, as the host and core see them."""
    from services import update_status

    state, req = tmp_path / "updates", tmp_path / "update-requests"
    state.mkdir()
    req.mkdir()
    monkeypatch.setattr(update_status, "STATE_DIR", state)
    monkeypatch.setattr(update_status, "REQUEST_DIR", req)
    return state, req


def _found(state, version="9.9.9", **extra):
    """The host's check found *version* (a manifest it verified)."""
    doc = {"schema": 1, "product": "opennvr", "version": version, "channel": "stable", "min_updater": 1,
           "git_commit": "1" * 40, "git_tag": f"v{version}", "alembic_head": "x",
           "pins": {"CORE_TAG": version}, "notes_url": "https://example/notes",
           "images": [{"ref": f"ghcr.io/open-nvr/core:{version}", "digest": "sha256:" + "a" * 64,
                       "ids": {"linux/amd64": "sha256:" + "b" * 64}}], **extra}
    (state / "candidate").mkdir(exist_ok=True)
    (state / "candidate" / "release-manifest.json").write_text(json.dumps(doc))
    (state / "check.env").write_text("CHECKED_AT=2026-10-29T01:00:00Z\nCODE=0\n")


def _policy(env, mode="manual", who="admin", **kw):  # noqa: F811
    return env.client.put(f"{URL}/policy", headers=env.jwt(who), json={"mode": mode, **kw})


def test_status_reads_the_host(env, host):  # noqa: F811
    state, _ = host
    _found(state)
    (state / "state.env").write_text("FROM=0.1.8\nTO=0.1.9\nSTEP=backing_up\nSTEP=switching\nSTEP=succeeded\n"
                                     "FINISHED=2026-10-29T01:05:00Z\n")
    (state / "history.log").write_text("2026-10-20T01:00:00Z 0.1.7 0.1.8 succeeded\n"
                                       "2026-10-29T01:05:00Z 0.1.8 0.1.9 rolled_back\n")
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (state / "runner.env").write_text(f"LAST_SEEN={now}\n")
    s = env.client.get(URL, headers=env.jwt("admin")).json()
    assert s["available"]["version"] == "9.9.9" and s["available"]["notes_url"] == "https://example/notes"
    assert s["last_check"]["result"] == "available"
    assert s["run"] == {"from": "0.1.8", "to": "0.1.9", "step": "succeeded", "started": None,
                        "finished": "2026-10-29T01:05:00Z", "in_progress": False}
    assert [h["result"] for h in s["history"]] == ["rolled_back", "succeeded"]  # newest first
    assert s["runner"]["running"] is True and s["connected"] is True
    assert s["policy"] == {"mode": "auto", "channel": "stable", "auto_scope": "minor", "window_days": "mon,tue,wed,thu,fri,sat,sun",
                           "window_start": "02:00", "window_end": "04:00",  # the default: nightly, fixes and features
                           "apps_mode": "", "apps_auto_scope": "", "app_modes": {}}  # apps: the same
    assert s["pending_request"] is None


def test_a_found_version_that_is_not_newer_is_not_offered(env, host):  # noqa: F811
    from main import __version__

    _found(host[0], version=__version__)
    assert env.client.get(URL, headers=env.jwt("admin")).json()["available"] is None


def test_a_silent_runner_is_reported(env, host):  # noqa: F811
    old = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (host[0] / "runner.env").write_text(f"LAST_SEEN={old}\n")
    assert env.client.get(URL, headers=env.jwt("admin")).json()["runner"]["running"] is False


def test_request_lands_for_the_host_and_is_audited(env, host):  # noqa: F811
    state, req = host
    _found(state)
    assert _policy(env).status_code == 200
    r = env.client.post(f"{URL}/request", headers=env.jwt("admin"), json={"version": "9.9.9"})
    assert r.status_code == 202, r.text
    lines = dict(line.split("=", 1) for line in (req / "request.env").read_text().splitlines())
    assert lines["TO"] == "9.9.9" and lines["ACTION"] == "update" and lines["REQUESTED_BY"] == "admin"
    assert lines["ID"] == r.json()["request"]
    s = env.client.get(URL, headers=env.jwt("admin")).json()
    assert s["pending_request"]["to"] == "9.9.9"
    # The runner marks it done: no longer pending.
    (state / "last-request").write_text(lines["ID"] + "\n")
    assert env.client.get(URL, headers=env.jwt("admin")).json()["pending_request"] is None
    sess = env.Session()
    actions = [a for (a,) in sess.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action.like("system.update%")).all()]
    sess.close()
    assert actions == ["system.update_policy", "system.update_request"]


@pytest.mark.parametrize("setup, body, words", [
    (lambda s: None, {"version": "9.9.9"}, "not the update this site found"),
    (lambda s: _found(s), {"version": "9.9.8"}, "not the update this site found"),
    (lambda s: (_found(s), (s / "state.env").write_text("STEP=switching\n")), {"version": "9.9.9"}, "already running"),
    (lambda s: _found(s, manual_steps=[{"id": "x", "text": "Add NATS_APPS_TOKEN"}]), {"version": "9.9.9"},
     "Add NATS_APPS_TOKEN"),
])
def test_requests_that_cannot_go_ahead(env, host, setup, body, words):  # noqa: F811
    setup(host[0])
    _policy(env)
    r = env.client.post(f"{URL}/request", headers=env.jwt("admin"), json=body)
    assert r.status_code == 409 and words in r.json()["detail"], r.text
    assert not (host[1] / "request.env").exists()


def test_no_request_while_updates_are_off_or_one_is_waiting(env, host):  # noqa: F811
    _found(host[0])
    assert _policy(env, mode="off").status_code == 200  # the default (auto) would allow it
    r = env.client.post(f"{URL}/request", headers=env.jwt("admin"), json={"version": "9.9.9"})
    assert r.status_code == 409 and "turned off" in r.json()["detail"]
    _policy(env)
    assert env.client.post(f"{URL}/request", headers=env.jwt("admin"), json={"version": "9.9.9"}).status_code == 202
    r = env.client.post(f"{URL}/request", headers=env.jwt("admin"), json={"version": "9.9.9"})
    assert r.status_code == 409 and "already requested" in r.json()["detail"]
    assert env.client.delete(f"{URL}/request", headers=env.jwt("admin")).status_code == 200
    assert env.client.delete(f"{URL}/request", headers=env.jwt("admin")).status_code == 404


def test_without_the_folders_mounted_nothing_is_requested(env, host, monkeypatch):  # noqa: F811
    from services import update_status

    _found(host[0])
    _policy(env)
    monkeypatch.setattr(update_status, "REQUEST_DIR", host[0] / "missing")
    r = env.client.post(f"{URL}/request", headers=env.jwt("admin"), json={"version": "9.9.9"})
    assert r.status_code == 503


def test_policy_is_validated_stored_and_mirrored(env, host):  # noqa: F811
    r = _policy(env, mode="auto", auto_scope="patch", window_days="Sat, sun,sat", window_start="01:30", window_end="03:00")
    assert r.status_code == 200, r.text
    assert r.json()["window_days"] == "sat,sun"
    mirrored = (host[1] / "policy.env").read_text().splitlines()
    assert "MODE=auto" in mirrored and "WINDOW_DAYS=sat,sun" in mirrored and "AUTO_SCOPE=patch" in mirrored
    assert env.client.get(URL, headers=env.jwt("admin")).json()["policy"]["mode"] == "auto"
    for bad in ({"mode": "always"}, {"mode": "auto", "window_days": "someday"},
                {"mode": "auto", "window_start": "25:00"}, {"mode": "auto", "auto_scope": "major"}):
        assert env.client.put(f"{URL}/policy", headers=env.jwt("admin"), json=bad).status_code == 422


def test_seeing_needs_settings_view_acting_needs_system_update(env, host):  # noqa: F811
    _found(host[0])
    assert env.client.get(URL, headers=env.jwt("vera")).status_code == 200
    assert _policy(env, who="vera").status_code == 403
    assert env.client.post(f"{URL}/request", headers=env.jwt("vera"), json={"version": "9.9.9"}).status_code == 403
    assert env.client.delete(f"{URL}/request", headers=env.jwt("vera")).status_code == 403


def test_a_token_may_ask_for_the_update_only_with_its_scope_and_never_set_policy(env, host):  # noqa: F811
    """Home Assistant's Install button (UPD-305): a token with system.update
    asks for the update the host found; without it, or for the policy, no."""
    from tests.test_api_tokens import _as, _mint

    _found(host[0])
    _policy(env)
    plain = _mint(env, scopes=["settings.view"])["token"]
    assert env.client.get(URL, headers=_as(plain)).status_code == 200
    assert env.client.post(f"{URL}/request", headers=_as(plain), json={"version": "9.9.9"}).status_code == 403
    can = _mint(env, scopes=["settings.view", "system.update"])["token"]
    assert env.client.put(f"{URL}/policy", headers=_as(can), json={"mode": "auto"}).status_code == 403
    r = env.client.post(f"{URL}/request", headers=_as(can), json={"version": "9.9.9"})
    assert r.status_code == 202, r.text
    assert env.client.delete(f"{URL}/request", headers=_as(can)).status_code == 200
    # Only someone who holds system.update can put it in a token.
    r = env.client.post("/api/v1/api-tokens", headers=env.jwt("vera"),
                        json={"name": "x", "scopes": ["settings.view", "system.update"]})
    assert r.status_code in (403, 422)


def test_system_update_is_seeded_for_admins_only():
    from services.permission_catalog import UPGRADE_PATH_PERMISSIONS

    perm = next(p for p in UPGRADE_PATH_PERMISSIONS if p.name == "system.update")
    assert perm.backfill_from is None  # a new ability: nobody gains it on upgrade


def test_a_rolled_back_run_shows_what_the_check_said(env, host):  # noqa: F811
    state, _ = host
    _found(state)
    (state / "state.env").write_text("FROM=0.1.8\nTO=0.1.9\nSTEP=switching\nSTEP=verifying\nSTEP=rolling_back\n"
                                     "STEP=rolled_back\nFINISHED=2026-10-29T01:05:00Z\n")
    (state / "verify-candidate.log").write_text("OpenNVR 0.1.9 did not come up right:\n"
                                                "  - the database is at migration x, not y\n")
    (state / "verify-installed.log").write_text("OpenNVR 0.1.8 is running and healthy.\n")
    s = env.client.get(URL, headers=env.jwt("admin")).json()
    assert s["run"]["step"] == "rolled_back"
    assert s["run"]["message"].splitlines()[-1] == "  - the database is at migration x, not y"  # the candidate's, not the rollback's
    (state / "state.env").write_text("FROM=0.1.8\nTO=0.1.9\nSTEP=switching\nSTEP=succeeded\n")
    assert "message" not in env.client.get(URL, headers=env.jwt("admin")).json()["run"]


# ── apps released from the registry ─────────────────────────────────────

_DIGEST = "sha256:" + "c" * 64


def _app_found(state, *, signature="ok", offers=True):
    """The host's last app check (updater/apps.py) found Attendly 1.3.0."""
    report = {"id": "attendly", "repository": "ghcr.io/open-nvr/attendly", "image_key": "ATTENDLY_IMAGE",
              "state": "available" if offers else "up_to_date", "reason": "1.3.0 is available",
              "installed_ref": "ghcr.io/open-nvr/attendly:1.2.0", "installed_version": "1.2.0",
              "installed_revoked": "", "skipped": ["2.0.0: needs OpenNVR 0.3.0 (this site runs 0.1.12)"],
              "signer_identity": "x", "signer_issuer": "https://token.actions.githubusercontent.com",
              "offers": [{"version": "1.3.0", "kind": "minor", "digest": _DIGEST,
                          "ref": f"ghcr.io/open-nvr/attendly@{_DIGEST}", "rollback": "safe"}] if offers else []}
    (state / "apps.json").write_text(json.dumps({"checked_at": "2026-11-16T10:00:00Z", "core_version": "0.1.12",
                                                 "platform": "linux/amd64", "problems": [], "apps": [report]}))
    (state / "apps-check.env").write_text("CHECKED_EPOCH=1\nCODE=0\n")
    if signature:
        (state / "apps-verified.json").write_text(json.dumps(
            {f"attendly@{_DIGEST}": {"ok": signature == "ok", "message": "no matching signatures", "at": "x"}}))


def test_status_reads_the_apps_the_host_checked(env, host):  # noqa: F811
    state, _ = host
    _app_found(state)
    (state / "apps-state.env").write_text("APP_ID=attendly\nFROM_VERSION=1.1.0\nVERSION=1.2.0\nSTEP=stopping\n"
                                          "STEP=switching\nSTEP=verifying\nSTEP=rolling_back\nSTEP=rolled_back\n"
                                          "FINISHED=2026-11-16T09:00:00Z\n")
    (state / "apps-up.log").write_text("attendly is running unhealthy\n")
    (state / "apps-history.log").write_text("2026-11-16T09:00:00Z attendly 1.1.0 1.2.0 rolled_back\n"
                                            "2026-11-16T09:30:00Z attendly 1.1.0 1.2.0 succeeded\n")
    (state / "apps-failed.txt").write_text("attendly 1.3.0\n")
    a = env.client.get(URL, headers=env.jwt("admin")).json()["apps"]
    [app] = a["apps"]
    assert app["installed_version"] == "1.2.0" and app["state"] == "available"
    assert app["available"] == {"version": "1.3.0", "kind": "minor", "rollback": "safe", "signature": "ok",
                                "signature_message": "no matching signatures", "failed_before": True}
    assert app["skipped"] == ["2.0.0: needs OpenNVR 0.3.0 (this site runs 0.1.12)"]
    assert a["last_check"] == {"at": "2026-11-16T10:00:00Z", "ok": True, "message": ""}
    assert a["run"]["step"] == "rolled_back" and a["run"]["in_progress"] is False
    assert "unhealthy" in a["run"]["message"]
    assert [h["result"] for h in a["history"]] == ["succeeded", "rolled_back"]  # newest first
    assert a["pending_request"] is None


def test_an_app_update_request_lands_for_the_host_and_is_audited(env, host):  # noqa: F811
    state, req = host
    _app_found(state)
    r = env.client.post(f"{URL}/apps/attendly/request", headers=env.jwt("admin"))
    assert r.status_code == 202, r.text
    lines = dict(line.split("=", 1) for line in (req / "app-request.env").read_text().splitlines())
    assert lines["APP"] == "attendly" and lines["ACTION"] == "app-update" and lines["ID"] == r.json()["request"]
    assert env.client.get(URL, headers=env.jwt("admin")).json()["apps"]["pending_request"]["app"] == "attendly"
    r = env.client.post(f"{URL}/apps/attendly/request", headers=env.jwt("admin"))
    assert r.status_code == 409 and "already requested" in r.json()["detail"]
    (state / "last-app-request").write_text(lines["ID"] + "\n")  # the runner took it
    assert env.client.get(URL, headers=env.jwt("admin")).json()["apps"]["pending_request"] is None
    assert env.client.post(f"{URL}/apps/attendly/request", headers=env.jwt("admin")).status_code == 202
    assert env.client.delete(f"{URL}/apps/request", headers=env.jwt("admin")).status_code == 200
    assert env.client.delete(f"{URL}/apps/request", headers=env.jwt("admin")).status_code == 404
    sess = env.Session()
    actions = [a for (a,) in sess.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action.like("system.app_update%")).all()]
    sess.close()
    assert sorted(actions) == ["system.app_update_cancel", "system.app_update_request", "system.app_update_request"]


@pytest.mark.parametrize("setup, policy, app, words", [
    (lambda s: _app_found(s, offers=False), {}, "attendly", "No newer release"),
    (lambda s: _app_found(s, signature="failed"), {}, "attendly", "no matching signatures"),
    (lambda s: _app_found(s), {"app_modes": {"attendly": "notify"}}, "attendly", "'notify'"),
    (lambda s: _app_found(s), {"apps_mode": "off"}, "attendly", "'off'"),
    (lambda s: (_app_found(s), (s / "state.env").write_text("STEP=switching\n")), {}, "attendly", "already running"),
    (lambda s: (_app_found(s), (s / "apps-state.env").write_text("STEP=verifying\n")), {}, "attendly",
     "already running"),
    (lambda s: _app_found(s), {}, "gate-watch", "No newer release"),
])
def test_app_requests_that_cannot_go_ahead(env, host, setup, policy, app, words):  # noqa: F811
    setup(host[0])
    assert _policy(env, mode="auto", **policy).status_code == 200
    r = env.client.post(f"{URL}/apps/{app}/request", headers=env.jwt("admin"))
    assert r.status_code == 409 and words in r.json()["detail"], r.text
    assert not (host[1] / "app-request.env").exists()


def test_app_modes_are_validated_and_mirrored_for_the_runner(env, host):  # noqa: F811
    from updater import envfile
    from updater.policy import Policy

    r = _policy(env, mode="auto", apps_mode="manual", app_modes={"attendly": "off", "gate-watch": "auto"})
    assert r.status_code == 200, r.text
    mirrored = (host[1] / "policy.env").read_text().splitlines()
    assert "APPS_MODE=manual" in mirrored
    assert "APP_ATTENDLY_MODE=off" in mirrored and "APP_GATE_WATCH_MODE=auto" in mirrored
    assert not any(line.startswith(("APP_MODES", "APPS_AUTO_SCOPE")) for line in mirrored)  # "" = follow: left out
    p = Policy.from_env(envfile.read_env(host[1] / "policy.env"))  # what the runner reads back
    assert (p.app_mode("attendly"), p.app_mode("gate-watch"), p.app_mode("other")) == ("off", "auto", "manual")
    for bad in ({"app_modes": {"Not_Kebab": "auto"}}, {"app_modes": {"attendly": "always"}},
                {"apps_mode": "sometimes"}, {"apps_auto_scope": "major"}):
        assert _policy(env, mode="auto", **bad).status_code == 422, bad


def test_app_requests_need_system_update(env, host):  # noqa: F811
    _app_found(host[0])
    assert env.client.post(f"{URL}/apps/attendly/request", headers=env.jwt("vera")).status_code == 403
    assert env.client.delete(f"{URL}/apps/request", headers=env.jwt("vera")).status_code == 403
    assert env.client.post(f"{URL}/apps/Not_An_Id/request", headers=env.jwt("admin")).status_code == 404


# -- Check now ---------------------------------------------------------------


def _runner_alive(state):
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    (state / "runner.env").write_text(f"LAST_SEEN={now}\n")


def test_check_now_lands_for_the_host_and_is_audited(env, host):  # noqa: F811
    """Check now: a plain file the runner picks up on its next tick; pending
    until the runner marks it; a second click meanwhile asks nothing new."""
    state, req = host
    _runner_alive(state)
    r = env.client.post(f"{URL}/check", headers=env.jwt("admin"))
    assert r.status_code == 202, r.text
    lines = dict(line.split("=", 1) for line in (req / "check-request.env").read_text().splitlines())
    assert lines["ACTION"] == "check" and lines["REQUESTED_BY"] == "admin" and lines["ID"] == r.json()["request"]
    assert env.client.get(URL, headers=env.jwt("admin")).json()["pending_check"]["requested_by"] == "admin"
    again = env.client.post(f"{URL}/check", headers=env.jwt("admin"))
    assert again.status_code == 202 and again.json() == {"request": None, "already_requested": True}
    assert dict(line.split("=", 1) for line in (req / "check-request.env").read_text().splitlines())["ID"] == lines["ID"]
    # The runner marks it taken: no longer pending, and a new one may be asked.
    (state / "last-check-request").write_text(lines["ID"] + "\n")
    assert env.client.get(URL, headers=env.jwt("admin")).json()["pending_check"] is None
    assert env.client.post(f"{URL}/check", headers=env.jwt("admin")).json()["already_requested"] is False
    sess = env.Session()
    actions = [a for (a,) in sess.query(env.models.AuditLog.action)
               .filter(env.models.AuditLog.action == "system.update_check").all()]
    sess.close()
    assert len(actions) == 2


def test_check_now_needs_a_live_runner_and_the_folders(env, host, monkeypatch):  # noqa: F811
    from services import update_status

    state, req = host
    old = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (state / "runner.env").write_text(f"LAST_SEEN={old}\n")
    r = env.client.post(f"{URL}/check", headers=env.jwt("admin"))
    assert r.status_code == 409 and "not running" in r.json()["detail"]
    assert not (req / "check-request.env").exists()
    _runner_alive(state)
    monkeypatch.setattr(update_status, "REQUEST_DIR", state / "missing")
    assert env.client.post(f"{URL}/check", headers=env.jwt("admin")).status_code == 503


def test_check_now_needs_system_update(env, host):  # noqa: F811
    _runner_alive(host[0])
    assert env.client.post(f"{URL}/check", headers=env.jwt("vera")).status_code == 403
    assert not (host[1] / "check-request.env").exists()
