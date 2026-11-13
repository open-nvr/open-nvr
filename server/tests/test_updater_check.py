# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The updater's decisions (updater/check.py, updater/verify.py, updater/cli.py).

Preflight refuses, before anything changes, a release that is not newer, an
edge build, a skipped minimum, hand-edited files, a site not on the release
it claims (unless adopted), a database newer than the release, a release
without an image for this machine, and one with manual steps nobody did.
Otherwise it plans: pins that the release owns (never one set by hand),
images by digest with the IDs that prove them on either image store, and
whether a rollback must restore the database.

Verification accepts only the release's images, its migration and a
healthy core of that version and commit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater import check, cli, envfile, manifest, revocations, source, verify

CORE = "ghcr.io/open-nvr/core"
OLD_C, NEW_C = "1" * 40, "2" * 40


def sha(c: str) -> str:
    return "sha256:" + c * 64


def release(version, commit, head, *, pins=None, images=None, **extra) -> dict:
    tag = version
    return {
        "schema": 1, "product": "opennvr", "version": version, "channel": "stable",
        "git_commit": commit, "git_tag": f"v{version}", "alembic_head": head, "min_updater": 1,
        "pins": pins or {"CORE_TAG": tag, "ADAPTER_TAG": tag},
        "images": images or [
            {"ref": f"{CORE}:{tag}", "digest": sha("d" if tag == "0.1.9" else "a"),
             "ids": {"linux/amd64": sha("e" if tag == "0.1.9" else "b"), "linux/arm64/v8": sha("f" if tag == "0.1.9" else "c")}},
            {"ref": "postgres:15-alpine", "digest": sha("9"), "ids": {"linux/amd64": sha("8")}},
        ],
        **extra,
    }


OLD = manifest.parse(release("0.1.8", OLD_C, "aaa", alembic_revisions=["aaa"]))
NEW = manifest.parse(release("0.1.9", NEW_C, "bbb", alembic_revisions=["aaa", "bbb"]))


def site(**over) -> check.Site:
    s = {"installed_version": "0.1.8", "installed": OLD, "env": {"CORE_TAG": "0.1.8", "ADAPTER_TAG": "0.1.8"},
         "head": OLD_C, "changed_files": (), "platform": "linux/amd64",
         "site_images": (f"{CORE}:0.1.8", "postgres:15-alpine", "opennvr/footage-search:local-build"),
         "core_image_id": sha("b"), "db_revision": "aaa"}
    s.update(over)
    return check.Site(**s)


# ── refusals: nothing changes ─────────────────────────────────────────

@pytest.mark.parametrize("over, cand, code, words", [
    ({"installed_version": "0.1.9"}, NEW, check.UP_TO_DATE, "not newer"),
    ({"installed_version": "0.2.0"}, NEW, check.UP_TO_DATE, "not newer"),
    ({}, manifest.parse(release("0.1.9", NEW_C, "bbb", channel="edge")), check.REFUSED, "edge build"),
    ({}, manifest.parse(release("0.1.9", NEW_C, "bbb", min_upgrade_from="0.1.9-rc.1")), check.REFUSED, "cannot be installed straight"),
    ({"changed_files": ("docker-compose.yml",)}, NEW, check.REFUSED, "changed by hand"),
    ({"head": "3" * 40}, NEW, check.REFUSED, "not running a release"),
    ({"core_image_id": sha("7")}, NEW, check.REFUSED, "not the 0.1.8 release image"),
    ({"installed": None}, NEW, check.REFUSED, "no signed record"),
    ({"db_revision": "zzz"}, NEW, check.REFUSED, "does not know"),
    ({"platform": "linux/riscv64"}, NEW, check.REFUSED, "no core image for this machine"),
    ({}, manifest.parse(release("0.1.9", NEW_C, "bbb", manual_steps=[{"id": "s", "text": "Add NATS_APPS_TOKEN"}])),
     check.NEEDS_OPERATOR, "needs you"),
])
def test_refusals(over, cand, code, words):
    r = check.decide(site(**over), cand)
    assert isinstance(r, check.Refusal) and r.code == code and words in r.reason, r


def test_changed_files_are_refused_even_when_adopting():
    r = check.decide(site(changed_files=("start.sh",), adopt=True), NEW)
    assert isinstance(r, check.Refusal) and "start.sh" in r.reason


def test_manual_steps_are_shown_and_can_be_accepted():
    cand = manifest.parse(release("0.1.9", NEW_C, "bbb", manual_steps=[{"id": "s", "text": "Add NATS_APPS_TOKEN"}]))
    r = check.decide(site(), cand)
    assert "Add NATS_APPS_TOKEN" in r.advice
    plan = check.decide(site(accept_manual=True), cand)
    assert isinstance(plan, check.Plan) and plan.manual_steps == ("Add NATS_APPS_TOKEN",)


