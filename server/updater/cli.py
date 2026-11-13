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

    k = sub.add_parser("keygen", help="a new release signing key")
    k.add_argument("--id", required=True)
    k.set_defaults(func=cmd_keygen)

    a = ap.parse_args(argv)
    return a.func(a)
