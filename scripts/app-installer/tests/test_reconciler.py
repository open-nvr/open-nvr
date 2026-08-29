# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Unit tests for the app-installer reconciler.

The docker/subprocess call is injected as a fake ``runner`` and the DB
as a fake ``store`` — no real Docker, no real database is ever touched.

Run with:

    cd scripts/app-installer && python -m pytest tests/ -v

Coverage:

* the TRUST BOUNDARY (review HIGH-1): an intent id must be kebab-case
  and, for installs, present in the installer's baked curated index —
  and the image/digest that deploy come from the INDEX, with the DB
  row's copies ignored. A compromised web app writing arbitrary rows
  can only ever select curated apps;
* a pending "installed" intent → ``compose up`` is called with the right
  argv and the status flips to ``applied``;
* a failed run (non-zero exit) → status ``failed`` + stderr in message;
* a pending "absent" intent → ``compose down`` (rm) is called and status
  flips to ``applied`` — including for an id de-listed from the index;
* an unpinned index entry (no digest) → a loud "UNPINNED — dev only"
  warning is logged (and it still deploys);
* a pinned entry → the digest-pinned ref is computed as image@sha256:...;
* ``reconcile_once`` skips already-applied rows, honors the caller's
  failure-backoff ``skip_ids``, and CAS-guards the status write on the
  ``desired`` value it acted on (review MED-2: the lost-uninstall race).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Import the reconciler module directly (installer is standalone).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reconciler import (  # noqa: E402
    CuratedApp,
    Intent,
    RunResult,
    build_down_argv,
    build_up_argv,
    image_env_key,
    load_curated_index,
    pinned_image_ref,
    reconcile_intent,
    reconcile_once,
)

# The test trust anchor — what the baked apps_index.yml provides in prod.
TEST_INDEX: dict[str, CuratedApp] = {
    "loitering-detection": CuratedApp(
        id="loitering-detection",
        image="ghcr.io/open-nvr/loitering-detection:latest",
        image_digest="sha256:" + "a" * 64,
    ),
    "occupancy-counting": CuratedApp(
        id="occupancy-counting",
        image="ghcr.io/open-nvr/occupancy-counting:latest",
        image_digest=None,  # unpinned entry — exercises the dev-only path
    ),
    "license-plate-recognition": CuratedApp(
        id="license-plate-recognition",
        image="ghcr.io/open-nvr/license-plate-recognition:latest",
        image_digest="sha256:" + "d" * 64,
        requires_adapters=("fast_plate_ocr",),
    ),
}


class FakeRunner:
    """Records every (argv, env) it's called with and returns a canned
    result. ``envs`` is parallel to ``calls`` so a test can assert the
    per-service image-override env reached the runner (or that none did)."""

    def __init__(self, result: RunResult):
        self.result = result
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []

    def __call__(
        self, argv: list[str], env: dict[str, str] | None = None
    ) -> RunResult:
        self.calls.append(argv)
        self.envs.append(env)
        return self.result


class FakeStore:
    """In-memory desired-state store. ``updates`` records the guarded
    ``desired`` value alongside status/message, mirroring the production
    CAS write (``UPDATE ... WHERE desired=?``)."""

    def __init__(self, intents: list[Intent]):
        self._intents = intents
        self.updates: list[tuple[str, str, str, str]] = []

    def list_intents(self) -> list[Intent]:
        return list(self._intents)

    def set_status(
        self, intent_id: str, status: str, message: str, *, desired: str
    ) -> None:
        self.updates.append((intent_id, status, message, desired))


def _intent(**kw) -> Intent:
    base = dict(
        id="loitering-detection",
        image="ghcr.io/open-nvr/loitering-detection:latest",
        image_digest="sha256:" + "a" * 64,
        desired="installed",
        status="pending",
    )
    base.update(kw)
    return Intent(**base)


def _reconcile(intent, runner, **kw):
    kw.setdefault("index", TEST_INDEX)
    return reconcile_intent(intent, runner, **kw)


# ── THE trust boundary (review HIGH-1) ────────────────────────────────


def test_intent_not_in_index_is_refused_without_touching_docker():
    """A compromised web app can write any row; an id outside the baked
    curated index must be refused before any docker call."""
    runner = FakeRunner(RunResult(returncode=0))
    intent = _intent(id="evil-app", image="attacker/evil:latest")

    status_, message = _reconcile(intent, runner)

    assert status_ == "failed"
    assert "not in the installer's curated index" in message
    assert runner.calls == []  # docker never touched


