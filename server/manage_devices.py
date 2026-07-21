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

"""
Break-glass CLI for the device firewall.

Recovery from a lockout without touching the database by hand. Run inside the
server container / host:

    python -m manage_devices list
    python -m manage_devices approve 192.168.1.20
    python -m manage_devices block 10.0.0.5
    python -m manage_devices off        # disable enforcement (admin toggle)
    python -m manage_devices on

The hardest kill remains the env override: set ``DEVICE_FIREWALL_KILL=true`` and
restart — enforcement is then forced off regardless of the stored toggle.
"""

from __future__ import annotations

import argparse
import sys

from core.database import SessionLocal
from services import device_firewall_service as dfw


def _print_devices(db) -> None:
    rows = dfw.list_devices(db)
    print(f"enforcement active: {dfw.enforcement_active(db)}")
    if not rows:
        print("(no devices recorded)")
        return
    print(f"{'IP':<20} {'STATUS':<10} {'SEEN':<6} LABEL")
    for d in rows:
        print(
            f"{d.ip_address:<20} {(d.status.value if d.status else '?'):<10} "
            f"{d.attempt_count or 0:<6} {d.label or ''}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage_devices")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list all known devices")
    p_ap = sub.add_parser("approve", help="approve an IP")
    p_ap.add_argument("ip")
    p_ap.add_argument("--label", default=None)
    p_bl = sub.add_parser("block", help="block an IP")
    p_bl.add_argument("ip")
    p_rm = sub.add_parser("delete", help="forget an IP")
    p_rm.add_argument("ip")
    sub.add_parser("on", help="enable enforcement")
    sub.add_parser("off", help="disable enforcement")

    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        if args.cmd == "list":
            _print_devices(db)
        elif args.cmd == "approve":
            dev = dfw.approve(db, args.ip, label=args.label)
            print(f"approved {dev.ip_address}")
        elif args.cmd == "block":
            dfw.block(db, args.ip)
            print(f"blocked {args.ip}")
        elif args.cmd == "delete":
            print("deleted" if dfw.delete(db, args.ip) else "not found")
        elif args.cmd in ("on", "off"):
            eff = dfw.set_enforcement(db, args.cmd == "on")
            print(f"enforcement active: {eff}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
