# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""scripts/pin_apps_index.py — writes image digests into the catalog
index textually (comments survive), with an injected resolver."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("pin_apps_index", REPO_ROOT / "scripts" / "pin_apps_index.py")
pin = importlib.util.module_from_spec(_spec)
sys.modules["pin_apps_index"] = pin
_spec.loader.exec_module(pin)

SAMPLE = """\
# The curated index — comments everywhere.
- id: alpha
  name: Alpha
  kind: installable
  image: ghcr.io/open-nvr/alpha:latest   # published by CI
  # image_digest: sha256:<64-hex>        # fill at release
  install:
    compose: |
      services:
        alpha:
          image: ghcr.io/open-nvr/alpha:latest
- id: beta
  name: Beta
  image: ghcr.io/open-nvr/beta:latest
  image_digest: sha256:%s
  requires_tasks: []
- id: gamma
  name: Gamma
  kind: external
  external_url: https://vendor.example
- id: delta
  name: Delta
  image: opennvr/delta:local-build
"""
OLD = "b" * 64
NEW_A = "sha256:" + "a" * 64
NEW_B = "sha256:" + "c" * 64


def test_split_ref():
    assert pin.split_ref("ghcr.io/open-nvr/alpha:latest") == ("ghcr.io", "open-nvr/alpha", "latest")
    assert pin.split_ref("ghcr.io/open-nvr/alpha") == ("ghcr.io", "open-nvr/alpha", "latest")
    assert pin.split_ref("ghcr.io/open-nvr/alpha:1.2@sha256:" + "0" * 64)[2] == "1.2"
    with pytest.raises(ValueError, match="no registry"):
        pin.split_ref("opennvr/delta:local-build")


def test_set_digests_replaces_commented_and_real_lines_and_keeps_everything_else():
    text = SAMPLE % OLD
    out, previous = pin.set_digests(text, {"alpha": NEW_A, "beta": NEW_B})
    assert previous == {"alpha": None, "beta": "sha256:" + OLD}
    assert f"  image: ghcr.io/open-nvr/alpha:latest   # published by CI\n  image_digest: {NEW_A}\n  install:" in out
    assert "# image_digest: sha256:<64-hex>" not in out          # the placeholder was replaced in place
    assert f"  image_digest: {NEW_B}\n  requires_tasks: []" in out
    assert out.startswith("# The curated index — comments everywhere.\n")
    assert "          image: ghcr.io/open-nvr/alpha:latest\n" in out   # the compose snippet untouched
    parsed = {e["id"]: e for e in yaml.safe_load(out)}
    assert parsed["alpha"]["image_digest"] == NEW_A and parsed["beta"]["image_digest"] == NEW_B
    # Inserting where no digest line exists at all.
    out2, prev2 = pin.set_digests("- id: x\n  image: ghcr.io/open-nvr/x:latest\n  emits: []\n", {"x": NEW_A})
    assert out2 == f"- id: x\n  image: ghcr.io/open-nvr/x:latest\n  image_digest: {NEW_A}\n  emits: []\n"
    assert prev2 == {"x": None}
    with pytest.raises(KeyError):
        pin.set_digests(text, {"nope": NEW_A})


def _run(tmp_path, argv, resolver, verifier=None, capsys=None):
    idx = tmp_path / "apps_index.yml"
    if not idx.exists():
        idx.write_text(SAMPLE % OLD)
    rc = pin.main(argv + ["--index", str(idx)], resolver=resolver,
                  verifier=verifier or (lambda image, digest: (True, "ok")))
    return rc, idx


def test_main_pins_installable_entries_only(tmp_path, capsys):
    seen = []

    def resolver(image):
        seen.append(image)
        if "delta" in image:
            raise ValueError("no registry host")
        return NEW_A if "alpha" in image else NEW_B
    rc, idx = _run(tmp_path, [], resolver)
    assert rc == 1                                                   # delta cannot be pinned → reported
    out = capsys.readouterr()
    assert "delta: FAILED" in out.err and "alpha: sha256:aaaa" in out.out and "(NEW)" in out.out
    assert "gamma" not in "".join(seen)                              # external: never resolved
    parsed = {e["id"]: e for e in yaml.safe_load(idx.read_text())}
    assert parsed["alpha"]["image_digest"] == NEW_A and parsed["beta"]["image_digest"] == NEW_B
    assert "image_digest" not in parsed["delta"]


def test_main_check_dry_run_app_filter_and_verify(tmp_path, capsys):
    resolver = lambda image: NEW_B if "beta" in image else NEW_A  # noqa: E731
    rc, idx = _run(tmp_path, ["--check", "--app", "beta"], resolver)
    assert rc == 1 and "beta: pin sha256:bbbb" in capsys.readouterr().out
    assert yaml.safe_load(idx.read_text())[1]["image_digest"] == "sha256:" + OLD   # nothing written
    rc, _ = _run(tmp_path, ["--dry-run", "--app", "alpha"], resolver)
    assert rc == 0 and "image_digest" not in yaml.safe_load(idx.read_text())[0]
    rc, _ = _run(tmp_path, ["--app", "nope"], resolver)
    assert rc == 2
    # --verify refuses to pin what does not verify.
    rc, _ = _run(tmp_path, ["--verify", "--app", "alpha"], resolver,
                 verifier=lambda image, digest: (False, "no matching signatures"))
    assert rc == 1 and "alpha: UNSIGNED" in capsys.readouterr().err
    assert "image_digest" not in yaml.safe_load(idx.read_text())[0]
    rc, _ = _run(tmp_path, ["--verify", "--app", "alpha"], resolver)
    assert rc == 0 and yaml.safe_load(idx.read_text())[0]["image_digest"] == NEW_A
    rc, _ = _run(tmp_path, ["--check", "--app", "alpha"], resolver)
    assert rc == 0 and "pinned and current" in capsys.readouterr().out
