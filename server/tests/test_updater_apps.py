# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""App updates from the registry (updater/apps.py, the app half of
updater/policy.py, and ``python -m updater apps-*``).

An app with ``updates: registry`` is released by a ``X.Y.Z`` tag in its own
registry repository; nothing in this repository changes. The site finds the
newest release of each step size it could take, skips what it can't run,
and installs one only when the host's cosign check passed and the policy
says so.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from updater import apps, cli, envfile
from updater.policy import APP_MAX_DELAY_H, Policy, app_delay, choose_offer, due_app

REPO = "ghcr.io/open-nvr/attendly"
IDENTITY = r"^https://github\.com/open-nvr/opennvr-pro-apps/\.github/workflows/publish-images\.yml@refs/tags/v.*$"
SRC = apps.AppSource(id="attendly", repository=REPO, signer_identity=IDENTITY)
PLATFORM = "linux/amd64"


def dg(seed: str) -> str:
    return "sha256:" + hashlib.sha256(seed.encode()).hexdigest()


class FakeRegistry:
    """tags → releases; ``labels`` per tag; ``no_build`` tags have none for
    this platform; ``fail`` raises on every call."""

    def __init__(self, tags, labels=None, no_build=(), fail=None):
        self._tags, self._labels, self._no_build, self._fail = list(tags), labels or {}, set(no_build), fail
        self.asked: list[str] = []

    def tags(self, repository):
        if self._fail:
            raise apps.RegistryError(self._fail)
        return self._tags

    def release(self, repository, tag, platform):
        self.asked.append(tag)
        if tag in self._no_build:
            return None
        return apps.Release(digest=dg(tag), labels=self._labels.get(tag, {}))


def running(ref=f"{REPO}:1.2.0", label="1.2.0"):
    return apps.Running(service="attendly", ref=ref, version_label=label)


def check(registry, run=None, core="0.1.12", revoked=None):
    return apps.check_app(SRC, run if run is not None else running(), registry=registry, platform=PLATFORM,
                          core_version=core, revoked=revoked)


# ── the catalog ──────────────────────────────────────────────────────


def test_only_entries_that_ask_and_name_a_signer_update_from_the_registry():
    doc = [
        {"id": "attendly", "image": f"{REPO}:1.2.0", "updates": "registry", "signing": {"identity": IDENTITY}},
        {"id": "line-crossing", "image": "ghcr.io/open-nvr/line-crossing:latest"},  # not asked
        {"id": "no-signer", "image": "ghcr.io/open-nvr/x:1.0.0", "updates": "registry"},
        {"id": "bad-regexp", "image": "ghcr.io/open-nvr/y:1.0.0", "updates": "registry",
         "signing": {"identity": "("}},
        {"id": "plain-http", "image": "ghcr.io/open-nvr/z:1.0.0", "updates": "registry",
         "signing": {"identity": "x", "issuer": "http://example"}},
        {"id": "Not_Kebab", "image": "ghcr.io/open-nvr/w:1.0.0", "updates": "registry",
         "signing": {"identity": "x"}},
    ]
    found, problems = apps.sources(doc)
    assert found == [SRC]
    assert SRC.image_key == "ATTENDLY_IMAGE"
    assert len(problems) == 4
    assert any("no-signer" in p and "signing.identity" in p for p in problems)
    assert apps.sources({"not": "a list"}) == ([], [])


def test_the_shipped_catalog_reads():
    import re

    found, problems = apps.load_sources()
    assert problems == []
    [attendly] = [s for s in found if s.id == "attendly"]
    assert attendly.repository == REPO and attendly.image_key == "ATTENDLY_IMAGE"
    # Its own release workflow on a v* tag; never a main build, nor another repository.
    ok = "https://github.com/open-nvr/opennvr-pro-apps/.github/workflows/publish-images.yml@refs/tags/v1.2.0"
    assert re.match(attendly.signer_identity, ok)
    for bad in (ok.replace("refs/tags/v1.2.0", "refs/heads/main"),
                ok.replace("opennvr-pro-apps", "open-nvr"),
                ok.replace("publish-images.yml", "other.yml")):
        assert not re.match(attendly.signer_identity, bad), bad


