# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Regenerate ``server/config/covered_vendors.json``, the vendor list the
security check matches cameras against.

The §889 vendors and their FCC Covered List entries come from a dated
release of the us-restricted-parties feed, which is rebuilt daily from
48 CFR 52.204-25 (eCFR) and fcc.gov/supplychain/coveredlist. Each file is
checked against the SHA-256 in that release's manifest before use. The
brand strings a camera reports (``hikvision``, ``zte``) and the affiliate
brands are OpenNVR's own and live below.

The check itself never goes online; this script is run by hand when the
lists change::

    cd server && python -m scripts.refresh_covered_vendors            # latest
    cd server && python -m scripts.refresh_covered_vendors --tag data-2026-11-18

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

FEED = "https://github.com/Mattbusel/us-restricted-parties"
FILES = ("us_far_889.jsonl", "us_fcc_covered.jsonl")
OUT = Path(__file__).resolve().parent.parent / "config" / "covered_vendors.json"

# §889 record id -> (display name, strings a camera's manufacturer/model/name
# carries). A §889 record missing here stops the refresh rather than being
# silently left out of the check.
BRANDS: dict[str, tuple[str, list[str]]] = {
    "us_far_889-hangzhou-hikvision-digital-technology-company": ("Hikvision", ["hikvision"]),
    "us_far_889-dahua-technology-company": ("Dahua", ["dahua"]),
    "us_far_889-huawei-technologies-company": ("Huawei", ["huawei"]),
    "us_far_889-hytera-communications-corporation": ("Hytera", ["hytera"]),
    "us_far_889-zte-corporation": ("ZTE", ["zte"]),
}

# Consumer/OEM brands the check flags under a covered parent. Not on either
# list by name; kept as OpenNVR has always had them.
AFFILIATE_BRANDS: list[tuple[str, str]] = [
    ("ezviz", "Hikvision"),
    ("annke", "Hikvision"),
    ("lorex", "Dahua"),
]


def _listing(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "list": row["LIST_NAME"],
        "listed_on": row.get("LISTED_ON"),
        "source_url": row.get("SOURCE_URL"),
    }


def build_snapshot(
    far_rows: list[dict[str, Any]],
    fcc_rows: list[dict[str, Any]],
    release: str,
    built_at: str,
    sha256: dict[str, str],
) -> dict[str, Any]:
    """Build the snapshot from the §889 and FCC Covered List records.

    Raises ``ValueError`` when a §889 vendor has no entry in ``BRANDS``.
    """
    fcc_by_slug = {r["RECORD_ID"].split("-", 1)[1]: r for r in fcc_rows}
    vendors: list[dict[str, Any]] = []
    for row in far_rows:
        rid = row["RECORD_ID"]
        if rid not in BRANDS:
            raise ValueError(
                f"§889 lists {row['NAME_ORG']!r} ({rid}), which has no brand "
                "mapping; add it to BRANDS in refresh_covered_vendors.py"
            )
        parent, match = BRANDS[rid]
        # SOURCE_DOC names the eCFR edition the §889 names were read from.
        lists = [{**_listing(row), "source_doc": row.get("SOURCE_DOC")}]
        fcc = fcc_by_slug.get(rid.split("-", 1)[1])
        if fcc:
            lists.append(_listing(fcc))
        vendors.append({
            "parent": parent, "kind": "branded", "match": match,
            "legal_name": row["NAME_ORG"], "lists": lists,
        })
    for brand, parent in AFFILIATE_BRANDS:
        vendors.append({"parent": parent, "kind": "affiliate", "match": [brand]})
    return {
        "_generated_by": "server/scripts/refresh_covered_vendors.py; do not edit by hand",
        "source": FEED,
        "release": release,
        "built_at": built_at,
        "sha256": sha256,
        "vendors": vendors,
    }


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "opennvr-refresh"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _latest_data_tag() -> str:
    """Newest ``data-*`` release; the feed also tags loader releases."""
    api = FEED.replace("https://github.com/", "https://api.github.com/repos/")
    releases = json.loads(_get(f"{api}/releases?per_page=30"))
    return next(r["tag_name"] for r in releases if r["tag_name"].startswith("data-"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", default="latest", help="feed data release tag (default: newest data-* release)")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    release = _latest_data_tag() if args.tag == "latest" else args.tag
    manifest = json.loads(_get(f"{FEED}/releases/download/{release}/manifest.json"))
    by_file = {v["file"]: v["sha256"] for v in manifest["lists"].values()}

    rows: dict[str, list[dict[str, Any]]] = {}
    for name in FILES:
        data = _get(f"{FEED}/releases/download/{release}/{name}")
        digest = hashlib.sha256(data).hexdigest()
        if digest != by_file.get(name):
            print(f"{name}: sha256 {digest} does not match the manifest", file=sys.stderr)
            return 1
        rows[name] = [json.loads(line) for line in data.decode("utf-8").splitlines() if line]

    snap = build_snapshot(
        rows["us_far_889.jsonl"], rows["us_fcc_covered.jsonl"],
        release=release, built_at=manifest["built_at"],
        sha256={n: by_file[n] for n in FILES},
    )
    args.out.write_text(json.dumps(snap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {args.out} from {release} ({len(snap['vendors'])} entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
