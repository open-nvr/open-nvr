# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The mirror layout scripts/make-offline-bundle.py writes (UPD-503).

The update scripts fetch ``vX.Y.Z/opennvr-<platform>-offline.tar`` and the
updater reads manifests from ``vX.Y.Z/`` and ``latest/`` (updater/source.py);
``latest/`` must follow the newest release even when bundles are added out of
order, and always carry the current withdrawn list.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from updater import manifest, revocations, source

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("make_offline_bundle", REPO / "scripts/make-offline-bundle.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fake_bundle(root: Path, version: str, *, history=(), withdrawn=()) -> Path:
    d = root / f"bundle-{version}"
    d.mkdir(parents=True)
    (d / manifest.MANIFEST_NAME).write_text(json.dumps({"version": version}))
    (d / manifest.SIGNATURE_NAME).write_text("sig")
    (d / revocations.NAME).write_text(json.dumps({"schema": 1, "revoked": [{"version": v} for v in withdrawn]}))
    (d / revocations.SIGNATURE_NAME).write_text("sig")
    for h in history:
        (d / "history" / f"v{h}").mkdir(parents=True)
        (d / "history" / f"v{h}" / manifest.MANIFEST_NAME).write_text(json.dumps({"version": h}))
        (d / "history" / f"v{h}" / manifest.SIGNATURE_NAME).write_text("sig")
    return d


def version_at(path: Path) -> str:
    return json.loads((path / manifest.MANIFEST_NAME).read_text())["version"]


def test_bundle_names_match_what_the_update_scripts_fetch(tool):
    assert tool.bundle_name("linux/amd64") == "opennvr-linux-amd64-offline.tar"
    assert tool.bundle_name("linux/arm64/v8") == "opennvr-linux-arm64-v8-offline.tar"


def test_the_mirror_layout_and_latest_following_the_newest(tool, tmp_path):
    mirror = tmp_path / "mirror"
    tool._fill_mirror(mirror, fake_bundle(tmp_path, "0.2.1", history=("0.2.0",)), "0.2.1")
    assert version_at(mirror / "v0.2.1") == version_at(mirror / "latest") == "0.2.1"
    assert version_at(mirror / "v0.2.0") == "0.2.0"  # history, for a site with no record
    # An older release added later does not move latest; the withdrawn list does update.
    tool._fill_mirror(mirror, fake_bundle(tmp_path, "0.1.9", withdrawn=("0.2.0",)), "0.1.9")
    assert version_at(mirror / "latest") == "0.2.1" and version_at(mirror / "v0.1.9") == "0.1.9"
    assert "0.2.0" in (mirror / "latest" / revocations.NAME).read_text()
    tool._fill_mirror(mirror, fake_bundle(tmp_path, "0.3.0"), "0.3.0")
    assert version_at(mirror / "latest") == "0.3.0"


def test_the_updater_reads_that_layout(tmp_path):
    assert source.urls("0.2.1", "https://mirror.lan/onvr")[0] == "https://mirror.lan/onvr/v0.2.1/release-manifest.json"
    assert source.urls(None, "https://mirror.lan/onvr")[1] == "https://mirror.lan/onvr/latest/release-manifest.json.sig"