@pytest.mark.parametrize("ref, parts", [
    (f"{REPO}:1.2.0", (REPO, "1.2.0", None)),
    (f"{REPO}@{dg('a')}", (REPO, None, dg("a"))),
    (f"{REPO}:1.2.0@{dg('a')}", (REPO, "1.2.0", dg("a"))),
    ("registry.local:5000/open-nvr/attendly", ("registry.local:5000/open-nvr/attendly", None, None)),
    ("registry.local:5000/a:1.0.0", ("registry.local:5000/a", "1.0.0", None)),
])
def test_split_ref(ref, parts):
    assert apps.split_ref(ref) == parts


def test_read_running():
    got = apps.read_running(f"attendly {REPO}@{dg('a')} 1.2.0\nnats nats:2.10\n\nbroken\n")
    assert got["attendly"] == apps.Running("attendly", f"{REPO}@{dg('a')}", "1.2.0")
    assert got["nats"].version_label == ""
    assert "broken" not in got


# ── which release a site is on ───────────────────────────────────────


@pytest.mark.parametrize("run, version", [
    (running(f"{REPO}@{dg('x')}", "1.2.0"), "1.2.0"),     # pinned by digest: the label says
    (running(f"{REPO}:1.1.0", ""), "1.1.0"),               # a release tag, no label
    (running(f"{REPO}:latest", "1.0.3"), "1.0.3"),         # a moving tag, but the label knows
])
def test_installed_version(run, version):
    assert apps.installed_version(SRC, run) == (version, "")


@pytest.mark.parametrize("run, words", [
    (running("opennvr/attendly:local-build", ""), "not an image from"),  # a local build: hands off
    (running(f"{REPO}:latest", "main"), "not a release"),
    (running(f"{REPO}:main", ""), "set ATTENDLY_IMAGE"),
])
def test_anything_but_a_release_is_left_alone(run, words):
    report = check(FakeRegistry(["9.9.9"]), run=run)
    assert report.state == apps.UNMANAGED and words in report.reason
    assert report.offers == []


# ── what it could move to ────────────────────────────────────────────


def test_only_release_tags_count_and_the_newest_of_each_step_is_offered():
    tags = ["main", "latest", "1.2", "sha-0bcc2cc", f"sha256-{'a' * 64}.sig", "1.3.0-rc.1",
            "1.0.0", "1.2.0", "1.2.1", "1.2.3", "1.3.0", "1.4.2", "2.0.0", "2.1.0"]
    report = check(FakeRegistry(tags))
    assert report.state == apps.AVAILABLE
    assert [(o.version, o.kind) for o in report.offers] == [("2.1.0", "major"), ("1.4.2", "minor"),
                                                           ("1.2.3", "patch")]
    o = report.offers[0]
    assert o.digest == dg("2.1.0") and o.ref == f"{REPO}@{dg('2.1.0')}" and o.rollback == "safe"
    assert report.installed_version == "1.2.0" and report.installed_ref == f"{REPO}:1.2.0"


def test_up_to_date_and_not_installed():
    assert check(FakeRegistry(["1.1.0", "1.2.0", "latest"])).state == apps.UP_TO_DATE
    report = apps.check_app(SRC, None, registry=FakeRegistry(["1.3.0"]), platform=PLATFORM, core_version="0.1.12")
    assert report.state == apps.NOT_INSTALLED and report.offers == []


def test_a_release_this_core_cannot_run_falls_back_to_the_one_before():
    labels = {"1.4.0": {apps.LABEL_MIN_CORE: "0.2.0"}, "1.3.1": {apps.LABEL_MIN_CORE: "0.1.12"}}
    report = check(FakeRegistry(["1.3.0", "1.3.1", "1.4.0"], labels=labels))
    assert [o.version for o in report.offers] == ["1.3.1"]
    assert any("1.4.0: needs OpenNVR 0.2.0" in s for s in report.skipped)


