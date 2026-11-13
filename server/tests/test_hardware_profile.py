"""The hardware bar an app or adapter inherits from its tasks, and whether
this box meets it (services/hardware_profile). Advisory: a verdict, never
a gate."""
from __future__ import annotations

from pathlib import Path

import pytest

# test_apps_index sets the environment the app needs and owns the TestClient
# fixture; importing it here reuses both for the two route tests below.
from tests.test_apps_index import client  # noqa: F401,E402
from routers.ai_models import TaskEntry, TaskHardware, _load_tasks_registry  # noqa: E402
from services import hardware_profile as hp  # noqa: E402


def _entry(task, cores=None, ram=None, gpu=None, aliases=()):
    return TaskEntry(task=task, label=task, aliases=list(aliases),
                     hardware=TaskHardware(cores=cores, ram_gb=ram, gpu=gpu))


REG = [
    _entry("object_detection", 2, 1, "recommended", aliases=["detect_objects"]),
    _entry("image_captioning", 4, 4, "recommended", aliases=["scene_caption"]),
    _entry("text_to_speech", 4, 1, "none"),
    _entry("package_detection", 2, 1, "none"),
    TaskEntry(task="bare", label="bare"),
]


def test_every_shipped_task_carries_the_bar_as_numbers():
    """tasks.yml's hardware block is what the fit badge reads; a task
    without the numbers would make an app that needs it look free."""
    for e in _load_tasks_registry():
        assert e.hardware is not None, e.task
        assert e.hardware.cores and e.hardware.ram_gb is not None and e.hardware.gpu in hp.GPU_RANK, e.task


def test_requirement_is_the_max_over_tasks_and_the_strongest_gpu_word():
    req = hp.requirement_for_tasks(["object_detection", "text_to_speech", "package_detection"], REG)
    assert (req.cores, req.ram_gb, req.gpu) == (4, 1.0, "recommended")
    assert req.tasks == ["object_detection", "text_to_speech", "package_detection"]


def test_aliases_canonicalise_and_a_bare_task_adds_nothing():
    req = hp.requirement_for_tasks(["scene_caption", "detect_objects", "bare", "unknown_task"], REG)
    assert req.tasks == ["image_captioning", "object_detection"]
    assert (req.cores, req.ram_gb, req.gpu) == (4, 4.0, "recommended")
    assert hp.describe(["bare"], hp.HostProfile(8, 16.0, "cpu"), REG) is None


@pytest.mark.parametrize("host,verdict,why", [
    (hp.HostProfile(8, 16.0, "cuda"), "ok", []),
    (hp.HostProfile(8, 16.0, "cpu"), "tight", ["a GPU is recommended"]),
    (hp.HostProfile(4, 16.0, "cuda"), "tight", ["4 cores is the floor"]),
    (hp.HostProfile(2, 16.0, "cuda"), "short", ["needs 4 cores, this box has 2"]),
    (hp.HostProfile(8, 2.0, "cuda"), "short", ["needs 4 GB RAM, this box has 2"]),
    (hp.HostProfile(8, None, "cuda"), "ok", []),   # RAM unknown is not held against the box
])
def test_fit_verdicts(host, verdict, why):
    req = hp.requirement_for_tasks(["image_captioning"], REG)
    out = hp.fit(req, host)
    assert out["verdict"] == verdict
    for w in why:
        assert any(w in r for r in out["reasons"]), out


def test_a_required_gpu_is_short_without_one():
    reg = [_entry("heavy", 4, 8, "required")]
    assert hp.fit(hp.requirement_for_tasks(["heavy"], reg), hp.HostProfile(16, 64.0, "cpu"))["verdict"] == "short"
    assert hp.fit(hp.requirement_for_tasks(["heavy"], reg), hp.HostProfile(16, 64.0, "cuda"))["verdict"] == "ok"


def test_cgroup_quota_caps_the_core_count(tmp_path: Path):
    (tmp_path / "cpu.max").write_text("150000 100000\n")
    assert hp._cgroup_cpu_quota(tmp_path) == 1.5
    (tmp_path / "cpu.max").write_text("max 100000\n")
    assert hp._cgroup_cpu_quota(tmp_path) is None
    (tmp_path / "cpu.max").unlink()
    (tmp_path / "cpu").mkdir()
    (tmp_path / "cpu" / "cpu.cfs_quota_us").write_text("400000")
    (tmp_path / "cpu" / "cpu.cfs_period_us").write_text("100000")
    assert hp._cgroup_cpu_quota(tmp_path) == 4.0
    assert hp._cgroup_cpu_quota(tmp_path / "nope") is None
    assert hp.available_cores() >= 1


def test_the_system_hardware_route_reports_the_box():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.auth import get_current_active_user
    from routers.system import router as system_router
    app = FastAPI()
    app.include_router(system_router)
    app.dependency_overrides[get_current_active_user] = lambda: object()
    with TestClient(app) as client:
        r = client.get("/system/hardware")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cores"] >= 1 and "accelerator" in body and isinstance(body["has_gpu"], bool)


def test_the_index_derives_each_apps_bar_from_its_tasks(client):
    r = client.get("/apps/index")
    assert r.status_code == 200
    apps = {a["id"]: a for a in r.json()["apps"]}
    loit = apps["loitering-detection"]
    assert set(loit["hardware"]["tasks"]) == {"object_detection", "multi_object_tracking"}
    assert loit["hardware"]["cores"] == 2 and loit["hardware"]["gpu"] == "recommended"
    assert loit["hardware"]["verdict"] in ("ok", "tight", "short")
    assert isinstance(loit["hardware"]["reasons"], list)
