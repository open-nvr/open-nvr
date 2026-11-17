# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""App updates from the registry (docs/design/app-updates-from-registry.md).

An app whose catalog entry says ``updates: registry`` is released by tagging
``vX.Y.Z`` in its own repository: that repository's CI pushes and signs
``<image>:X.Y.Z``. Nothing in this repository changes. This module answers,
for each such app running on the site: which release is it on, and which
newer releases could it move to?

* Only plain ``X.Y.Z`` tags are releases. ``latest``, ``main``, ``1.2``,
  ``sha-…`` and cosign's ``sha256-….sig`` are not, so a push to the app's
  main branch never reaches a site.
* A release is pinned by the digest of what the registry served for its
  tag, computed here from the bytes (never taken from a header).
* The image's labels say what it needs: ``org.opennvr.min_core_version``
  (a site on an older core skips it) and ``org.opennvr.rollback``
  (``manual``: it changes the app's data so the old version can't read it,
  and is never installed automatically).
* Whether a release was signed by the app's own CI is checked on the host
  with cosign (it has Docker; this image has no cosign). This module lists
  what must be checked and records the answers; ``policy.due_app`` refuses
  an unchecked or failed one.

Pure apart from ``Registry``, which is the only part that talks HTTP.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from updater.manifest import ManifestError, is_newer, parse_version, upgrade_kind

