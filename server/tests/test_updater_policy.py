# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Automatic updates (updater/policy.py, `python -m updater due`).

Installed by itself only in auto mode, for a stable release with no manual
steps and no state migrations, no bigger than the policy allows, inside the
site-local window (one past midnight belongs to the day it starts), and,
unless urgent, after this site's own delay of up to 72 hours.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater import cli, envfile, manifest, source
from updater.policy import MAX_DELAY_H, Policy, due, in_window, site_delay

# 2026-11-01 is a Sunday.
SUN_0300 = datetime(2026, 11, 1, 3, 0)


def man(version="0.1.9", **extra):
    doc = {"schema": 1, "product": "opennvr", "version": version, "channel": "stable", "min_updater": 1,
           "git_commit": "1" * 40, "git_tag": f"v{version}", "alembic_head": "x", "pins": {"CORE_TAG": version},
           "images": [{"ref": f"ghcr.io/open-nvr/core:{version}", "digest": "sha256:" + "a" * 64,
                       "ids": {"linux/amd64": "sha256:" + "b" * 64}}], **extra}
    return manifest.parse(doc)


AUTO = Policy(mode="auto")
LONG_AGO = datetime(2026, 10, 1, tzinfo=UTC)


def is_due(policy=AUTO, candidate=None, installed="0.1.8", local=SUN_0300, utc=None, seen=LONG_AGO, seed="s"):
    return due(policy, candidate or man(), installed, local_now=local,
               utc_now=utc or datetime(2026, 11, 1, 2, 0, tzinfo=UTC), first_seen=seen, seed=seed)


def test_a_patch_in_the_window_after_the_delay_is_due():
    assert is_due() == (True, "due")


@pytest.mark.parametrize("kw, words", [
    ({"policy": Policy(mode="manual")}, "not automatic"),
    ({"policy": Policy(mode="notify")}, "not automatic"),
    ({"candidate": man(channel="edge")}, "only releases"),
    ({"candidate": man(manual_steps=[{"id": "s", "text": "do it"}])}, "steps on the host"),
    ({"candidate": man(state_migrations=True)}, "cannot be rolled back"),
    ({"candidate": man("0.2.0")}, "minor update is installed by hand"),
    ({"candidate": man("1.0.0"), "policy": Policy(mode="auto", auto_scope="minor")}, "major update"),
    ({"local": SUN_0300.replace(hour=5)}, "outside the maintenance window"),
    ({"local": SUN_0300 + timedelta(days=1)}, "outside the maintenance window"),
])
def test_not_due(kw, words):
    ok, why = is_due(**kw)
    assert not ok and words in why, why


def test_minor_updates_when_the_policy_allows_them():
    assert is_due(candidate=man("0.2.0"), policy=Policy(mode="auto", auto_scope="minor"))[0]


def test_windows_including_one_past_midnight():
    sat_night = Policy(mode="auto", window_days=("sat",), window_start="23:00", window_end="02:00")
    assert in_window(sat_night, datetime(2026, 10, 31, 23, 30))      # Saturday evening
    assert in_window(sat_night, datetime(2026, 11, 1, 1, 59))        # its Sunday morning
    assert not in_window(sat_night, datetime(2026, 11, 1, 2, 0))     # ends at 02:00
    assert not in_window(sat_night, datetime(2026, 11, 1, 23, 30))   # Sunday is not a window day
    weekdays = Policy(mode="auto", window_days=("mon", "tue"), window_start="01:00", window_end="03:00")
    assert in_window(weekdays, datetime(2026, 11, 3, 1, 0))          # Tuesday
    assert not in_window(weekdays, datetime(2026, 11, 4, 1, 0))      # Wednesday


