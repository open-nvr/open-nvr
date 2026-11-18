# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""#976: the recording path on a Docker install.

Under Docker the settings-page path is a path inside the containers. Only
the compose mount (/app/recordings, host side RECORDINGS_PATH) reaches the
host disk, so a host path typed there (/mnt/storage) silently recorded
into the containers' own layers.

Pinned here:

* the API refuses such a path and says to set RECORDINGS_PATH in .env;
* paths inside the mount, and on a volume the operator mounted
  themselves, are still accepted (the latter with a MediaMTX warning);
* a blank path resets to the default and re-points MediaMTX (it used to
  fail validation, and skipped the sync even if it hadn't);
* GET reports the host folder and flags a bad path already saved;
* outside Docker the path is a host path and is left alone.
"""

from __future__ import annotations

import json


import pytest

from tests.test_api_tokens import env  # noqa: F401 - shared fixture

URL = "/api/v1/recordings/storage"


@pytest.fixture()
def docker(env, monkeypatch, tmp_path):  # noqa: F811
    """A compose-style deployment, with MediaMTX and the disk stubbed."""
    from core.config import settings
    from services.mediamtx_admin_service import MediaMtxAdminService
    from services import storage_service as ss

    monkeypatch.setattr(settings, "recordings_host_base", "/mnt/storage-host")
    monkeypatch.setattr(settings, "recordings_container_base", "/app/recordings")
    monkeypatch.setattr(settings, "recordings_base_path", "/app/recordings")
    # No disk stat or mkdir of container paths on the test host.
    monkeypatch.setattr(ss.storage_service, "get_storage_info", lambda db: {})
    # Only /data is a mount the operator added; nothing else is.
    monkeypatch.setattr(ss.os.path, "ismount", lambda p: p.replace("\\", "/") == "/data")

    calls: list = []

    async def defaults(patch):
        calls.append(("defaults", patch["recordPath"]))

    async def by_name(name, patch):
        calls.append((name, patch["recordPath"]))

    monkeypatch.setattr(MediaMtxAdminService, "pathdefaults_patch", staticmethod(defaults))
    monkeypatch.setattr(MediaMtxAdminService, "patch_path_by_name", staticmethod(by_name))
    env.mtx = calls
    return env


def _put(env, path):  # noqa: F811
    return env.client.put(URL, headers=env.jwt("admin"),
                          json={"recordings_base_path": path})


def _saved(env):  # noqa: F811
    import core.database as cdb
    from models import SecuritySetting

    s = cdb.SessionLocal()
    try:
        row = s.query(SecuritySetting).filter_by(key="recordings_storage").first()
        return json.loads(row.json_value).get("recordings_base_path") if row else None
    finally:
        s.close()


@pytest.mark.parametrize("path", ["/mnt/storage", "mnt/storage", "/app/recordings2"])
def test_docker_refuses_a_path_outside_the_mount(docker, path):
    r = _put(docker, path)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "RECORDINGS_PATH" in detail and ".env" in detail
    assert "/mnt/storage-host" in detail  # the host folder in use now
    assert docker.mtx == []  # MediaMTX untouched
    assert _saved(docker) is None


@pytest.mark.parametrize("path", ["/app/recordings", "/app/recordings/site-a"])
def test_docker_accepts_paths_inside_the_mount(docker, path):
    r = _put(docker, path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path_status"] == "ok" and "warning" not in body
    assert ("defaults", f"{path}/%path/%Y-%m-%d/%H/%M-%S-%f") in docker.mtx


def test_docker_accepts_an_operator_mount_with_a_warning(docker):
    r = _put(docker, "/data/nvr")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["path_status"] == "other_mount"
    assert "MediaMTX" in body["warning"]
    assert _saved(docker) == "/data/nvr"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_resets_to_default_and_repoints_mediamtx(docker, blank):
    # A site that saved a bad path before the guard existed.
    _put(docker, "/app/recordings/old")
    docker.mtx.clear()

    r = _put(docker, blank)
    assert r.status_code == 200, r.text
    assert r.json()["recordings_base_path"] == "/app/recordings"
    assert _saved(docker) is None
    assert ("defaults", "/app/recordings/%path/%Y-%m-%d/%H/%M-%S-%f") in docker.mtx


def test_get_reports_the_host_folder_and_a_bad_saved_path(docker):
    import core.database as cdb
    from models import SecuritySetting

    r = docker.client.get(URL, headers=docker.jwt("admin"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["docker_managed"] is True
    assert body["host_path"] == "/mnt/storage-host"
    assert body["container_path"] == "/app/recordings"
    assert body["custom_path"] is None and body["path_status"] == "ok"

    # Saved before #976 (the API refuses it now): written straight to the DB.
    s = cdb.SessionLocal()
    row = s.query(SecuritySetting).filter_by(key="recordings_storage").first()
    row.json_value = json.dumps({"recordings_base_path": "/mnt/storage"})
    s.commit()
    s.close()

    body = docker.client.get(URL, headers=docker.jwt("admin")).json()
    assert body["custom_path"] == "/mnt/storage"
    assert body["recordings_base_path"] == "/mnt/storage"
    assert body["path_status"] == "container_layer"


def test_outside_docker_any_host_path_is_fine(monkeypatch):
    from core.config import settings
    from services.storage_service import recordings_path_status

    monkeypatch.setattr(settings, "recordings_host_base", None)
    for p in ("/mnt/storage", "D:/Recordings", "./recordings", None):
        assert recordings_path_status(p) == "ok"


def test_status_helper_under_docker(monkeypatch):
    from core.config import settings
    from services import storage_service as ss

    monkeypatch.setattr(settings, "recordings_host_base", "./recordings")
    monkeypatch.setattr(settings, "recordings_container_base", "/app/recordings")
    monkeypatch.setattr(ss.os.path, "ismount", lambda p: False)
    assert ss.recordings_path_status(None) == "ok"
    assert ss.recordings_path_status("recordings") == "ok"  # legacy default
    assert ss.recordings_path_status("/app/recordings/") == "ok"
    assert ss.recordings_path_status("/app/recordings2") == "container_layer"
    assert ss.recordings_path_status("/mnt/storage") == "container_layer"