#: A release tag: X.Y.Z, nothing else.
RELEASE_TAG = re.compile(r"^\d+\.\d+\.\d+$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_APP_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
#: Registry repository: host/path, lower case (the forms docker accepts).
_REPOSITORY = re.compile(r"^[a-z0-9.-]+(?::\d+)?/[a-z0-9._/-]+$")

LABEL_VERSION = "org.opencontainers.image.version"
LABEL_MIN_CORE = "org.opennvr.min_core_version"
LABEL_ROLLBACK = "org.opennvr.rollback"

GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
DEFAULT_INDEX = Path(__file__).resolve().parents[1] / "config" / "apps_index.yml"

# Report states.
NOT_INSTALLED = "not_installed"
UNMANAGED = "unmanaged"
UP_TO_DATE = "up_to_date"
AVAILABLE = "available"
BLOCKED = "blocked"
ERROR = "error"
OFF = "off"


class RegistryError(Exception):
    """The registry could not be asked, or answered something unusable.
    The message is meant for the operator."""


# ── the catalog side ─────────────────────────────────────────────────


@dataclass(frozen=True)
class AppSource:
    """One catalog app that updates from the registry."""

    id: str
    repository: str  # ghcr.io/open-nvr/attendly
    signer_identity: str  # cosign --certificate-identity-regexp
    signer_issuer: str = GITHUB_OIDC_ISSUER
    #: Who signs the app's list of withdrawn releases (``<repository>:
    #: revocations``). Empty: the app publishes none.
    revocations_identity: str = ""

    @property
    def image_key(self) -> str:
        """The .env variable its compose service reads; the same transform
        as scripts/app-installer/reconciler.py:image_env_key."""
        return self.id.upper().replace("-", "_") + "_IMAGE"


def split_ref(ref: str) -> tuple[str, str | None, str | None]:
    """(repository, tag, digest) of an image reference. A port in the host
    is not a tag: only a colon after the last slash is."""
    rest, _, digest = ref.partition("@")
    last = rest.rsplit("/", 1)[-1]
    tag = None
    if ":" in last:
        rest, _, tag = rest.rpartition(":")
    return rest, tag or None, digest or None


def sources(doc: object) -> tuple[list[AppSource], list[str]]:
    """The apps in a parsed apps_index.yml that update from the registry,
    and one problem line per entry that asks to but cannot (a missing or
    malformed signer: never trusted by default)."""
    found: list[AppSource] = []
    problems: list[str] = []
    entries = doc if isinstance(doc, list) else []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("updates") != "registry":
            continue
        app_id = entry.get("id")
        if not isinstance(app_id, str) or not _APP_ID.match(app_id):
            problems.append(f"an entry with updates: registry has no valid id ({app_id!r})")
            continue
        image = entry.get("image")
        repository = split_ref(image)[0] if isinstance(image, str) else ""
        if not _REPOSITORY.match(repository):
            problems.append(f"{app_id}: image {image!r} is not a registry image")
            continue
        signing = entry.get("signing")
        identity = signing.get("identity") if isinstance(signing, dict) else None
        issuer = signing.get("issuer", GITHUB_OIDC_ISSUER) if isinstance(signing, dict) else None
        if not isinstance(identity, str) or not identity.strip():
            problems.append(f"{app_id}: updates: registry needs signing.identity (who must have signed it)")
            continue
        try:
            re.compile(identity)
        except re.error as exc:
            problems.append(f"{app_id}: signing.identity is not a valid regexp: {exc}")
            continue
        if not isinstance(issuer, str) or not issuer.startswith("https://"):
            problems.append(f"{app_id}: signing.issuer must be an https URL")
            continue
        revocations = signing.get("revocations_identity", "")
        if not isinstance(revocations, str):
            problems.append(f"{app_id}: signing.revocations_identity must be a regexp")
            continue
        try:
            re.compile(revocations)
        except re.error as exc:
            problems.append(f"{app_id}: signing.revocations_identity is not a valid regexp: {exc}")
            continue
        found.append(AppSource(id=app_id, repository=repository,
                               signer_identity=identity.strip(), signer_issuer=issuer.strip(),
                               revocations_identity=revocations.strip()))
    return found, problems


def load_sources(path: Path = DEFAULT_INDEX) -> tuple[list[AppSource], list[str]]:
    import yaml

    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return [], [f"cannot read the app catalog {path}: {exc}"]
    return sources(doc)


# ── what runs on the site ────────────────────────────────────────────


@dataclass(frozen=True)
class Running:
    """One running compose service, as the host saw it: the image reference
    it was started from, and the version label of the image it runs."""

    service: str
    ref: str
    version_label: str = ""


def read_running(text: str) -> dict[str, Running]:
    """``<service> <image ref> [<version label>]`` per line (written by the
    host from ``docker inspect``). Malformed lines are skipped."""
    out: dict[str, Running] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        out[parts[0]] = Running(service=parts[0], ref=parts[1], version_label=parts[2] if len(parts) > 2 else "")
    return out


# ── the registry ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Release:
    """What a release tag points at: the digest to pin (the multi-platform
    index when there is one, so compose picks the platform) and the labels
    of this site's platform build."""

    digest: str
    labels: dict[str, str]


class RegistrySource(Protocol):
    def tags(self, repository: str) -> list[str]: ...

    def release(self, repository: str, tag: str, platform: str) -> Release | None:
        """None when the release has no build for *platform*."""
        ...


_INDEX_TYPES = ("application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json")
_MANIFEST_TYPES = ("application/vnd.oci.image.manifest.v1+json",
                   "application/vnd.docker.distribution.manifest.v2+json")
_ACCEPT = ", ".join(_INDEX_TYPES + _MANIFEST_TYPES)
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _platform(p: str) -> tuple[str, str]:
    os_, _, arch = p.partition("/")
    return os_, arch.split("/", 1)[0]


class Registry:
    """A Docker registry (v2 API) read anonymously, the way ``docker pull``
    does for a public image: ask, and on a 401 get a token from the realm
    the registry names, then ask again."""

    def __init__(self, client=None, timeout: float = 30.0):
        import httpx

        self._http = httpx
        self._client = client or httpx.Client(timeout=timeout, follow_redirects=True)
        self._tokens: dict[tuple[str, str], str] = {}

    def _get(self, repository: str, path: str, *, accept: str | None = None):
        host, _, name = repository.partition("/")
        url = path if path.startswith("https://") else f"https://{host}/v2/{name}/{path}"
        for attempt in (1, 2):
            headers = {"Accept": accept} if accept else {}
            token = self._tokens.get((host, name))
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                r = self._client.get(url, headers=headers)
            except self._http.HTTPError as exc:
                raise RegistryError(f"could not reach {host}: {exc}") from exc
            if r.status_code == 401 and attempt == 1:
                self._tokens[(host, name)] = self._token(host, name, r.headers.get("www-authenticate", ""))
                continue
            if r.status_code != 200:
                raise RegistryError(f"{repository}: {path.split('?')[0]} answered HTTP {r.status_code}")
            return r
        raise RegistryError(f"{repository}: refused even with a token")

    def _token(self, host: str, name: str, challenge: str) -> str:
        if not challenge.lower().startswith("bearer "):
            raise RegistryError(f"{host} asks for a login this updater does not have")
        params = dict(_CHALLENGE.findall(challenge))
        realm = params.pop("realm", "")
        if not realm.startswith("https://"):
            raise RegistryError(f"{host} named no usable token service")
        params.setdefault("scope", f"repository:{name}:pull")
        token = None
        try:
            r = self._client.get(realm, params=params)
            if r.status_code == 200:
                body = r.json()
                token = body.get("token") or body.get("access_token")
        except (self._http.HTTPError, ValueError) as exc:
            raise RegistryError(f"could not get a pull token from {host}: {exc}") from exc
        if not token:
            raise RegistryError(f"{host} gave no pull token for {name} (is the image public?)")
        return token

    def tags(self, repository: str) -> list[str]:
        out: list[str] = []
        path = "tags/list?n=1000"
        host = repository.partition("/")[0]
        for _ in range(50):  # a bound, not a limit anyone reaches
            r = self._get(repository, path)
            try:
                out.extend(t for t in (r.json().get("tags") or []) if isinstance(t, str))
            except ValueError as exc:
                raise RegistryError(f"{repository}: the tag list is not JSON") from exc
            m = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', r.headers.get("link", ""))
            if not m:
                return out
            nxt = m.group(1)
            path = nxt if nxt.startswith("https://") else f"https://{host}{nxt}"
        return out

    def _manifest(self, repository: str, ref: str) -> tuple[str, dict]:
        r = self._get(repository, f"manifests/{ref}", accept=_ACCEPT)
        digest = _sha256(r.content)
        if ref.startswith("sha256:") and digest != ref:
            raise RegistryError(f"{repository}@{ref}: the registry served other bytes ({digest})")
        try:
            return digest, r.json()
        except ValueError as exc:
            raise RegistryError(f"{repository}:{ref}: the manifest is not JSON") from exc

    def release(self, repository: str, tag: str, platform: str) -> Release | None:
        digest, doc = self._manifest(repository, tag)
        want_os, want_arch = _platform(platform)
        manifest = doc
        if doc.get("mediaType") in _INDEX_TYPES or "manifests" in doc:
            chosen = None
            for m in doc.get("manifests") or []:
                p = m.get("platform") or {}
                if p.get("os") == want_os and p.get("architecture") == want_arch:
                    chosen = m.get("digest")
                    break
            if not isinstance(chosen, str) or not _DIGEST.match(chosen):
                return None
            _, manifest = self._manifest(repository, chosen)
        config = (manifest.get("config") or {}).get("digest")
        if not isinstance(config, str) or not _DIGEST.match(config):
            raise RegistryError(f"{repository}:{tag}: the manifest names no config")
        blob = self._get(repository, f"blobs/{config}")
        if _sha256(blob.content) != config:
            raise RegistryError(f"{repository}:{tag}: the config blob does not match its digest")
        try:
            cfg = blob.json()
        except ValueError as exc:
            raise RegistryError(f"{repository}:{tag}: the config is not JSON") from exc
        if manifest is doc and (cfg.get("os"), cfg.get("architecture")) != (want_os, want_arch):
            return None  # a single-platform image for another platform
        labels = (cfg.get("config") or {}).get("Labels") or {}
        return Release(digest=digest, labels={k: str(v) for k, v in labels.items()})

    def artifact_json(self, repository: str, digest: str, *, limit: int = 65536) -> object:
        """The JSON document an OCI artifact at *digest* carries (its one
        layer), e.g. the list ``revocations.yml`` pushes with oras. Only by
        digest: that is what cosign checked, so a moved tag cannot swap it."""
        if not _DIGEST.match(digest):
            raise RegistryError(f"{repository}: {digest!r} is not a digest")
        _, manifest = self._manifest(repository, digest)
        layers = [la for la in manifest.get("layers") or [] if isinstance(la, dict)]
        if len(layers) != 1 or not isinstance(layers[0].get("digest"), str) \
                or not _DIGEST.match(layers[0]["digest"]):
            raise RegistryError(f"{repository}@{digest}: not a one-document artifact")
        if int(layers[0].get("size") or 0) > limit:
            raise RegistryError(f"{repository}@{digest}: the document is larger than {limit} bytes")
        blob = self._get(repository, f"blobs/{layers[0]['digest']}")
        if _sha256(blob.content) != layers[0]["digest"]:
            raise RegistryError(f"{repository}@{digest}: the document does not match its digest")
        try:
            return blob.json()
        except ValueError as exc:
            raise RegistryError(f"{repository}@{digest}: the document is not JSON") from exc


REVOCATIONS_TAG = "revocations"


def parse_revocations(doc: object) -> dict[str, str]:
    """{version: reason} from an app's signed list of withdrawn releases
    (``{"schema": 1, "revoked": [{"version", "reason"}]}``). Anything else
    is refused, never read halfway."""
    if not isinstance(doc, dict) or doc.get("schema") != 1 or not isinstance(doc.get("revoked"), list):
        raise RegistryError("the list of withdrawn releases is not one this updater reads")
    out: dict[str, str] = {}
    for item in doc["revoked"]:
        if not isinstance(item, dict) or not RELEASE_TAG.match(str(item.get("version", ""))):
            raise RegistryError(f"an entry in the list of withdrawn releases is malformed: {item!r}")
        out[str(item["version"])] = str(item.get("reason") or "withdrawn")[:300]
    return out


def cosign_digest(output: str) -> str:
    """The digest ``cosign verify`` checked, from its JSON output."""
    try:
        doc = json.loads(output)
        digest = doc[0]["critical"]["image"]["docker-manifest-digest"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RegistryError("cosign's output names no digest") from exc
    if not isinstance(digest, str) or not _DIGEST.match(digest):
        raise RegistryError(f"cosign's output names no digest ({digest!r})")
    return digest


# ── the check ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Offer:
    """A newer release this site could move to."""

    version: str
    kind: str  # patch | minor | major
    digest: str
    ref: str  # repository@digest: what goes in .env
    rollback: str = "safe"  # safe | manual


@dataclass
class AppReport:
    id: str
    repository: str
    image_key: str
    state: str
    reason: str = ""
    installed_ref: str = ""
    installed_version: str = ""
    installed_revoked: str = ""  # the reason, when the running version was withdrawn
    offers: list[Offer] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # newer releases left out, and why
    signer_identity: str = ""
    signer_issuer: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> AppReport:
        d = dict(d)
        d["offers"] = [Offer(**o) for o in d.get("offers") or []]
        return cls(**d)


def installed_version(src: AppSource, running: Running) -> tuple[str, str]:
    """(version, "") for a site on one of this app's releases, or ("", why
    not) for anything else, which is left alone: another repository (a local
    build), or a moving tag without a release version label."""
    repository, tag, _ = split_ref(running.ref)
    if repository != src.repository:
        return "", f"runs {running.ref}, not an image from {src.repository}; left as it is"
    if RELEASE_TAG.match(running.version_label or ""):
        return running.version_label, ""
    if tag and RELEASE_TAG.match(tag):
        return tag, ""
    return "", (f"runs {running.ref}, which is not a release (version label "
                f"{running.version_label or 'missing'}); set {src.image_key} to a release to get updates")


def check_app(src: AppSource, running: Running | None, *, registry: RegistrySource, platform: str,
              core_version: str, revoked: dict[str, str] | None = None) -> AppReport:
    """What this app could move to. Never raises for a registry problem: the
    report says ERROR with the reason, and nothing gets installed."""
    revoked = revoked or {}
    report = AppReport(id=src.id, repository=src.repository, image_key=src.image_key, state=UP_TO_DATE,
                       signer_identity=src.signer_identity, signer_issuer=src.signer_issuer)
    if running is None:
        report.state, report.reason = NOT_INSTALLED, "not running on this site"
        return report
    report.installed_ref = running.ref
    version, why = installed_version(src, running)
    if not version:
        report.state, report.reason = UNMANAGED, why
        return report
    report.installed_version = version
    report.installed_revoked = revoked.get(version, "")

    try:
        tags = registry.tags(src.repository)
    except RegistryError as exc:
        report.state, report.reason = ERROR, str(exc)
        return report
    newer = []
    for tag in tags:
        if not RELEASE_TAG.match(tag) or not is_newer(tag, version):
            continue
        if tag in revoked:
            report.skipped.append(f"{tag}: withdrawn ({revoked[tag]})")
            continue
        newer.append(tag)
    newer.sort(key=parse_version, reverse=True)

    # The newest usable release of each kind of step (patch, minor, major):
    # the policy then picks the biggest step it allows. Newest first, so a
    # release this core can't run falls back to the one before it.
    for kind in ("major", "minor", "patch"):
        for tag in (t for t in newer if upgrade_kind(version, t) == kind):
            try:
                rel = registry.release(src.repository, tag, platform)
            except RegistryError as exc:
                report.state, report.reason = ERROR, str(exc)
                return report
            if rel is None:
                report.skipped.append(f"{tag}: no build for {platform}")
                continue
            need = rel.labels.get(LABEL_MIN_CORE, "").strip()
            if need:
                try:
                    too_old = parse_version(core_version) < parse_version(need)
                except ManifestError:
                    report.skipped.append(f"{tag}: unreadable {LABEL_MIN_CORE} {need!r}")
                    continue
                if too_old:
                    report.skipped.append(f"{tag}: needs OpenNVR {need} (this site runs {core_version})")
                    continue
            rollback = "manual" if rel.labels.get(LABEL_ROLLBACK, "").strip() == "manual" else "safe"
            report.offers.append(Offer(version=tag, kind=kind, digest=rel.digest,
                                       ref=f"{src.repository}@{rel.digest}", rollback=rollback))
            break

    report.offers.sort(key=lambda o: parse_version(o.version), reverse=True)
    if report.offers:
        report.state = AVAILABLE
        report.reason = f"{report.offers[0].version} is available"
    elif report.skipped:
        report.state, report.reason = BLOCKED, "newer releases exist, but none this site can run"
    else:
        report.reason = f"{version} is the newest release"
    return report


def verify_key(app_id: str, digest: str) -> str:
    """The key a cosign result is kept under: a result is for one app's one
    digest, never carried to another app that happens to share it."""
    return f"{app_id}@{digest}"
