# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Where a manifest comes from: GitHub releases, a mirror, or a bundle on disk.

Whatever the source, the bytes are only trusted after ``manifest.verify``.
"""

from __future__ import annotations

from pathlib import Path

from updater.manifest import MANIFEST_NAME, SIGNATURE_NAME, ManifestError

GITHUB_RELEASES = "https://github.com/open-nvr/open-nvr/releases"


def urls(version: str | None, base: str | None = None, *, channel: str = "stable") -> tuple[str, str]:
    """Manifest and signature URLs for *version* (None = the newest on *channel*).

    GitHub: ``…/releases/latest/download/<file>`` or ``…/releases/download/vX.Y.Z/<file>``;
    the newest edge build is on the rolling ``edge`` pre-release.
    A mirror (``UPDATE_SOURCE_URL``) keeps a simpler layout:
    ``<base>/latest/<file>``, ``<base>/vX.Y.Z/<file>`` and ``<base>/edge/<file>``.
    """
    github = base is None or base.rstrip("/") == GITHUB_RELEASES
    if version is None and channel == "edge":
        root = f"{GITHUB_RELEASES}/download/edge" if github else f"{base.rstrip('/')}/edge"
    elif github:
        root = f"{GITHUB_RELEASES}/latest/download" if version is None else f"{GITHUB_RELEASES}/download/v{version}"
    else:
        base = base.rstrip("/")
        root = f"{base}/latest" if version is None else f"{base}/v{version}"
    return f"{root}/{MANIFEST_NAME}", f"{root}/{SIGNATURE_NAME}"


def fetch(version: str | None, base: str | None = None, *, channel: str = "stable",
          timeout: float = 30.0) -> tuple[bytes, bytes]:
    """(manifest bytes, signature bytes) over HTTPS; ManifestError when absent."""
    import httpx

    out = []
    for url in urls(version, base, channel=channel):
        try:
            r = httpx.get(url, follow_redirects=True, timeout=timeout)
        except httpx.HTTPError as exc:
            raise ManifestError(f"could not reach {url}: {exc}") from exc
        if r.status_code == 404:
            raise ManifestError(f"no signed manifest at {url}"
                                + ("" if version else " (no release has one yet)"))
        if r.status_code != 200:
            raise ManifestError(f"{url} answered HTTP {r.status_code}")
        out.append(r.content)
    return out[0], out[1]


def from_dir(path: Path) -> tuple[bytes, bytes]:
    """(manifest bytes, signature bytes) from a directory (a bundle, a copy)."""
    m, s = path / MANIFEST_NAME, path / SIGNATURE_NAME
    if not m.is_file() or not s.is_file():
        raise ManifestError(f"{path} has no {MANIFEST_NAME} with its {SIGNATURE_NAME}")
    return m.read_bytes(), s.read_bytes()


def save(path: Path, raw: bytes, sig: bytes) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / MANIFEST_NAME).write_bytes(raw)
    (path / SIGNATURE_NAME).write_bytes(sig)