def test_blocked_when_nothing_newer_can_run_here():
    report = check(FakeRegistry(["1.3.0"], labels={"1.3.0": {apps.LABEL_MIN_CORE: "1.0.0"}}))
    assert report.state == apps.BLOCKED and report.offers == []
    report = check(FakeRegistry(["1.3.0"], no_build=["1.3.0"]))
    assert report.state == apps.BLOCKED and "no build for linux/amd64" in report.skipped[0]
    report = check(FakeRegistry(["1.3.0"], labels={"1.3.0": {apps.LABEL_MIN_CORE: "soon"}}))
    assert report.state == apps.BLOCKED and "unreadable" in report.skipped[0]


def test_withdrawn_releases_are_skipped_and_a_withdrawn_install_is_flagged():
    report = check(FakeRegistry(["1.2.1", "1.2.2"]), revoked={"1.2.2": "eats data", "1.2.0": "bad"})
    assert [o.version for o in report.offers] == ["1.2.1"]
    assert report.installed_revoked == "bad"


def test_the_rollback_label():
    report = check(FakeRegistry(["1.3.0"], labels={"1.3.0": {apps.LABEL_ROLLBACK: "manual"}}))
    assert report.offers[0].rollback == "manual"


def test_a_registry_problem_is_reported_not_raised():
    report = check(FakeRegistry([], fail="could not reach ghcr.io"))
    assert report.state == apps.ERROR and "could not reach" in report.reason


def test_a_report_survives_the_round_trip():
    report = check(FakeRegistry(["1.3.0"]))
    assert apps.AppReport.from_dict(json.loads(json.dumps(report.to_dict()))) == report


# ── the registry over HTTP ───────────────────────────────────────────


def _blob(doc) -> tuple[bytes, str]:
    raw = json.dumps(doc).encode()
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def fake_ghcr(*, labels=None, tamper_config=False, single=None):
    """A GHCR-shaped registry: a 401 challenge, a token realm, two pages of
    tags, a multi-platform index with attestation entries, and a CDN host the
    config blob redirects to."""
    config, config_digest = _blob({"os": "linux", "architecture": "amd64",
                                   "config": {"Labels": labels or {apps.LABEL_VERSION: "1.3.0"}}})
    arm_config, arm_digest = _blob({"os": "linux", "architecture": "arm64", "config": {"Labels": {}}})
    image, image_digest = _blob({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                 "config": {"digest": config_digest}})
    arm_image, arm_image_digest = _blob({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                         "config": {"digest": arm_digest}})
    index, index_digest = _blob({"mediaType": "application/vnd.oci.image.index.v1+json", "manifests": [
        {"digest": dg("att"), "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": arm_image_digest, "platform": {"os": "linux", "architecture": "arm64"}},
        {"digest": image_digest, "platform": {"os": "linux", "architecture": "amd64"}},
    ]})
    if single == "amd64":
        index, index_digest = image, image_digest
    elif single == "arm64":
        index, index_digest = arm_image, arm_image_digest
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if request.url.host == "ghcr.io" and request.url.path == "/token":
            assert request.url.params["scope"] == "repository:open-nvr/attendly:pull"
            return httpx.Response(200, json={"token": "T"})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=b"tampered" if tamper_config else config)
        if request.headers.get("authorization") != "Bearer T":
            return httpx.Response(401, headers={"www-authenticate":
                                                'Bearer realm="https://ghcr.io/token",service="ghcr.io",'
                                                'scope="repository:open-nvr/attendly:pull"'})
        path = request.url.path
        if path == "/v2/open-nvr/attendly/tags/list":
            if request.url.params.get("last"):
                return httpx.Response(200, json={"tags": ["1.3.0", "latest"]})
            return httpx.Response(200, json={"tags": ["1.2.0"]},
                                  headers={"link": '</v2/open-nvr/attendly/tags/list?last=1.2.0&n=1000>; rel="next"'})
        if path == "/v2/open-nvr/attendly/manifests/1.3.0":
            return httpx.Response(200, content=index, headers={"docker-content-digest": dg("lies")})
        if path == f"/v2/open-nvr/attendly/manifests/{image_digest}":
            return httpx.Response(200, content=image)
        if path == f"/v2/open-nvr/attendly/manifests/{arm_image_digest}":
            return httpx.Response(200, content=arm_image)
        if path == f"/v2/open-nvr/attendly/blobs/{config_digest}":
            return httpx.Response(307, headers={"location": "https://cdn.example/blob"})
        if path == f"/v2/open-nvr/attendly/blobs/{arm_digest}":
            return httpx.Response(200, content=arm_config)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return apps.Registry(client=client), index_digest, seen


