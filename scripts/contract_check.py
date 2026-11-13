#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Fail when the HA-facing contract changes without the right version bump (HA-115).

Compares ``server/contract/contract.json`` with the same file at a base git
ref (default ``origin/main``):

* **breaking** (an endpoint, frame type, required field, descriptor
  field/platform/command, schema field or type removed or narrowed) needs a
  MAJOR bump;
* **additive** (anything new) needs at least a MINOR bump;
* no change needs no bump;
* the version never goes backwards.

It also checks that ``core/contract.py``'s CONTRACT_VERSION matches the file.

    python scripts/contract_check.py [--base origin/main]

A base without the file (before the contract existed) passes.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = "server/contract/contract.json"


def _ver(v: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", v or "")
    if not m:
        raise SystemExit(f"contract_check: bad version {v!r}")
    return tuple(int(x) for x in m.groups())


def _types(schema: dict) -> set:
    t = schema.get("type")
    return set([t] if isinstance(t, str) else (t or []))


def diff(base: dict, head: dict) -> tuple[list[str], list[str]]:
    """``(breaking, additive)`` changes from base to head."""
    breaking: list[str] = []
    additive: list[str] = []

    def keyed(c):
        return {(e["method"], e["path"]): e for e in c.get("rest", [])}

    b, h = keyed(base), keyed(head)
    for k in b.keys() - h.keys():
        breaking.append(f"REST {k[0]} {k[1]} removed")
    for k in h.keys() - b.keys():
        additive.append(f"REST {k[0]} {k[1]} added")
    for k in b.keys() & h.keys():
        if b[k].get("token_scope") != h[k].get("token_scope"):
            breaking.append(f"REST {k[0]} {k[1]} token scope changed")
        if b[k].get("response") and b[k].get("response") != h[k].get("response"):
            breaking.append(f"REST {k[0]} {k[1]} response schema changed")

    def frames(c, part):
        return (c.get("ws_v2") or {}).get(part) or {}

    for part in ("control_frames", "event_types"):
        bf, hf = frames(base, part), frames(head, part)
        for name in bf.keys() - hf.keys():
            breaking.append(f"ws {part} {name!r} removed")
        for name in hf.keys() - bf.keys():
            additive.append(f"ws {part} {name!r} added")
        for name in bf.keys() & hf.keys():
            gone = set(bf[name]) - set(hf[name])
            if gone:
                breaking.append(f"ws {name!r} fields removed: {sorted(gone)}")
            if set(hf[name]) - set(bf[name]):
                additive.append(f"ws {name!r} fields added")
    if set((base.get("ws_v2") or {}).get("event_frame", [])) - set(
            (head.get("ws_v2") or {}).get("event_frame", [])):
        breaking.append("ws event frame fields removed")

    gone = set(base.get("features", [])) - set(head.get("features", []))
    if gone:
        breaking.append(f"features removed: {sorted(gone)}")
    if set(head.get("features", [])) - set(base.get("features", [])):
        additive.append("features added")

    bd, hd = base.get("descriptor") or {}, head.get("descriptor") or {}
    for field in ("required", "optional", "platforms", "command_types", "core_controls"):
        gone = set(bd.get(field, [])) - set(hd.get(field, []))
        new = set(hd.get(field, [])) - set(bd.get(field, []))
        if gone:
            breaking.append(f"descriptor {field} removed: {sorted(gone)}")
        if new:
            additive.append(f"descriptor {field} added: {sorted(new)}")

    bs, hs = base.get("schemas") or {}, head.get("schemas") or {}
    for name in bs.keys() - hs.keys():
        breaking.append(f"schema {name!r} removed")
    for name in hs.keys() - bs.keys():
        additive.append(f"schema {name!r} added")
    for name in bs.keys() & hs.keys():
        _schema_diff(f"schema {name}", bs[name], hs[name], breaking, additive)
    return breaking, additive


def _schema_diff(where, b: dict, h: dict, breaking, additive) -> None:
    gone = set(b.get("required", [])) - set(h.get("required", []))
    if gone:
        breaking.append(f"{where}: no longer guarantees {sorted(gone)}")
    if set(h.get("required", [])) - set(b.get("required", [])):
        additive.append(f"{where}: now guarantees more fields")
    bt, ht = _types(b), _types(h)
    if bt and ht and not ht <= bt:
        breaking.append(f"{where}: type widened {sorted(bt)} -> {sorted(ht)}")
    if "enum" in b and "enum" in h and set(h["enum"]) - set(b["enum"]):
        breaking.append(f"{where}: enum gained values clients may not handle")
    if b.get("$ref") != h.get("$ref"):
        breaking.append(f"{where}: $ref changed")
    bp, hp = b.get("properties") or {}, h.get("properties") or {}
    for key in bp.keys() & hp.keys():
        _schema_diff(f"{where}.{key}", bp[key], hp[key], breaking, additive)
    if "items" in b and "items" in h:
        _schema_diff(f"{where}[]", b["items"], h["items"], breaking, additive)


def verdict(base: dict | None, head: dict) -> list[str]:
    """Problems (empty = OK) for going from base to head."""
    hv = _ver(head["contract_version"])
    if base is None:
        return []
    bv = _ver(base["contract_version"])
    if hv < bv:
        return [f"version went backwards: {bv} -> {hv}"]
    breaking, additive = diff(base, head)
    if breaking and hv[0] == bv[0]:
        return [f"breaking change without a MAJOR bump ({bv} -> {hv}):"] + \
            [f"  - {x}" for x in breaking]
    if additive and hv[:2] == bv[:2] and not breaking:
        return [f"additive change without a MINOR bump ({bv} -> {hv}):"] + \
            [f"  - {x}" for x in additive]
    return []


def _base_contract(ref: str) -> dict | None:
    """The contract at ``ref``; None only when the FILE is absent there (the
    contract is new). A ref that cannot be resolved (e.g. a shallow CI
    checkout that never fetched it) is an error, not a pass."""
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                      cwd=ROOT, capture_output=True).returncode != 0:
        raise SystemExit(f"contract_check: base ref {ref!r} not found; fetch it first")
    shown = subprocess.run(["git", "show", f"{ref}:{CONTRACT}"], cwd=ROOT,
                           capture_output=True, text=True)
    if shown.returncode != 0:
        return None
    return json.loads(shown.stdout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    args = ap.parse_args()
    head = json.loads((ROOT / CONTRACT).read_text(encoding="utf-8"))
    code = (ROOT / "server/core/contract.py").read_text(encoding="utf-8")
    m = re.search(r'CONTRACT_VERSION\s*=\s*"([^"]+)"', code)
    problems = []
    if not m or m.group(1) != head["contract_version"]:
        problems.append(f"core/contract.py CONTRACT_VERSION {m and m.group(1)!r} != "
                        f"{CONTRACT} {head['contract_version']!r}")
    base = _base_contract(args.base)
    if base is None:
        print(f"contract_check: no {CONTRACT} at {args.base}; nothing to compare")
    problems += verdict(base, head)
    for p in problems:
        print(p, file=sys.stderr)
    if not problems:
        print(f"contract_check: OK ({head['contract_version']})")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