def test_non_kebab_id_is_refused_without_touching_docker():
    """Defense against argv flag injection: a leading-dash or otherwise
    non-kebab id never reaches the compose argv."""
    runner = FakeRunner(RunResult(returncode=0))
    for bad in ("--remove-orphans", "Evil_App", "a b", "", "app;rm"):
        status_, message = _reconcile(_intent(id=bad), runner)
        assert status_ == "failed", bad
        assert "kebab-case" in message
    assert runner.calls == []


def test_db_supplied_image_is_ignored_in_favor_of_index(caplog):
    """THE HIGH-1 regression: the row claims an attacker image + digest;
    the deploy env must carry the INDEX's image@digest instead."""
    runner = FakeRunner(RunResult(returncode=0, stdout="Started"))
    intent = _intent(
        image="attacker/evil:latest",
        image_digest="sha256:" + "e" * 64,  # attacker "pins" their own image
    )

    with caplog.at_level(logging.WARNING, logger="opennvr.app-installer"):
        status_, _ = _reconcile(intent, runner)

    assert status_ == "applied"
    # The pin env is built from the CURATED entry, not the row.
    assert runner.envs[0] == {
        "LOITERING_DETECTION_IMAGE": (
            "ghcr.io/open-nvr/loitering-detection@sha256:" + "a" * 64
        )
    }
    assert any("IGNORING the row's copy" in r.message for r in caplog.records)


def test_absent_works_for_delisted_id():
    """Teardown must still work for an app removed from the index —
    otherwise a de-listed app becomes uninstallable."""
    runner = FakeRunner(RunResult(returncode=0, stdout="Removed"))
    intent = _intent(id="some-old-app", desired="absent")

    status_, _ = _reconcile(intent, runner)

    assert status_ == "applied"
    assert runner.calls[0][-4:] == ["rm", "-s", "-f", "some-old-app"]


def test_load_curated_index_parses_the_shipped_file():
    """The real baked artifact must load and contain installable apps."""
    repo_root = Path(__file__).resolve().parents[3]
    index = load_curated_index(repo_root / "server" / "config" / "apps_index.yml")
    assert index, "shipped index is empty"
    for app in index.values():
        assert app.id and app.image


def test_load_curated_index_fails_closed_on_garbage(tmp_path):
    bad = tmp_path / "index.yml"
    bad.write_text("just: a mapping, not a list\n")
    with pytest.raises(ValueError, match="top-level list"):
        load_curated_index(bad)


# ── installed → compose up + applied ──────────────────────────────────


def test_pending_installed_calls_compose_up_and_applies():
    runner = FakeRunner(RunResult(returncode=0, stdout="Started"))
    intent = _intent()

    status_, message = _reconcile(intent, runner)

    assert status_ == "applied"
    assert message == "Started"
    # Exactly one compose call, with the right argv.
    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[:2] == ["docker", "compose"]
    assert "-f" in argv and "docker-compose.apps.yml" in argv
    assert argv[-3:] == ["up", "-d", "loitering-detection"]
    assert argv == build_up_argv(intent)
    # A digest-bearing index entry → the runner is handed the pinned
    # image override env so compose actually deploys the pinned ref.
    assert runner.envs[0] == {
        "LOITERING_DETECTION_IMAGE": pinned_image_ref(intent)
    }


def test_digest_bearing_entry_passes_pinned_ref_env():
    """The pin must REACH the runner (finding: it was dead code before)."""
    runner = FakeRunner(RunResult(returncode=0, stdout="Started"))
    intent = _intent(
        id="license-plate-recognition",
        image="ghcr.io/open-nvr/license-plate-recognition:latest",
        image_digest="sha256:" + "d" * 64,
    )

    _reconcile(intent, runner)

    assert runner.envs[0] == {
        "LICENSE_PLATE_RECOGNITION_IMAGE": (
            "ghcr.io/open-nvr/license-plate-recognition@sha256:" + "d" * 64
        )
    }


# ── failed run → failed + stderr message ──────────────────────────────


def test_failed_run_records_failed_with_stderr():
    runner = FakeRunner(
        RunResult(returncode=1, stderr="no such service: bogus")
    )
    intent = _intent()

    status_, message = _reconcile(intent, runner)

    assert status_ == "failed"
    assert "no such service" in message