def test_registry_tags_follow_the_challenge_and_every_page():
    reg, _, seen = fake_ghcr()
    assert reg.tags(REPO) == ["1.2.0", "1.3.0", "latest"]
    assert sum("/token" in u for u in seen) == 1  # one token, reused


def test_registry_release_pins_the_index_by_its_own_bytes_and_reads_this_platforms_labels():
    reg, index_digest, _ = fake_ghcr(labels={apps.LABEL_MIN_CORE: "0.1.12"})
    rel = reg.release(REPO, "1.3.0", "linux/amd64")
    assert rel.digest == index_digest  # computed, not the header's dg("lies")
    assert rel.labels == {apps.LABEL_MIN_CORE: "0.1.12"}
    assert reg.release(REPO, "1.3.0", "linux/riscv64") is None


def test_registry_release_of_a_single_platform_image():
    reg, digest, _ = fake_ghcr(single="amd64")
    assert reg.release(REPO, "1.3.0", "linux/amd64").digest == digest
    reg, _, _ = fake_ghcr(single="arm64")
    assert reg.release(REPO, "1.3.0", "linux/amd64") is None


def test_registry_refuses_a_config_that_is_not_what_the_manifest_named():
    reg, _, _ = fake_ghcr(tamper_config=True)
    with pytest.raises(apps.RegistryError, match="does not match"):
        reg.release(REPO, "1.3.0", "linux/amd64")


def test_registry_problems_are_registry_errors():
    def down(request):
        raise httpx.ConnectError("no route")
    reg = apps.Registry(client=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(apps.RegistryError, match=r"could not reach ghcr.io"):
        reg.tags(REPO)
    reg = apps.Registry(client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(401, headers={"www-authenticate": 'Basic realm="x"'}))))
    with pytest.raises(apps.RegistryError, match="login"):
        reg.tags(REPO)


# ── the policy ───────────────────────────────────────────────────────

SUN_0300 = datetime(2026, 11, 1, 3, 0)
LONG_AGO = datetime(2026, 10, 1, tzinfo=UTC)
UTC_NOW = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)
OK = (True, "signature verified")


def offer(version="1.2.1", kind="patch", rollback="safe"):
    return apps.Offer(version=version, kind=kind, digest=dg(version), ref=f"{REPO}@{dg(version)}",
                      rollback=rollback)


def app_due(policy=Policy(), o=None, verified=OK, local=SUN_0300, utc=UTC_NOW, seen=LONG_AGO, now=False):
    return due_app(policy, "attendly", o or offer(), verified=verified, local_now=local, utc_now=utc,
                   first_seen=seen, seed="s", now=now)


def test_app_modes_from_the_policy_file():
    p = Policy.from_env({"MODE": "manual", "APPS_MODE": "auto", "APP_ATTENDLY_MODE": "notify",
                         "APP_GATE_WATCH_MODE": "off", "APP_X_MODE": "bogus", "APPS_AUTO_SCOPE": "patch"})
    assert p.app_mode("attendly") == "notify"
    assert p.app_mode("gate-watch") == "off"
    assert p.app_mode("other") == "auto"          # the apps mode
    assert p.app_scope() == "patch"
    q = Policy.from_env({"MODE": "manual", "AUTO_SCOPE": "patch"})
    assert q.app_mode("attendly") == "manual" and q.app_scope() == "patch"  # follows the release policy
    assert Policy.from_env({}) == Policy()


