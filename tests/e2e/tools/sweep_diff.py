#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compare two UI sweeps: the API contract strictly, the screenshots by eye.

    python tests/e2e/tools/sweep_diff.py tests/e2e/.artifacts/sweep/before \\
                                         tests/e2e/.artifacts/sweep/after

* **Contract** -- every route's request shapes (see tests/ui/test_ui_sweep.py).
  Any difference is printed and makes the exit code 1. For a presentation-only
  change the right number of differences is zero; for a behaviour change,
  every difference should be one the commit meant to make.
* **Overflow** -- routes that scroll sideways, before and after, per size.
* **Screenshots** -- written as ``<after>/compare.html``: before and after side
  by side for every route, theme and size, so a reviewer can scroll through a
  whole refactor in one page. Pixel diffing is deliberately not attempted: a
  restyle changes every pixel on purpose.

Standard library only, so it runs on the host with no setup.
"""

from __future__ import annotations

import html
import json
import os
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def diff_contract(before: dict, after: dict) -> list[str]:
    lines: list[str] = []
    for route in sorted(set(before) | set(after)):
        b, a = set(before.get(route, [])), set(after.get(route, []))
        if route not in before:
            lines.append(f"{route}: new route in 'after'")
        if route not in after:
            lines.append(f"{route}: route missing from 'after'")
        for gone in sorted(b - a):
            lines.append(f"{route}: - {gone}")
        for new in sorted(a - b):
            lines.append(f"{route}: + {new}")
    return lines


def write_compare_page(before_dir: Path, after_dir: Path) -> Path:
    shots = sorted({p.name for p in (after_dir / "shots").glob("*.png")} | {p.name for p in (before_dir / "shots").glob("*.png")})
    rows = []
    for name in shots:
        b = before_dir / "shots" / name
        a = after_dir / "shots" / name
        rel_b = os.path.relpath(b, after_dir).replace(os.sep, "/")
        cell_b = f'<img loading="lazy" src="{html.escape(rel_b)}">' if b.exists() else "<em>missing</em>"
        cell_a = f'<img loading="lazy" src="shots/{html.escape(name)}">' if a.exists() else "<em>missing</em>"
        rows.append(
            f"<h2>{html.escape(name[:-4])}</h2><div class=pair><figure>{cell_b}<figcaption>before</figcaption></figure>"
            f"<figure>{cell_a}<figcaption>after</figcaption></figure></div>"
        )
    page = after_dir / "compare.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>UI sweep compare</title>"
        "<style>body{font:13px system-ui;margin:16px;background:#111;color:#ddd}"
        "h2{font-size:13px;margin:24px 0 6px}.pair{display:grid;grid-template-columns:1fr 1fr;gap:8px}"
        "figure{margin:0}img{width:100%;border:1px solid #333}figcaption{color:#888}</style>"
        + "".join(rows)
    )
    return page


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    before_dir, after_dir = (Path(p) for p in argv)

    contract = diff_contract(_load(before_dir / "contract.json"), _load(after_dir / "contract.json"))
    print(f"API contract: {len(contract)} difference(s)")
    for line in contract:
        print("  " + line)

    ob, oa = _load(before_dir / "overflow.json"), _load(after_dir / "overflow.json")
    print(f"sideways overflow: {len(ob)} route(s) before, {len(oa)} after")
    for route in sorted(set(oa) - set(ob)):
        print(f"  NEW overflow on {route}: {oa[route]}")

    page = write_compare_page(before_dir, after_dir)
    print(f"screenshots side by side: {page}")
    return 1 if contract else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