# ── absent → compose down (rm) + applied ──────────────────────────────


def test_absent_calls_compose_down_and_applies():
    runner = FakeRunner(RunResult(returncode=0, stdout="Removed"))
    intent = _intent(desired="absent")

    status_, message = _reconcile(intent, runner)

    assert status_ == "applied"
    argv = runner.calls[0]
    assert argv[-4:] == ["rm", "-s", "-f", "loitering-detection"]
    assert argv == build_down_argv(intent)
    assert runner.envs[0] is None  # teardown passes no image override


# ── unpinned index entry → loud warning ───────────────────────────────


def test_unpinned_entry_logs_dev_only_warning(caplog):
    runner = FakeRunner(RunResult(returncode=0, stdout="Started"))
    # occupancy-counting's INDEX entry is unpinned (image_digest=None).
    intent = _intent(
        id="occupancy-counting",
        image="ghcr.io/open-nvr/occupancy-counting:latest",
        image_digest=None,
    )

    with caplog.at_level(logging.WARNING, logger="opennvr.app-installer"):
        status_, _ = _reconcile(intent, runner)

    assert status_ == "applied"  # unpinned still deploys (dev)
    assert any(
        "UNPINNED" in rec.message and "dev only" in rec.message
        for rec in caplog.records
    )
    # A digest-less entry passes NO pin override — compose falls back to
    # the service's :local-build default.
    assert runner.envs[0] is None


def test_image_env_key_transform():
    """The app-id → ENV transform is the single shared contract between
    the reconciler and docker-compose.apps.yml."""
    assert image_env_key("license-plate-recognition") == (
        "LICENSE_PLATE_RECOGNITION_IMAGE"
    )
    assert image_env_key("loitering-detection") == "LOITERING_DETECTION_IMAGE"
    assert image_env_key("occupancy-counting") == "OCCUPANCY_COUNTING_IMAGE"


def test_pinned_image_ref_shapes():
    # sha256-prefixed digest, tagged image → tag stripped, digest appended.
    intent = _intent(
        image="ghcr.io/open-nvr/loitering-detection:latest",
        image_digest="sha256:" + "b" * 64,
    )
    assert pinned_image_ref(intent) == (
        "ghcr.io/open-nvr/loitering-detection@sha256:" + "b" * 64
    )
    # bare digest (no sha256: prefix) is normalised.
    intent2 = _intent(image="ghcr.io/open-nvr/x:1.0", image_digest="c" * 64)
    assert pinned_image_ref(intent2) == "ghcr.io/open-nvr/x@sha256:" + "c" * 64
    # no digest → None (unpinned).
    assert pinned_image_ref(_intent(image_digest=None)) is None


# ── reconcile_once sweep semantics ────────────────────────────────────


def test_reconcile_once_skips_applied_processes_pending():
    runner = FakeRunner(RunResult(returncode=0, stdout="ok"))
    store = FakeStore(
        [
            _intent(id="occupancy-counting", status="applied"),
            _intent(id="loitering-detection", status="pending"),
        ]
    )

    outcomes = reconcile_once(store, runner, index=TEST_INDEX)

    # Only the pending one was reconciled + written back.
    assert [o[0] for o in outcomes] == ["loitering-detection"]
    assert store.updates == [
        ("loitering-detection", "applied", "ok", "installed")
    ]
    assert len(runner.calls) == 1


def test_reconcile_once_writes_failed_status():
    runner = FakeRunner(RunResult(returncode=2, stderr="boom"))
    store = FakeStore([_intent(id="occupancy-counting", status="pending")])

    reconcile_once(store, runner, index=TEST_INDEX)

    assert store.updates[0][0] == "occupancy-counting"
    assert store.updates[0][1] == "failed"
    assert "boom" in store.updates[0][2]


def test_reconcile_once_honors_backoff_skip_ids():
    """A poison intent in backoff is not retried this sweep (review
    MED-3: no more 10s retry storm)."""
    runner = FakeRunner(RunResult(returncode=0, stdout="ok"))
    store = FakeStore(
        [
            _intent(id="loitering-detection", status="failed"),
            _intent(id="occupancy-counting", status="pending"),
        ]
    )

    outcomes = reconcile_once(
        store, runner,
        index=TEST_INDEX, skip_ids=frozenset({"loitering-detection"}),
    )

    assert [o[0] for o in outcomes] == ["occupancy-counting"]
    assert len(runner.calls) == 1