def test_an_app_update_in_the_window_after_the_delay_is_due():
    assert app_due() == (True, "due")


@pytest.mark.parametrize("kw, words", [
    ({"policy": Policy(apps_mode="off")}, "off"),
    ({"policy": Policy(app_modes=(("attendly", "notify"),))}, "not automatic"),
    ({"policy": Policy(mode="manual")}, "not automatic"),
    ({"verified": None}, "not been checked"),
    ({"verified": (False, "no matching signatures")}, "no matching signatures"),
    ({"o": offer(rollback="manual")}, "rolled back"),
    ({"local": datetime(2026, 11, 1, 12, 0)}, "maintenance window"),
])
def test_an_app_update_is_not_due(kw, words):
    ok, why = app_due(**kw)
    assert not ok and words in why, why


def test_update_now_skips_window_and_delay_but_never_the_signature_or_off():
    late = datetime(2026, 11, 1, 12, 0)
    assert app_due(local=late, seen=UTC_NOW, now=True, o=offer(rollback="manual"),
                   policy=Policy(mode="manual")) == (True, "asked for")
    assert not app_due(now=True, verified=None)[0]
    assert not app_due(now=True, policy=Policy(apps_mode="off"))[0]
    assert not app_due(now=True, policy=Policy(apps_mode="notify"))[0]


def test_each_site_waits_its_own_app_delay():
    delays = {app_delay(f"site-{i}", "attendly", "1.3.0") for i in range(200)}
    assert all(timedelta(0) <= d < timedelta(hours=APP_MAX_DELAY_H) for d in delays)
    assert len(delays) > 150
    assert app_delay("s", "attendly", "1.3.0") != app_delay("s", "gate-watch", "1.3.0")
    wait = app_delay("s", "attendly", "1.2.1")
    assert not app_due(seen=UTC_NOW, utc=UTC_NOW + wait - timedelta(minutes=1))[0]
    assert app_due(seen=UTC_NOW, utc=UTC_NOW + wait)[0]


def test_choose_offer_takes_the_biggest_step_the_scope_allows():
    report = check(FakeRegistry(["1.2.3", "1.4.0", "2.0.0"]))
    assert choose_offer(Policy(), report)[0].version == "1.4.0"
    assert choose_offer(Policy(apps_auto_scope="patch"), report)[0].version == "1.2.3"
    assert choose_offer(Policy(), report, now=True)[0].version == "2.0.0"
    only_major = check(FakeRegistry(["2.0.0"]))
    o, why = choose_offer(Policy(), only_major)
    assert o is None and "major update, installed by hand" in why
    report.installed_revoked = "bad"
    assert choose_offer(Policy(), report)[0].version == "2.0.0"  # get off a withdrawn version
    assert choose_offer(Policy(), check(FakeRegistry(["1.2.0"])))[0] is None


# ── the CLI ──────────────────────────────────────────────────────────


@pytest.fixture
def site(tmp_path, monkeypatch):
    index = tmp_path / "apps_index.yml"
    index.write_text(json.dumps([{"id": "attendly", "image": f"{REPO}:1.2.0", "updates": "registry",
                                  "signing": {"identity": IDENTITY}}]))  # JSON is YAML
    run = tmp_path / "running.txt"
    run.write_text(f"attendly {REPO}:1.2.0 1.2.0\n")
    policy = tmp_path / "policy.env"
    envfile.write_kv(policy, {"MODE": "auto", "WINDOW_DAYS": "sun", "WINDOW_START": "02:00", "WINDOW_END": "04:00"})
    registry = FakeRegistry(["1.2.0", "1.2.1", "1.3.0", "latest"])
    monkeypatch.setattr(apps, "Registry", lambda: registry)
    work = tmp_path / "work"
    base = ["--work", str(work)]
    return {"work": work, "policy": policy, "index": index, "registry": registry,
            "check": lambda *x: cli.main(["apps-check", *base, "--index", str(index), "--running", str(run),
                                          "--platform", PLATFORM, "--installed-version", "0.1.12",
                                          "--policy-file", str(policy), *x]),
            "verified": lambda v, r="ok": cli.main(["apps-verified", *base, "--id", "attendly",
                                                    "--digest", dg(v), "--result", r, "--message", "cosign said so"]),
            "due": lambda t="2026-11-01T03:00", *x: cli.main(["apps-due", *base, "--policy-file", str(policy),
                                                              "--local-time", t, *x])}