# ── the plan ──────────────────────────────────────────────────────────

def test_a_plain_patch_update():
    plan = check.decide(site(), NEW)
    assert isinstance(plan, check.Plan)
    assert (plan.from_version, plan.to_version, plan.kind) == ("0.1.8", "0.1.9", "patch")
    assert plan.pins == {"CORE_TAG": "0.1.9", "ADAPTER_TAG": "0.1.9"}
    assert plan.from_commit == OLD_C and plan.git_commit == NEW_C
    core = next(i for i in plan.images if i.ref.startswith(CORE))
    assert core.ref == f"{CORE}:0.1.9" and core.accept == (sha("d"), sha("e"))
    assert {i.ref for i in plan.images} == {f"{CORE}:0.1.9", "postgres:15-alpine"}  # local builds untouched
    assert plan.db_restore_on_rollback and plan.rollback == "auto" and not plan.warnings


def test_arm64_matches_the_v8_variant():
    plan = check.decide(site(platform="linux/arm64", core_image_id=sha("c")), NEW)
    assert next(i for i in plan.images if i.ref.startswith(CORE)).accept == (sha("d"), sha("f"))


def test_an_image_loaded_from_a_bundle_is_proved_by_its_platform_manifest():
    img = manifest.Image(ref=f"{CORE}:0.1.9", digest=sha("d"), ids={"linux/arm64/v8": sha("f")},
                         platform_manifests={"linux/arm64/v8": sha("6"), "linux/amd64": sha("7")})
    assert check.accepted_ids(img, "linux/arm64") == (sha("d"), sha("f"), sha("6"))


def test_a_containerd_store_reports_the_registry_digest_as_id():
    plan = check.decide(site(core_image_id=sha("a")), NEW)
    assert isinstance(plan, check.Plan)


def test_a_pin_set_by_hand_is_left_alone():
    plan = check.decide(site(env={"CORE_TAG": "0.1.8", "ADAPTER_TAG": "custom"}), NEW)
    assert plan.pins == {"CORE_TAG": "0.1.9"}
    assert any("ADAPTER_TAG=custom" in w for w in plan.warnings)


def test_a_missing_pin_is_written():
    plan = check.decide(site(env={"CORE_TAG": "0.1.8"}), NEW)
    assert plan.pins == {"CORE_TAG": "0.1.9", "ADAPTER_TAG": "0.1.9"}


def test_an_image_overridden_by_hand_is_left_alone():
    plan = check.decide(site(site_images=(f"{CORE}:0.1.8", "postgres:16-alpine")), NEW)
    assert {i.ref for i in plan.images} == {f"{CORE}:0.1.9"}
    assert any("postgres:16-alpine" in w for w in plan.warnings)


def test_same_migration_head_needs_no_database_restore():
    same = manifest.parse(release("0.1.9", NEW_C, "aaa"))
    assert check.decide(site(), same).db_restore_on_rollback is False


def test_state_migrations_make_rollback_manual():
    cand = manifest.parse(release("0.1.9", NEW_C, "bbb", state_migrations=True))
    plan = check.decide(site(), cand)
    assert plan.rollback == "manual" and any("not rolled back" in w for w in plan.warnings)


def test_adopting_replaces_what_is_there_with_the_release():
    """--adopt promises the release's files and images replace the site's:
    a value that differs from the record is not kept as a hand-set override."""
    plan = check.decide(site(env={"CORE_TAG": "main", "ADAPTER_TAG": "main"},
                             site_images=(f"{CORE}:main", "postgres:16-alpine"), adopt=True), NEW)
    assert plan.pins == {"CORE_TAG": "0.1.9", "ADAPTER_TAG": "0.1.9"}
    assert {i.ref for i in plan.images} == {f"{CORE}:0.1.9", "postgres:15-alpine"}
    assert any("CORE_TAG=main is replaced" in w for w in plan.warnings)


def test_adopting_an_unmanaged_site():
    plan = check.decide(site(head="3" * 40, core_image_id=sha("7"), adopt=True), NEW)
    assert isinstance(plan, check.Plan) and plan.from_commit == "3" * 40
    plan = check.decide(site(installed=None, adopt=True, env={"CORE_TAG": "main"}), NEW)
    assert plan.pins["CORE_TAG"] == "0.1.9" and plan.db_restore_on_rollback
    assert any("Adopted" in w for w in plan.warnings)


def test_older_manifests_without_revisions_skip_that_check():
    cand = manifest.parse(release("0.1.9", NEW_C, "bbb"))
    assert isinstance(check.decide(site(db_revision="zzz"), cand), check.Plan)


