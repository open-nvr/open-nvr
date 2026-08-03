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

    python -m manage_devices list        # shows the ID of every known browser
    python -m manage_devices approve 3
    python -m manage_devices block 4
    python -m manage_devices off         # disable enforcement (admin toggle)
    python -m manage_devices on

Devices are addressed by the ID from ``list``, not by IP — a browser is
identified by the device cookie the server issued it (see
``services.device_firewall_service``), and many browsers share one address
behind NAT.

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
    print(f"{'ID':<5} {'LAST IP':<20} {'STATUS':<10} {'LOGINS':<7} LABEL / AGENT")
    for d in rows:
        print(
            f"{d.id:<5} {(d.ip_address or '-'):<20} "
            f"{(d.status.value if d.status else '?'):<10} "
            f"{d.attempt_count or 0:<7} {d.label or (d.user_agent or '')[:50]}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage_devices")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list all known devices with their IDs")
    p_ap = sub.add_parser("approve", help="approve a device by ID")
    p_ap.add_argument("device_id", type=int)
    p_ap.add_argument("--label", default=None)
    p_bl = sub.add_parser("block", help="block a device by ID")
    p_bl.add_argument("device_id", type=int)
    p_rm = sub.add_parser("delete", help="forget a device by ID")
    p_rm.add_argument("device_id", type=int)
    sub.add_parser("on", help="enable enforcement")
    sub.add_parser("off", help="disable enforcement")

    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        if args.cmd == "list":
            _print_devices(db)
        elif args.cmd == "approve":
            dev = dfw.approve(db, args.device_id, label=args.label)
            print(
                f"approved device {dev.id} (last seen {dev.ip_address or '-'})"
                if dev
                else "not found"
            )
        elif args.cmd == "block":
            dev = dfw.block(db, args.device_id)
            print(f"blocked device {args.device_id}" if dev else "not found")
        elif args.cmd == "delete":
            print("deleted" if dfw.delete(db, args.device_id) else "not found")
        elif args.cmd in ("on", "off"):
            eff = dfw.set_enforcement(db, args.cmd == "on")
            print(f"enforcement active: {eff}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