def test_cli_check_writes_the_report_and_what_cosign_must_check(site):
    assert site["check"]() == 0
    doc = json.loads((site["work"] / "apps.json").read_text())
    [a] = doc["apps"]
    assert a["state"] == apps.AVAILABLE and [o["version"] for o in a["offers"]] == ["1.3.0", "1.2.1"]
    lines = (site["work"] / "apps-verify.txt").read_text().splitlines()
    assert lines == [f"attendly {REPO}@{dg('1.3.0')} {IDENTITY} {apps.GITHUB_OIDC_ISSUER}",
                     f"attendly {REPO}@{dg('1.2.1')} {IDENTITY} {apps.GITHUB_OIDC_ISSUER}"]
    first = json.loads((site["work"] / "apps-seen.json").read_text())
    site["verified"]("1.3.0")
    site["check"]()
    assert json.loads((site["work"] / "apps-seen.json").read_text()) == first  # the clock is not restarted
    assert len((site["work"] / "apps-verify.txt").read_text().splitlines()) == 1  # 1.3.0 is checked


def test_cli_due_needs_the_signature_then_writes_what_to_install(site):
    site["check"]()
    seen = site["work"] / "apps-seen.json"
    seen.write_text(json.dumps(dict.fromkeys(json.loads(seen.read_text()), "2026-10-01T00:00:00Z")))
    assert site["due"]() == cli.NOT_DUE
    assert not (site["work"] / "apps-due.env").exists()
    site["verified"]("1.3.0", "failed")
    assert site["due"]() == cli.NOT_DUE
    site["verified"]("1.3.0")
    assert site["due"]() == 0
    due = envfile.read_env(site["work"] / "apps-due.env")
    assert due == {"APP_ID": "attendly", "IMAGE_KEY": "ATTENDLY_IMAGE", "IMAGE_REF": f"{REPO}@{dg('1.3.0')}",
                   "VERSION": "1.3.0", "FROM_VERSION": "1.2.0", "FROM_REF": f"{REPO}:1.2.0", "ROLLBACK": "safe"}
    assert site["due"]("2026-11-01T12:00") == cli.NOT_DUE  # outside the window
    assert not (site["work"] / "apps-due.env").exists()     # a stale answer is never left behind
    assert site["due"]("2026-11-01T12:00", "--app", "attendly", "--now") == 0
    assert site["due"]("2026-11-01T12:00", "--now") == 1    # Update now names one app
    assert site["due"]("2026-11-01T03:00", "--app", "nope") == cli.NOT_DUE


def test_cli_due_never_retries_a_release_that_was_rolled_back(site):
    site["check"]()
    seen = site["work"] / "apps-seen.json"
    seen.write_text(json.dumps(dict.fromkeys(json.loads(seen.read_text()), "2026-10-01T00:00:00Z")))
    site["verified"]("1.3.0")
    (site["work"] / "apps-failed.txt").write_text("attendly 1.3.0\n")
    assert site["due"]() == cli.NOT_DUE
    assert site["due"]("2026-11-01T03:00", "--app", "attendly", "--now") == 0  # by hand it may