# ── verification ──────────────────────────────────────────────────────

def running(core_id=sha("e"), ref=f"{CORE}:0.1.9"):
    return [verify.Running("opennvr_core", core_id, ref), verify.Running("opennvr_db", sha("8"), "postgres:15-alpine"),
            verify.Running("opennvr_app", sha("5"), "opennvr/footage-search:local-build")]


def test_verify_passes_on_the_release():
    ok = {"version": "0.1.9", "commit": NEW_C}
    assert verify.problems(NEW, "linux/amd64", ok, "bbb", running()) == []
    assert verify.problems(NEW, "linux/amd64", {"version": "0.1.9", "commit": None}, "bbb", running(sha("d"))) == []


@pytest.mark.parametrize("health, rev, run, words", [
    (None, "bbb", running(), "not answering"),
    ({"version": "0.1.8", "commit": NEW_C}, "bbb", running(), "reports version 0.1.8"),
    ({"version": "0.1.9", "commit": "9" * 40}, "bbb", running(), "built from"),
    ({"version": "0.1.9", "commit": NEW_C}, "aaa", running(), "migration aaa"),
    ({"version": "0.1.9", "commit": NEW_C}, "bbb", running(sha("7")), "not the release's"),
    ({"version": "0.1.9", "commit": NEW_C}, "bbb", running()[1:], "core container is not running"),
])
def test_verify_finds(health, rev, run, words):
    found = verify.problems(NEW, "linux/amd64", health, rev, run)
    assert any(words in p for p in found), found


def test_verify_waits_for_core_then_gives_up():
    t = [0.0]
    answers = iter([None, None, {"version": "0.1.9", "commit": NEW_C}])
    assert verify.wait_until_healthy(NEW, "linux/amd64", running(), None, timeout=60,
                                     health=lambda: next(answers), revision=lambda _: "bbb",
                                     now=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s)) == []
    t[0] = 0.0
    found = verify.wait_until_healthy(NEW, "linux/amd64", running(), None, timeout=12,
                                      health=lambda: None, revision=lambda _: "bbb",
                                      now=lambda: t[0], sleep=lambda s: t.__setitem__(0, t[0] + s))
    assert found and t[0] >= 12


def test_read_running():
    assert verify.read_running("/opennvr_core sha256:x ghcr.io/open-nvr/core:0.1.9\n\nbad line\n") == [
        verify.Running("opennvr_core", "sha256:x", "ghcr.io/open-nvr/core:0.1.9")]


# ── files and sources ─────────────────────────────────────────────────

def test_env_is_read_like_compose(tmp_path):
    f = tmp_path / ".env"
    f.write_text('# c\nCORE_TAG="0.1.8"\nADAPTER_TAG=\'x\'\n\nA=1\nA=2\nURL=http://h:1/p?q=1\n')
    assert envfile.read_env(f) == {"CORE_TAG": "0.1.8", "ADAPTER_TAG": "x", "A": "2", "URL": "http://h:1/p?q=1"}
    assert envfile.read_env(tmp_path / "missing") == {}


def test_kv_files_refuse_anything_a_shell_could_misread(tmp_path):
    envfile.write_kv(tmp_path / "ok", {"CORE_TAG": "0.1.9", "IMG": "ghcr.io/x@sha256:ab"})
    assert (tmp_path / "ok").read_text() == "CORE_TAG=0.1.9\nIMG=ghcr.io/x@sha256:ab\n"
    for bad in ({"X": "a b"}, {"X": "$(id)"}, {"X": "a;b"}, {"lower": "1"}):
        with pytest.raises(ValueError):
            envfile.write_kv(tmp_path / "bad", bad)


def test_source_urls():
    gh = "https://github.com/open-nvr/open-nvr/releases"
    assert source.urls(None)[0] == f"{gh}/latest/download/release-manifest.json"
    assert source.urls("0.1.9")[1] == f"{gh}/download/v0.1.9/release-manifest.json.sig"
    assert source.urls("0.1.9", "https://mirror.lan/onvr/")[0] == "https://mirror.lan/onvr/v0.1.9/release-manifest.json"
    assert source.urls(None, "https://mirror.lan/onvr")[0] == "https://mirror.lan/onvr/latest/release-manifest.json"


# ── the CLI, end to end on files ──────────────────────────────────────

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