def test_status_write_is_guarded_on_acted_desired():
    """Review MED-2 (the lost-uninstall race): the write-back must carry
    the desired value the sweep ACTED ON, so the production CAS
    (``UPDATE ... WHERE desired=?``) can no-op when the operator flipped
    the desired state mid-reconcile."""
    runner = FakeRunner(RunResult(returncode=0, stdout="ok"))
    store = FakeStore([_intent(desired="absent", status="pending")])

    reconcile_once(store, runner, index=TEST_INDEX)

    (intent_id, status_, _msg, desired) = store.updates[0]
    assert (intent_id, status_, desired) == (
        "loitering-detection", "applied", "absent"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── RFC-0002 Phase 3: refcounted adapter provisioning (decision 7) ──


def _lpr_intent(**kw) -> Intent:
    base = dict(
        id="license-plate-recognition",
        image="ghcr.io/open-nvr/license-plate-recognition:latest",
        image_digest="sha256:" + "d" * 64,
        desired="installed",
        status="pending",
    )
    base.update(kw)
    return Intent(**base)


def test_install_ups_required_adapters_ahead_of_the_app():
    runner = FakeRunner(RunResult(0, "ok", ""))
    status, _ = _reconcile(_lpr_intent(), runner)
    assert status == "applied"
    argv = runner.calls[0]
    up_at = argv.index("-d")
    services = argv[up_at + 1:]
    # The adapter service is upped in the same command, BEFORE the app —
    # an already-running adapter is a compose no-op (reuse), a missing
    # service block fails the whole install loudly.
    assert services == ["fast-plate-ocr-adapter", "license-plate-recognition"]


def test_uninstall_releases_the_last_holders_adapter():
    runner = FakeRunner(RunResult(0, "ok", ""))
    status, _ = _reconcile(
        _lpr_intent(desired="absent"), runner, held_adapters=frozenset()
    )
    assert status == "applied"
    argv = runner.calls[0]
    rm_at = argv.index("rm")
    assert argv[rm_at + 1:] == [
        "-s", "-f", "license-plate-recognition", "fast-plate-ocr-adapter"]


def test_uninstall_never_removes_an_adapter_another_app_holds():
    runner = FakeRunner(RunResult(0, "ok", ""))
    _reconcile(
        _lpr_intent(desired="absent"), runner,
        held_adapters=frozenset({"fast_plate_ocr"}),
    )
    argv = runner.calls[0]
    assert "fast-plate-ocr-adapter" not in argv


def test_delisted_app_releases_no_adapters():
    # An id no longer in the curated index: its requirements are unknown,
    # so teardown removes ONLY the app service — never guess an adapter.
    runner = FakeRunner(RunResult(0, "ok", ""))
    status, _ = _reconcile(
        _intent(id="ghost-app", desired="absent"), runner,
    )
    assert status == "applied"
    assert [a for a in runner.calls[0] if a.endswith("-adapter")] == []


def test_sweep_computes_held_from_other_installed_intents():
    # LPR uninstalling while a second (hypothetical) app that also
    # requires fast_plate_ocr stays installed -> the adapter survives.
    index = dict(TEST_INDEX)
    index["parking-audit"] = CuratedApp(
        id="parking-audit",
        image="ghcr.io/open-nvr/parking-audit:latest",
        image_digest="sha256:" + "e" * 64,
        requires_adapters=("fast_plate_ocr",),
    )
    intents = [
        _lpr_intent(desired="absent"),
        _intent(id="parking-audit",
                image="ghcr.io/open-nvr/parking-audit:latest",
                image_digest="sha256:" + "e" * 64,
                desired="installed", status="applied"),
    ]
    runner = FakeRunner(RunResult(0, "ok", ""))
    store = FakeStore(intents)
    outcomes = reconcile_once(store, runner, index=index)
    assert [o[0] for o in outcomes] == ["license-plate-recognition"]
    assert "fast-plate-ocr-adapter" not in runner.calls[0]


def test_shipped_index_carries_lpr_adapter_requirement():
    repo_root = Path(__file__).resolve().parents[3]
    index = load_curated_index(
        repo_root / "server" / "config" / "apps_index.yml")
    assert index["license-plate-recognition"].requires_adapters == (
        "fast_plate_ocr",)
    assert index["smart-doorbell"].requires_adapters == ("insightface",)