def test_cli_check_skips_an_app_whose_updates_are_off(site, monkeypatch):
    envfile.write_kv(site["policy"], {"MODE": "auto", "APP_ATTENDLY_MODE": "off"})
    monkeypatch.setattr(apps, "Registry", lambda: FakeRegistry([], fail="must not be asked"))
    assert site["check"]() == 0
    [a] = json.loads((site["work"] / "apps.json").read_text())["apps"]
    assert a["state"] == apps.OFF


def test_cli_verified_refuses_junk(site):
    assert cli.main(["apps-verified", "--work", str(site["work"]), "--id", "../x", "--digest", dg("a"),
                     "--result", "ok"]) == 1
    assert cli.main(["apps-verified", "--work", str(site["work"]), "--id", "attendly", "--digest", "sha256:zz",
                     "--result", "ok"]) == 1


# ── withdrawn releases ───────────────────────────────────────────────

REV_IDENTITY = r"^https://github\.com/open-nvr/opennvr-pro-apps/\.github/workflows/revocations\.yml@refs/heads/main$"


def test_parse_revocations_reads_the_list_or_refuses_it():
    doc = {"schema": 1, "revoked": [{"version": "1.3.0", "reason": "loses marks"}, {"version": "1.2.2"}]}
    assert apps.parse_revocations(doc) == {"1.3.0": "loses marks", "1.2.2": "withdrawn"}
    assert apps.parse_revocations({"schema": 1, "revoked": []}) == {}
    for bad in (None, [], {"schema": 2, "revoked": []}, {"schema": 1},
                {"schema": 1, "revoked": [{"version": "latest"}]}, {"schema": 1, "revoked": ["1.3.0"]}):
        with pytest.raises(apps.RegistryError):
            apps.parse_revocations(bad)


def test_cosign_digest_is_what_cosign_checked():
    out = json.dumps([{"critical": {"image": {"docker-manifest-digest": dg("rev")}, "type": "cosign"}}])
    assert apps.cosign_digest(out) == dg("rev")
    for bad in ("", "not json", "[]", json.dumps([{"critical": {"image": {"docker-manifest-digest": "x"}}}])):
        with pytest.raises(apps.RegistryError):
            apps.cosign_digest(bad)


def test_the_catalog_names_who_signs_the_list():
    found, problems = apps.sources([{"id": "attendly", "image": f"{REPO}:1.2.0", "updates": "registry",
                                     "signing": {"identity": IDENTITY, "revocations_identity": REV_IDENTITY}},
                                    {"id": "x", "image": "ghcr.io/open-nvr/x:1.0.0", "updates": "registry",
                                     "signing": {"identity": IDENTITY, "revocations_identity": "("}}])
    assert [s.revocations_identity for s in found] == [REV_IDENTITY]
    assert any("revocations_identity" in p for p in problems)
    import re

    [shipped] = [s for s in apps.load_sources()[0] if s.id == "attendly"]
    ok = "https://github.com/open-nvr/opennvr-pro-apps/.github/workflows/revocations.yml@refs/heads/main"
    assert re.match(shipped.revocations_identity, ok)
    for bad in (ok.replace("refs/heads/main", "refs/heads/feature"), ok.replace("revocations.yml", "publish-images.yml"),
                ok.replace("opennvr-pro-apps", "open-nvr")):
        assert not re.match(shipped.revocations_identity, bad), bad


def _artifact_registry(doc, *, tamper=False, layers=1):
    """A registry holding one oras-pushed artifact, the way revocations.yml
    pushes it: an OCI manifest, an empty config, one JSON layer."""
    body = json.dumps(doc).encode()
    layer = {"mediaType": "application/json", "digest": "sha256:" + hashlib.sha256(body).hexdigest(),
             "size": len(body)}
    manifest = json.dumps({"mediaType": "application/vnd.oci.image.manifest.v1+json",
                           "artifactType": "application/vnd.opennvr.revocations.v1+json",
                           "config": {"mediaType": "application/vnd.oci.empty.v1+json"},
                           "layers": [layer] * layers}).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v2/open-nvr/attendly/manifests/{digest}":
            return httpx.Response(200, content=manifest)
        if request.url.path == f"/v2/open-nvr/attendly/blobs/{layer['digest']}":
            return httpx.Response(200, content=b"{}" if tamper else body)
        return httpx.Response(404)

    return apps.Registry(client=httpx.Client(transport=httpx.MockTransport(handler))), digest