def run_check(tmp_path, monkeypatch, published: dict, *extra):
    work, repo = tmp_path / "work", tmp_path / "repo"
    work.mkdir(exist_ok=True)
    repo.mkdir(exist_ok=True)
    (repo / ".env").write_text("CORE_TAG=0.1.8\nADAPTER_TAG=0.1.8\nSECRET=abc\n")
    (work / "images.in").write_text(f"{CORE}:0.1.8\npostgres:15-alpine\n")

    def fetch(version, base=None, channel="stable"):
        if version not in published:
            raise manifest.ManifestError("nope")
        return published[version]
    monkeypatch.setattr(source, "fetch", fetch)
    # Never the network: the withdrawn list is whatever the test publishes.
    monkeypatch.setattr(revocations, "fetch", lambda base=None: published.get("revoked", {}))
    return cli.main(["check", "--work", str(work), "--repo", str(repo), "--platform", "linux/amd64",
                     "--head", OLD_C, "--site-images", str(work / "images.in"),
                     "--core-image-id", sha("b"), "--installed-version", "0.1.8", *extra]), work


def test_cli_check_writes_a_plan(tmp_path, monkeypatch, signed, capsys):
    old, new = signed(release("0.1.8", OLD_C, "aaa")), signed(release("0.1.9", NEW_C, "bbb"))
    code, work = run_check(tmp_path, monkeypatch, {None: new, "0.1.8": old})
    assert code == check.READY, capsys.readouterr()
    assert envfile.read_env(work / "plan.env")["TO_VERSION"] == "0.1.9"
    assert envfile.read_env(work / "pins.env") == {"CORE_TAG": "0.1.9", "ADAPTER_TAG": "0.1.9"}
    assert (work / "images.txt").read_text().splitlines()[0].startswith(f"{CORE}:0.1.9 {sha('d')} {sha('d')},{sha('e')}")
    assert (work / "installed" / "release-manifest.json").is_file()  # first use: fetched and kept
    assert (work / "candidate" / "release-manifest.json").read_bytes() == new[0]


def test_cli_check_refuses_a_forged_manifest(tmp_path, monkeypatch, signed, capsys):
    old = signed(release("0.1.8", OLD_C, "aaa"))
    raw, sig = signed(release("0.1.9", NEW_C, "bbb"))
    code, work = run_check(tmp_path, monkeypatch, {None: (raw.replace(b"0.1.9", b"0.1.7"), sig), "0.1.8": old})
    assert code == 1 and "changed or corrupted" in capsys.readouterr().err
    assert not (work / "plan.env").exists()


def test_cli_check_without_a_record_of_the_installed_release(tmp_path, monkeypatch, signed, capsys):
    new = signed(release("0.1.9", NEW_C, "bbb"))
    code, _ = run_check(tmp_path, monkeypatch, {None: new})
    assert code == check.REFUSED and "--adopt" in capsys.readouterr().out
    code, _ = run_check(tmp_path, monkeypatch, {None: new}, "--adopt")
    assert code == check.READY


def test_cli_check_from_a_bundle_needs_no_network(tmp_path, monkeypatch, signed):
    bundle = tmp_path / "bundle"
    source.save(bundle, *signed(release("0.1.9", NEW_C, "bbb")))
    source.save(bundle / "history" / "v0.1.8", *signed(release("0.1.8", OLD_C, "aaa")))
    code, work = run_check(tmp_path, monkeypatch, {}, "--bundle", str(bundle))
    assert code == check.READY
    assert envfile.read_env(work / "plan.env")["FROM_VERSION"] == "0.1.8"


def test_cli_verify(tmp_path, monkeypatch, signed):
    d = tmp_path / "m"
    source.save(d, *signed(release("0.1.9", NEW_C, "bbb")))
    run = tmp_path / "running.txt"
    run.write_text(f"/opennvr_core {sha('e')} {CORE}:0.1.9\n")
    monkeypatch.setattr(verify, "get_health", lambda url=None: {"version": "0.1.9", "commit": NEW_C})
    monkeypatch.setattr(verify, "db_revision", lambda url: "bbb")
    args = ["verify", "--manifest-dir", str(d), "--running", str(run), "--platform", "linux/amd64", "--timeout", "0"]
    assert cli.main(args) == 0
    monkeypatch.setattr(verify, "db_revision", lambda url: "aaa")
    assert cli.main(args) == cli.FAILED_VERIFY


def test_installed_version_reads_main_py():
    import main

    assert cli.installed_version() == main.__version__


def test_keygen_makes_a_working_pair(capsys):
    assert cli.main(["keygen", "--id", "k1"]) == 0
    out = capsys.readouterr().out.splitlines()
    seed = out[1].strip()
    entry = json.loads(out[3].strip())
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K
    k = K.from_private_bytes(base64.b64decode(seed))
    raw = b"{}"
    assert manifest.verify.__name__  # module loaded
    pub = manifest._load_key_file
    p = Path(pytest.importorskip("tempfile").mkdtemp()) / "k.json"
    p.write_text(json.dumps({"keys": [entry]}))
    pub(p)["k1"].verify(k.sign(raw), raw)
