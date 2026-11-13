# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Announce OpenNVR on the LAN over mDNS/DNS-SD (HA-117).

Home Assistant (and anything else that browses ``_opennvr._tcp``) then
offers "OpenNVR discovered" instead of asking for a URL. Optional: manual
URL entry always works and is the supported default.

Runs as the ``opennvr-mdns`` compose service (profile ``mdns``) with
``network_mode: host``, because multicast must leave the host's own NIC.
That makes it **Linux-only**: Docker Desktop (Windows/macOS) has no real
host networking, so the announcement would stay inside the VM.

It advertises only what is public anyway (``GET /health``: the version),
the HTTPS port and the API path. The site id, and everything else, needs a
token.

Environment:
  OPENNVR_HOST_IP     the LAN address to announce (required; start.sh sets it)
  OPENNVR_LAN_IPS     more addresses, comma-separated (multi-NIC)
  OPENNVR_HTTPS_PORT  default 443
  OPENNVR_HEALTH_URL  default https://127.0.0.1:<port>/health
  OPENNVR_MDNS_NAME   instance name, default "OpenNVR"
"""

from __future__ import annotations

import ipaddress
import json
import os
import signal
import socket
import ssl
import sys
import time
import urllib.request

SERVICE_TYPE = "_opennvr._tcp.local."
TXT_SCHEMA = "1"
REFRESH_S = 300


def build_txt(*, version: str | None, port: int, api_path: str = "/api/v1") -> dict[str, str]:
    """The TXT record. Only public facts; values are short strings (each
    key=value must stay under 255 bytes)."""
    txt = {"txtvers": TXT_SCHEMA, "path": api_path, "https": "1", "port": str(int(port))}
    if version:
        txt["version"] = str(version)[:40]
    return txt


def parse_addresses(primary: str | None, extra: str | None) -> list[str]:
    """Unique, valid, non-loopback IPv4/IPv6 addresses, primary first."""
    out: list[str] = []
    for raw in [primary or ""] + (extra or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_unspecified or str(ip) in out:
            continue
        out.append(str(ip))
    return out


def read_version(url: str, timeout: float = 5.0) -> str | None:
    """Core's version from the public /health, or None while it is down."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # core's own self-signed cert on loopback
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as resp:
            return json.loads(resp.read().decode()).get("version")
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    from zeroconf import ServiceInfo, Zeroconf

    port = int(os.environ.get("OPENNVR_HTTPS_PORT", "443"))
    addresses = parse_addresses(os.environ.get("OPENNVR_HOST_IP"),
                                os.environ.get("OPENNVR_LAN_IPS"))
    if not addresses:
        print("mdns-announcer: OPENNVR_HOST_IP is not set; nothing to announce", flush=True)
        return 2
    name = os.environ.get("OPENNVR_MDNS_NAME", "OpenNVR").strip() or "OpenNVR"
    health = os.environ.get("OPENNVR_HEALTH_URL", f"https://127.0.0.1:{port}/health")
    zc = Zeroconf()
    stop = {"now": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(now=True))
    info = None
    try:
        while not stop["now"]:
            new = ServiceInfo(
                SERVICE_TYPE, f"{name}.{SERVICE_TYPE}",
                addresses=[socket.inet_pton(socket.AF_INET6 if ":" in a else socket.AF_INET, a)
                           for a in addresses],
                port=port,
                properties=build_txt(version=read_version(health), port=port),
                server=f"{socket.gethostname()}.local.",
            )
            if info is None:
                zc.register_service(new)
                print(f"mdns-announcer: announcing {name} on {addresses}:{port}", flush=True)
            elif new.properties != info.properties:
                zc.update_service(new)
            info = new
            for _ in range(REFRESH_S):
                if stop["now"]:
                    break
                time.sleep(1)
    finally:
        if info is not None:
            zc.unregister_service(info)
        zc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