def test_registry_reads_an_artifact_only_by_digest_and_only_its_own_bytes():
    doc = {"schema": 1, "revoked": [{"version": "1.3.0", "reason": "x"}]}
    reg, digest = _artifact_registry(doc)
    assert reg.artifact_json(REPO, digest) == doc
    with pytest.raises(apps.RegistryError, match="not a digest"):
        reg.artifact_json(REPO, "revocations")
    reg, digest = _artifact_registry(doc, tamper=True)
    with pytest.raises(apps.RegistryError, match="does not match"):
        reg.artifact_json(REPO, digest)
    reg, digest = _artifact_registry(doc, layers=2)
    with pytest.raises(apps.RegistryError, match="one-document"):
        reg.artifact_json(REPO, digest)


def _with_revocations(site):
    site["index"].write_text(json.dumps([{"id": "attendly", "image": f"{REPO}:1.2.0", "updates": "registry",
                                          "signing": {"identity": IDENTITY, "revocations_identity": REV_IDENTITY}}]))
    return ["--work", str(site["work"]), "--index", str(site["index"])]


def test_cli_lists_only_the_apps_that_publish_a_list(site):
    base = _with_revocations(site)
    assert cli.main(["apps-revocations-list", *base]) == 0  # no revocations tag published
    assert (site["work"] / "apps-revocations.txt").read_text() == ""
    assert json.loads((site["work"] / "apps-revocations.json").read_text()) == {"attendly": {}}
    site["registry"]._tags.append("revocations")
    assert cli.main(["apps-revocations-list", *base]) == 0
    assert (site["work"] / "apps-revocations.txt").read_text() == \
        f"attendly {REPO}:revocations {REV_IDENTITY} {apps.GITHUB_OIDC_ISSUER}\n"


def test_a_withdrawn_release_is_never_offered(site, monkeypatch):
    base = _with_revocations(site)
    site["registry"]._tags.append("revocations")
    cli.main(["apps-revocations-list", *base])
    seen = {}
    site["registry"].artifact_json = lambda repo, digest: (seen.update(digest=digest)
                                                           or {"schema": 1, "revoked": [{"version": "1.3.0",
                                                                                         "reason": "loses marks"}]})
    out = site["work"] / "cosign.json"
    out.write_text(json.dumps([{"critical": {"image": {"docker-manifest-digest": dg("rev")}}}]))
    assert cli.main(["apps-revocations-record", *base, "--id", "attendly", "--result", "ok",
                     "--cosign-output", str(out)]) == 0
    assert seen["digest"] == dg("rev")  # read where cosign checked, not at the moving tag
    assert site["check"]() == 0
    [a] = json.loads((site["work"] / "apps.json").read_text())["apps"]
    assert [o["version"] for o in a["offers"]] == ["1.2.1"]
    assert any("1.3.0: withdrawn (loses marks)" in s for s in a["skipped"])


def test_a_list_that_does_not_check_out_stops_that_apps_updates(site):
    base = _with_revocations(site)
    site["registry"]._tags.append("revocations")
    cli.main(["apps-revocations-list", *base])
    assert cli.main(["apps-revocations-record", *base, "--id", "attendly", "--result", "failed",
                     "--message", "no matching signatures"]) == 1
    assert site["check"]() == 0
    [a] = json.loads((site["work"] / "apps.json").read_text())["apps"]
    assert a["state"] == apps.ERROR and a["offers"] == []
    assert "no matching signatures" in a["reason"]
    # A registry that cannot be asked about the list stops it the same way.
    site["registry"]._fail = "could not reach ghcr.io"
    cli.main(["apps-revocations-list", *base])
    assert "_failed" in json.loads((site["work"] / "apps-revocations.json").read_text())["attendly"]
