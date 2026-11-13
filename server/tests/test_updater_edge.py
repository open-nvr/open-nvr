# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""The edge channel (UPD-504): signed manifests for main builds.

X.Y.(Z+1)-edge.N comes after the last release and before the next, in run
order; edge is found only when asked for, never installed by itself; an
edge manifest pins CORE_TAG to the build's sha- images and names no tag.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

from tests.test_updater_check import NEW_C, release, site
from updater import check, manifest, source
from updater.policy import Policy, due

REPO = Path(__file__).resolve().parents[2]


def test_edge_versions_sit_between_releases():
    order = ["0.1.8", "0.1.9-edge.9", "0.1.9-edge.12", "0.1.9-edge.100", "0.1.9", "0.1.10-edge.1"]
    assert sorted(order, key=manifest.parse_version) == order


def test_edge_is_found_only_where_asked():
    gh = "https://github.com/open-nvr/open-nvr/releases"
    assert source.urls(None, channel="edge")[0] == f"{gh}/download/edge/release-manifest.json"
    assert source.urls(None, "https://mirror.lan/o", channel="edge")[0] == "https://mirror.lan/o/edge/release-manifest.json"
    assert source.urls(None)[0] == f"{gh}/latest/download/release-manifest.json"


def test_edge_is_offered_only_on_request_and_never_by_itself():
    edge = manifest.parse(release("0.1.9-edge.12", NEW_C, "bbb", channel="edge", git_tag=""))
    assert isinstance(check.decide(site(), edge), check.Refusal)
    plan = check.decide(site(allow_edge=True), edge)
    assert isinstance(plan, check.Plan) and plan.git_tag == ""
    ok, why = due(Policy(mode="auto"), edge, "0.1.8", local_now=datetime(2026, 11, 1, 3),
                  utc_now=datetime(2026, 11, 4, tzinfo=UTC), first_seen=None, seed="s")
    assert not ok and "only releases" in why


def test_an_edge_manifest_pins_the_builds_images_and_names_no_tag():
    spec = importlib.util.spec_from_file_location("make_manifest", REPO / "scripts/release/make_manifest.py")
    mk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mk)

    class Reg:
        def resolve(self, ref):
            return "sha256:" + "a" * 64, {"linux/amd64": "sha256:" + "b" * 64}, {}
    doc = mk.build("0.1.9-edge.12", "1" * 40, {}, Reg(), channel="edge", pin_overrides={"CORE_TAG": "sha-1234567"})
    m = manifest.parse(doc)
    assert m.channel == "edge" and m.git_tag == "" and m.pins["CORE_TAG"] == "sha-1234567"
    assert any(i.ref == "ghcr.io/open-nvr/core:sha-1234567" for i in m.images)
    assert m.notes_url.endswith("/releases/tag/edge")
