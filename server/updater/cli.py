# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""``python -m updater …``: run by the host scripts inside a core image.

The host mounts the OpenNVR folder read-only at /repo and
``data/updates`` read-write at /work, and passes what only the host can see
(platform, git state, running containers) as files and arguments.

* ``check``  (installed release's image) — fetch and verify a candidate,
  decide, write the plan; exit 0 ready, 10 up to date, 20 refused,
  21 needs the operator, 1 error.
* ``verify`` (the image that should now run) — exit 0 when the stack is
  that release and healthy, 30 when it is not.
* ``due`` — exit 0 when the update found should be installed automatically
  now (updater/policy.py), 11 with the reason when not.
* ``keygen`` — a new release signing key.
* ``apps-check`` — which newer release each registry-updated app could move
  to (updater/apps.py); writes apps.json, and apps-verify.txt with the
  images the host must check with cosign.
* ``apps-verified`` — the host records one cosign result.
* ``apps-due`` — exit 0 with apps-due.env when an app update should be
  installed now, 11 when none should.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from updater import check, envfile, manifest, revocations, source, verify

FAILED_VERIFY = 30


def installed_version() -> str:
    """The version of the image this runs in (main.py's __version__, read
    rather than imported: importing main starts the whole app)."""
    text = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("cannot tell which version this image is")
    return m.group(1)


def _lines(path: Path | None) -> tuple[str, ...]:
    if path is None or not path.is_file():
        return ()
    return tuple(line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _load_installed(work: Path, version: str, base: str | None, bundle: Path | None) -> manifest.Manifest | None:
    """The installed release's manifest: kept from the last update, or (first
    use) the one published for this version, from a bundle's history or
    online. None when there is none to be had."""
    places = [work / "installed"]
    if bundle is not None:
        places.append(bundle / "history" / f"v{version}")
    for place in places:
        try:
            raw, sig = source.from_dir(place)
            found = manifest.verify(raw, sig)
        except manifest.ManifestError:
            continue
        if found.version == version:
            if place != work / "installed":
                source.save(work / "installed", raw, sig)
            return found
    if bundle is not None:
        return None  # air-gapped: no network to ask
    try:
        raw, sig = source.fetch(version, base)
        found = manifest.verify(raw, sig)
    except manifest.ManifestError:
        return None
    source.save(work / "installed", raw, sig)
    return found


class _Tee:
    """Write to the terminal and keep a copy (check.log, for the app)."""

    def __init__(self, stream, copy: list[str]):
        self.stream, self.copy = stream, copy

    def write(self, text: str) -> int:
        self.copy.append(text)
        return self.stream.write(text)

    def flush(self) -> None:
        self.stream.flush()


def _note_seen(work: Path, version: str) -> None:
    """When this version was first found: the automatic delay counts from it."""
    seen = envfile.read_env(work / "candidate-seen.env")
    if seen.get("VERSION") != version:
        envfile.write_kv(work / "candidate-seen.env",
                         {"VERSION": version, "FIRST_SEEN": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")})


def cmd_check(a: argparse.Namespace) -> int:
    """`_check`, with its outcome recorded for the app: check.env (when,
    and the exit code) and check.log (what it said)."""
    said: list[str] = []
    out, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = _Tee(out, said), _Tee(err, said)
    try:
        code = _check(a)
    finally:
        sys.stdout, sys.stderr = out, err
    now = datetime.now(UTC)
    try:
        a.work.mkdir(parents=True, exist_ok=True)
        envfile.write_kv(a.work / "check.env", {"CHECKED_AT": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                                "CHECKED_EPOCH": str(int(now.timestamp())), "CODE": str(code)})
        (a.work / "check.log").write_text("".join(said), encoding="utf-8")
    except OSError:
        pass  # the record is for the app; the check itself already answered
    return code


def _check(a: argparse.Namespace) -> int:
    work: Path = a.work
    for stale in ("plan.json", "plan.env", "pins.env", "images.txt"):
        (work / stale).unlink(missing_ok=True)
    version = a.installed_version or installed_version()
    try:
        if a.bundle:
            raw, sig = source.from_dir(a.bundle)
        else:
            raw, sig = source.fetch(a.to, a.source_url, channel=a.channel)
        candidate = manifest.verify(raw, sig)
        # Withdrawn releases: from the bundle (as it was when made) or the
        # current list online. A list that fails its check stops here.
        revoked = revocations.from_dir(a.bundle) if a.bundle else revocations.fetch(a.source_url)
    except manifest.ManifestError as exc:
        print(f"Cannot check for an update: {exc}", file=sys.stderr)
        return 1
    if a.to and candidate.version != a.to.removeprefix("v"):
        print(f"Asked for {a.to}, but the manifest found is {candidate.version}.", file=sys.stderr)
        return check.REFUSED

    site = check.Site(
        installed_version=version,
        installed=_load_installed(work, version, a.source_url, a.bundle),
        env=envfile.read_env(a.repo / ".env"),
        head=a.head, changed_files=_lines(a.changed_files), platform=a.platform,
        site_images=_lines(a.site_images), core_image_id=a.core_image_id or None,
        db_revision=verify.db_revision(os.environ.get("DATABASE_URL")),
        accept_manual=a.accept_manual, adopt=a.adopt, allow_edge=a.channel == "edge",
        revoked=revoked,
    )
    result = check.decide(site, candidate)
    if isinstance(result, check.Refusal):
        print(result.reason)
        for line in result.advice:
            print(f"  - {line}")
        if result.code == check.NEEDS_OPERATOR:
            # Kept so the app can show the update and the steps it needs.
            source.save(work / "candidate", raw, sig)
            _note_seen(work, candidate.version)
        return result.code

    source.save(work / "candidate", raw, sig)
    _note_seen(work, candidate.version)
    (work / "plan.json").write_text(json.dumps(asdict(result), indent=2) + "\n", encoding="utf-8")
    envfile.write_kv(work / "plan.env", {
        "FROM_VERSION": result.from_version, "TO_VERSION": result.to_version, "KIND": result.kind,
        "GIT_COMMIT": result.git_commit, "GIT_TAG": result.git_tag, "FROM_COMMIT": result.from_commit,
        "DB_RESTORE_ON_ROLLBACK": "1" if result.db_restore_on_rollback else "0", "ROLLBACK": result.rollback,
        # The site did not match the installed manifest (that is what --adopt
        # waived), so a rollback must not be verified against it either.
        "ADOPTED": "1" if a.adopt else "0",
    })
    envfile.write_kv(work / "pins.env", result.pins)
    (work / "images.txt").write_text(
        "".join(f"{i.ref} {i.digest} {','.join(i.accept)}\n" for i in result.images), encoding="utf-8", newline="\n")

    print(f"OpenNVR {result.from_version} -> {result.to_version} ({result.kind} update)")
    for key, value in result.pins.items():
        print(f"  .env {key}={value}")
    print(f"  {len(result.images)} images to fetch; rollback: {result.rollback}"
          + (", restores the database" if result.db_restore_on_rollback else ""))
    for w in result.warnings:
        print(f"  ! {w}")
    return check.READY


def cmd_verify(a: argparse.Namespace) -> int:
    try:
        raw, sig = source.from_dir(a.manifest_dir)
        expected = manifest.verify(raw, sig)
    except manifest.ManifestError as exc:
        print(f"Cannot verify: {exc}", file=sys.stderr)
        return 1
    running = verify.read_running(a.running.read_text(encoding="utf-8")) if a.running.is_file() else []
    found = verify.wait_until_healthy(expected, a.platform, running, os.environ.get("DATABASE_URL"),
                                      timeout=a.timeout)
    if found:
        print(f"OpenNVR {expected.version} did not come up right:")
        for p in found:
            print(f"  - {p}")
        return FAILED_VERIFY
    print(f"OpenNVR {expected.version} is running and healthy.")
    return 0


NOT_DUE = 11


def cmd_due(a: argparse.Namespace) -> int:
    """Exit 0 when the update the host found should be installed now, else
    11 with the reason (updater/policy.py)."""
    from updater import policy as pol

    work: Path = a.work
    p = pol.Policy.from_env(envfile.read_env(a.policy_file))
    try:
        raw, sig = source.from_dir(work / "candidate")
        candidate = manifest.verify(raw, sig)
    except manifest.ManifestError as exc:
        print(f"Not now: {exc}")
        return NOT_DUE
    seed_file = work / "site-seed"
    if not seed_file.is_file():
        seed_file.write_text(uuid.uuid4().hex + "\n", encoding="utf-8")
    seen = envfile.read_env(work / "candidate-seen.env")
    first_seen = None
    if seen.get("VERSION") == candidate.version and seen.get("FIRST_SEEN"):
        first_seen = datetime.fromisoformat(seen["FIRST_SEEN"].replace("Z", "+00:00"))
    ok, why = pol.due(p, candidate, a.installed_version or installed_version(),
                      local_now=datetime.fromisoformat(a.local_time), utc_now=datetime.now(UTC),
                      first_seen=first_seen, seed=seed_file.read_text(encoding="utf-8").strip())
    print(f"{candidate.version}: {why}")
    return 0 if ok else NOT_DUE


def _read_json(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_json(path: Path, doc: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)  # the app never reads half a file


def _now_z() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_apps_check(a: argparse.Namespace) -> int:
    from updater import apps, policy as pol

    work: Path = a.work
    work.mkdir(parents=True, exist_ok=True)
    core = a.installed_version or installed_version()
    found, problems = apps.load_sources(a.index or apps.DEFAULT_INDEX)
    running = apps.read_running(a.running.read_text(encoding="utf-8")) if a.running.is_file() else {}
    # The lists of withdrawn releases the host verified (apps-revocations-*).
    revoked_all = _read_json(a.revocations or work / "apps-revocations.json")
    p = pol.Policy.from_env(envfile.read_env(a.policy_file)) if a.policy_file else pol.Policy()
    verified = _read_json(work / "apps-verified.json")
    seen = _read_json(work / "apps-seen.json")
    registry = apps.Registry()

    reports, to_verify, still_seen = [], [], {}
    for src in found:
        listed = revoked_all.get(src.id)
        if p.app_mode(src.id) == "off":
            report = apps.AppReport(id=src.id, repository=src.repository, image_key=src.image_key,
                                    state=apps.OFF, reason="app updates are off")
        elif isinstance(listed, dict) and "_failed" in listed:
            # As for OpenNVR releases: a list that does not check out stops
            # the updates, since it is not safe to guess what it withdrew.
            report = apps.AppReport(id=src.id, repository=src.repository, image_key=src.image_key,
                                    state=apps.ERROR,
                                    reason=f"its list of withdrawn releases did not check out: {listed['_failed']}")
        else:
            revoked = revoked_all.get(src.id)
            revoked = {str(k): str(v) for k, v in revoked.items()} if isinstance(revoked, dict) else {}
            report = apps.check_app(src, running.get(src.id), registry=registry, platform=a.platform,
                                    core_version=core, revoked=revoked)
        reports.append(report)
        for offer in report.offers:
            key = f"{src.id} {offer.version}"
            still_seen[key] = seen.get(key) or _now_z()
            if not (verified.get(apps.verify_key(src.id, offer.digest)) or {}).get("ok"):
                to_verify.append(f"{src.id} {offer.ref} {src.signer_identity} {src.signer_issuer}\n")
        print(f"{src.id}: {report.state}: {report.reason}")
        for line in report.skipped:
            print(f"  - skipped {line}")
    for line in problems:
        print(f"! {line}", file=sys.stderr)

    _write_json(work / "apps.json", {"checked_at": _now_z(), "core_version": core, "platform": a.platform,
                                     "problems": problems, "apps": [r.to_dict() for r in reports]})
    _write_json(work / "apps-seen.json", still_seen)
    (work / "apps-verify.txt").write_text("".join(to_verify), encoding="utf-8", newline="\n")
    return 0


_APP_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def cmd_apps_revocations_list(a: argparse.Namespace) -> int:
    """Start a fresh apps-revocations.json, and list in apps-revocations.txt
    (``<id> <repository>:revocations <identity> <issuer>``) the apps that
    publish a list, for the host to check with cosign. An app that names no
    signer for one, or has not published one, has nothing withdrawn."""
    from updater import apps

    work: Path = a.work
    work.mkdir(parents=True, exist_ok=True)
    found, _ = apps.load_sources(a.index or apps.DEFAULT_INDEX)
    registry = apps.Registry()
    lists: dict[str, dict] = {}
    lines = []
    for src in found:
        lists[src.id] = {}
        if not src.revocations_identity:
            continue
        try:
            published = apps.REVOCATIONS_TAG in registry.tags(src.repository)
        except apps.RegistryError as exc:
            lists[src.id] = {"_failed": f"could not ask the registry: {exc}"}
            continue
        if published:
            lines.append(f"{src.id} {src.repository}:{apps.REVOCATIONS_TAG} {src.revocations_identity} "
                         f"{src.signer_issuer}\n")
    _write_json(work / "apps-revocations.json", lists)
    (work / "apps-revocations.txt").write_text("".join(lines), encoding="utf-8", newline="\n")
    return 0


def cmd_apps_revocations_record(a: argparse.Namespace) -> int:
    """The host's cosign check of one app's list: on success, read the list
    at the digest cosign checked; on failure (or a list that cannot be read),
    record why, which stops that app's updates."""
    from updater import apps

    if not _APP_ID.match(a.id):
        print("apps-revocations-record: --id must be an app id", file=sys.stderr)
        return 1
    path = a.work / "apps-revocations.json"
    lists = _read_json(path)
    src = next((s for s in apps.load_sources(a.index or apps.DEFAULT_INDEX)[0] if s.id == a.id), None)
    if src is None:
        print(f"apps-revocations-record: {a.id} does not update from the registry", file=sys.stderr)
        return 1
    if a.result == "failed":
        lists[a.id] = {"_failed": (a.message or "cosign refused it")[:300]}
    else:
        try:
            digest = apps.cosign_digest(a.cosign_output.read_text(encoding="utf-8") if a.cosign_output else "")
            lists[a.id] = apps.parse_revocations(apps.Registry().artifact_json(src.repository, digest))
        except (OSError, apps.RegistryError) as exc:
            lists[a.id] = {"_failed": str(exc)[:300]}
    _write_json(path, lists)
    if "_failed" in lists[a.id]:
        print(f"{a.id}: {lists[a.id]['_failed']}", file=sys.stderr)
        return 1
    print(f"{a.id}: {len(lists[a.id])} withdrawn")
    return 0


def cmd_apps_verified(a: argparse.Namespace) -> int:
    from updater import apps

    if not _APP_ID.match(a.id) or not _DIGEST.match(a.digest):
        print("apps-verified: --id must be an app id and --digest sha256:<64 hex>", file=sys.stderr)
        return 1
    path = a.work / "apps-verified.json"
    doc = _read_json(path)
    doc[apps.verify_key(a.id, a.digest)] = {"ok": a.result == "ok", "message": (a.message or "")[:500],
                                            "at": _now_z()}
    _write_json(path, doc)
    return 0


def cmd_apps_due(a: argparse.Namespace) -> int:
    from updater import apps, policy as pol

    work: Path = a.work
    work.mkdir(parents=True, exist_ok=True)
    (work / "apps-due.env").unlink(missing_ok=True)
    if a.now and not a.app:
        print("apps-due: --now is for one app (--app)", file=sys.stderr)
        return 1
    p = pol.Policy.from_env(envfile.read_env(a.policy_file))
    doc = _read_json(work / "apps.json")
    verified = _read_json(work / "apps-verified.json")
    seen = _read_json(work / "apps-seen.json")
    # "<id> <version>" per line: releases the host installed and had to roll
    # back. Never retried by themselves; Update now may still take one.
    failed = set(_lines(work / "apps-failed.txt"))
    seed_file = work / "site-seed"
    if not seed_file.is_file():
        seed_file.write_text(uuid.uuid4().hex + "\n", encoding="utf-8")
    seed = seed_file.read_text(encoding="utf-8").strip()
    local_now, utc_now = datetime.fromisoformat(a.local_time), datetime.now(UTC)

    reports = []
    for raw in doc.get("apps") or []:
        try:
            reports.append(apps.AppReport.from_dict(raw))
        except TypeError:
            continue  # written by another version of this updater; the next check rewrites it
    if a.app:
        reports = [r for r in reports if r.id == a.app]
        if not reports:
            print(f"{a.app}: not an app this site updates from the registry (run apps-check)")
            return NOT_DUE
    for report in reports:
        offer, why = pol.choose_offer(p, report, now=a.now)
        if offer is None:
            print(f"{report.id}: {why}")
            continue
        if not a.now and f"{report.id} {offer.version}" in failed:
            print(f"{report.id} {report.installed_version} -> {offer.version}: it did not come up healthy "
                  "here before and was rolled back; install it by hand")
            continue
        record = verified.get(apps.verify_key(report.id, offer.digest))
        check = (bool(record.get("ok")), str(record.get("message", ""))) if isinstance(record, dict) else None
        first = seen.get(f"{report.id} {offer.version}")
        ok, why = pol.due_app(p, report.id, offer, verified=check, local_now=local_now, utc_now=utc_now,
                              first_seen=datetime.fromisoformat(first.replace("Z", "+00:00")) if first else None,
                              seed=seed, now=a.now)
        print(f"{report.id} {report.installed_version} -> {offer.version}: {why}")
        if ok:
            envfile.write_kv(work / "apps-due.env", {
                "APP_ID": report.id, "IMAGE_KEY": report.image_key, "IMAGE_REF": offer.ref,
                "VERSION": offer.version, "FROM_VERSION": report.installed_version,
                "FROM_REF": report.installed_ref, "ROLLBACK": offer.rollback,
            })
            return 0  # one app per run: each gets its own backup and health check
    return NOT_DUE


def cmd_keygen(a: argparse.Namespace) -> int:
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())
    print("Store this as the RELEASE_SIGNING_KEY secret, and nowhere else:")
    print(f"  {base64.b64encode(seed).decode()}")
    print(f"Set RELEASE_SIGNING_KEY_ID to {a.id!r}, and add this to server/updater/keys.json:")
    print("  " + json.dumps(manifest.public_key_entry(key, a.id)))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m updater")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="fetch, verify and plan an update")
    c.add_argument("--work", type=Path, default=Path("/work"))
    c.add_argument("--repo", type=Path, default=Path("/repo"))
    c.add_argument("--to", help="a release version; default: the latest")
    c.add_argument("--channel", choices=("stable", "edge"), default="stable")
    c.add_argument("--source-url", default=os.environ.get("UPDATE_SOURCE_URL") or None)
    c.add_argument("--bundle", type=Path, help="an unpacked offline bundle")
    c.add_argument("--platform", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--changed-files", type=Path)
    c.add_argument("--site-images", type=Path)
    c.add_argument("--core-image-id")
    c.add_argument("--installed-version")
    c.add_argument("--accept-manual", action="store_true")
    c.add_argument("--adopt", action="store_true")
    c.set_defaults(func=cmd_check)

    v = sub.add_parser("verify", help="is the stack the release in --manifest-dir, and healthy?")
    v.add_argument("--manifest-dir", type=Path, required=True)
    v.add_argument("--running", type=Path, required=True)
    v.add_argument("--platform", required=True)
    v.add_argument("--timeout", type=float, default=300.0)
    v.set_defaults(func=cmd_verify)

    d = sub.add_parser("due", help="should the update found be installed automatically now?")
    d.add_argument("--work", type=Path, default=Path("/work"))
    d.add_argument("--policy-file", type=Path, required=True)
    d.add_argument("--local-time", required=True, help="the site's local time, YYYY-MM-DDTHH:MM")
    d.add_argument("--installed-version")
    d.set_defaults(func=cmd_due)

    ac = sub.add_parser("apps-check", help="newer releases of the apps that update from the registry")
    ac.add_argument("--work", type=Path, default=Path("/work"))
    ac.add_argument("--index", type=Path, default=None, help="apps_index.yml; default: this image's")
    ac.add_argument("--running", type=Path, required=True, help="<service> <image ref> [<version label>] lines")
    ac.add_argument("--platform", required=True)
    ac.add_argument("--installed-version", help="the core version; default: this image's")
    ac.add_argument("--revocations", type=Path, help="{app id: {version: reason}}, already verified; default: <work>/apps-revocations.json")
    ac.add_argument("--policy-file", type=Path)
    ac.set_defaults(func=cmd_apps_check)

    rl = sub.add_parser("apps-revocations-list", help="which apps publish a list of withdrawn releases")
    rl.add_argument("--work", type=Path, default=Path("/work"))
    rl.add_argument("--index", type=Path, default=None)
    rl.set_defaults(func=cmd_apps_revocations_list)

    rr = sub.add_parser("apps-revocations-record", help="record the host's cosign check of one app's list")
    rr.add_argument("--work", type=Path, default=Path("/work"))
    rr.add_argument("--index", type=Path, default=None)
    rr.add_argument("--id", required=True)
    rr.add_argument("--result", choices=("ok", "failed"), required=True)
    rr.add_argument("--cosign-output", type=Path, help="cosign verify's JSON output")
    rr.add_argument("--message", default="")
    rr.set_defaults(func=cmd_apps_revocations_record)

    av = sub.add_parser("apps-verified", help="record the host's cosign check of one app image")
    av.add_argument("--work", type=Path, default=Path("/work"))
    av.add_argument("--id", required=True)
    av.add_argument("--digest", required=True)
    av.add_argument("--result", choices=("ok", "failed"), required=True)
    av.add_argument("--message", default="")
    av.set_defaults(func=cmd_apps_verified)

    ad = sub.add_parser("apps-due", help="should an app update be installed now?")
    ad.add_argument("--work", type=Path, default=Path("/work"))
    ad.add_argument("--policy-file", type=Path, required=True)
    ad.add_argument("--local-time", required=True, help="the site's local time, YYYY-MM-DDTHH:MM")
    ad.add_argument("--app", help="only this app")
    ad.add_argument("--now", action="store_true", help="the operator's Update now: no window, no delay")
    ad.set_defaults(func=cmd_apps_due)

    k = sub.add_parser("keygen", help="a new release signing key")
    k.add_argument("--id", required=True)
    k.set_defaults(func=cmd_keygen)

    a = ap.parse_args(argv)
    return a.func(a)