def test_each_site_waits_its_own_delay_unless_urgent():
    delays = {site_delay(f"site-{i}", "0.1.9") for i in range(200)}
    assert all(timedelta(0) <= d < timedelta(hours=MAX_DELAY_H) for d in delays)
    assert len(delays) > 150                                         # spread out
    assert site_delay("a", "0.1.9") == site_delay("a", "0.1.9")      # stable for the pair
    seen = datetime(2026, 11, 1, 1, 0, tzinfo=UTC)
    wait = site_delay("s", "0.1.9")
    assert not is_due(seen=seen, utc=seen + wait - timedelta(minutes=1))[0]
    assert is_due(seen=seen, utc=seen + wait)[0]
    assert is_due(seen=seen, utc=seen, candidate=man(urgent=True))[0]
    assert not is_due(seen=None, utc=seen)[0] or wait == timedelta(0)  # first sight starts the clock


def test_policy_from_the_mirrored_file():
    p = Policy.from_env({"MODE": "auto", "AUTO_SCOPE": "minor", "WINDOW_DAYS": "sat,bogus,sun",
                         "WINDOW_START": "01:00", "WINDOW_END": "02:30"})
    assert p == Policy(mode="auto", auto_scope="minor", window_days=("sat", "sun"),
                       window_start="01:00", window_end="02:30")
    assert Policy.from_env({}).mode == "off"


# ── the CLI: due, and what check records for the app ────────────────────

@pytest.fixture
def signed(tmp_path, monkeypatch):
    key = Ed25519PrivateKey.generate()
    keys = tmp_path / "trusted.json"
    keys.write_text(json.dumps({"keys": [manifest.public_key_entry(key, "t")]}))
    monkeypatch.setenv(manifest.EXTRA_KEYS_ENV, str(keys))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    def make(doc):
        raw = json.dumps(doc).encode()
        return raw, manifest.sign(raw, key, "t")
    return make


def test_cli_due(tmp_path, signed):
    work = tmp_path / "work"
    source.save(work / "candidate", *signed(man(urgent=True).raw))
    policy = tmp_path / "policy.env"
    envfile.write_kv(policy, {"MODE": "auto", "WINDOW_DAYS": "sun", "WINDOW_START": "02:00", "WINDOW_END": "04:00"})
    args = ["due", "--work", str(work), "--policy-file", str(policy), "--installed-version", "0.1.8"]
    assert cli.main([*args, "--local-time", "2026-11-01T03:00"]) == 0
    assert cli.main([*args, "--local-time", "2026-11-01T05:00"]) == cli.NOT_DUE
    assert (work / "site-seed").read_text().strip()  # made once, kept
    envfile.write_kv(policy, {"MODE": "manual"})
    assert cli.main([*args, "--local-time", "2026-11-01T03:00"]) == cli.NOT_DUE


def test_check_records_its_outcome_for_the_app(tmp_path, monkeypatch, signed, capsys):
    from tests.test_updater_check import OLD_C, release, run_check

    old, new = signed(release("0.1.8", OLD_C, "aaa")), signed(release("0.1.9", "2" * 40, "bbb"))
    code, work = run_check(tmp_path, monkeypatch, {None: new, "0.1.8": old})
    assert code == 0
    rec = envfile.read_env(work / "check.env")
    assert rec["CODE"] == "0" and int(rec["CHECKED_EPOCH"]) > 0
    assert "0.1.8 -> 0.1.9" in (work / "check.log").read_text()
    seen = envfile.read_env(work / "candidate-seen.env")
    assert seen["VERSION"] == "0.1.9"
    first = seen["FIRST_SEEN"]
    run_check(tmp_path, monkeypatch, {None: new, "0.1.8": old})
    assert envfile.read_env(work / "candidate-seen.env")["FIRST_SEEN"] == first  # not restarted


def test_an_update_needing_steps_is_kept_for_the_app(tmp_path, monkeypatch, signed):
    from tests.test_updater_check import OLD_C, release, run_check

    old = signed(release("0.1.8", OLD_C, "aaa"))
    new = signed(release("0.1.9", "2" * 40, "bbb", manual_steps=[{"id": "s", "text": "Add NATS_APPS_TOKEN"}]))
    code, work = run_check(tmp_path, monkeypatch, {None: new, "0.1.8": old})
    assert code == 21
    assert (work / "candidate" / "release-manifest.json").is_file()
    assert not (work / "plan.env").exists()
    assert "Add NATS_APPS_TOKEN" in (work / "check.log").read_text()
