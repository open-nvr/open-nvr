"""End-to-end: Home Assistant with ONLY its MQTT integration discovers OpenNVR (HA-402/403).

Run by ``scripts/ha-dev/e2e-mqtt.ps1``, which starts a Mosquitto broker on
OpenNVR's network (published on the host's port 1883 for Home Assistant)
and a fresh dev Home Assistant WITHOUT the OpenNVR integration.

Checks:

1. an OpenNVR MQTT integration (bound to a fresh API token) is accepted and
   its connection test passes;
2. Home Assistant's MQTT integration, pointed at the broker, discovers
   OpenNVR's devices and entities;
3. states arrive (at least one camera's occupancy has a value);
4. a command from Home Assistant (a camera's detection switch) changes
   OpenNVR, audited as the MQTT integration; it is switched back;
5. OpenNVR reports ``mqtt_discovery`` in /system/info.

The OpenNVR integration and token are removed at the end.

Environment: HA_URL, NVR_URL, NVR_JWT, BROKER_FROM_NVR (e.g.
mqtt://opennvr_mosquitto:1883), BROKER_HOST_FROM_HA (e.g. host.docker.internal).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import aiohttp

sys.path.insert(0, os.path.dirname(__file__))
from e2e_ha import HA, HAClient, check, nvr, results  # noqa: E402

BROKER_FROM_NVR = os.environ.get("BROKER_FROM_NVR", "mqtt://opennvr_mosquitto:1883")
BROKER_HOST_FROM_HA = os.environ.get("BROKER_HOST_FROM_HA", "host.docker.internal")


async def main() -> int:
    async with aiohttp.ClientSession() as s:
        ha = HAClient(s)
        await ha.wait_up()
        await ha.onboard()
        tok = await nvr(s, "POST", "/api-tokens", json={
            "name": f"mqtt-e2e-{int(time.time())}", "expires_in_days": 1,
            "scopes": ["settings.view", "cameras.view", "cameras.manage", "alerts.view"]})
        integration_id = None
        try:
            created = await nvr(s, "POST", "/integrations", json={
                "name": "ha-mqtt-e2e", "type": "mqtt", "enabled": True,
                "config": {"broker_url": BROKER_FROM_NVR, "api_token_id": tok["id"],
                           "ha_discovery": True}})
            integration_id = created["id"]
            tested = await nvr(s, "POST", f"/integrations/{integration_id}/test")
            check("OpenNVR MQTT integration connects", tested.get("status") == "ok",
                  tested.get("message", ""))

            flow = await ha.post("/api/config/config_entries/flow", {"handler": "mqtt"})
            if flow.get("type") == "menu":         # HA offers "broker" among others
                flow = await ha.post(f"/api/config/config_entries/flow/{flow['flow_id']}",
                                     {"next_step_id": "broker"})
            fields = {f["name"] for f in flow.get("data_schema") or []}
            answer = {k: v for k, v in (
                ("broker", BROKER_HOST_FROM_HA), ("port", 1883), ("protocol", "5"),
                # HA 2026.9's advanced section: no client certificate, plain TCP.
                ("other_settings", {"set_client_cert": False, "set_ca_cert": "off",
                                    "transport": "tcp"})) if k in fields}
            print(f"MQTT flow step {flow.get('step_id')}: fields {sorted(fields)}")
            async with ha.s.post(f"{HA}/api/config/config_entries/flow/{flow['flow_id']}",
                                 json=answer, headers=ha.h) as r:
                if r.status != 200:
                    print("MQTT flow refused:", r.status, await r.text(),
                          flow.get("data_schema"))
                    r.raise_for_status()
                flow = await r.json()
            check("Home Assistant's MQTT integration is set up",
                  flow.get("type") == "create_entry", str(flow.get("errors") or ""))

            ents: list[dict] = []
            end = time.monotonic() + 60
            while time.monotonic() < end:
                ents = [e for e in await ha.ws({"type": "config/entity_registry/list"})
                        if e.get("platform") == "mqtt"]
                if len(ents) > 10:
                    break
                await asyncio.sleep(2)
            devices = [d for d in await ha.ws({"type": "config/device_registry/list"})
                       if d.get("manufacturer") == "OpenNVR"]
            check("devices and entities discovered", len(ents) > 10 and len(devices) >= 2,
                  f"{len(devices)} devices, {len(ents)} entities")

            occupancy = [e["entity_id"] for e in ents
                         if e["entity_id"].startswith("binary_sensor.")
                         and "occupancy" in e["entity_id"]]
            # Entities are registered before they subscribe to availability
            # and state; give the retained messages a moment to land.
            valued: list[str] = []
            end = time.monotonic() + 30
            while time.monotonic() < end:
                states = await ha.states()
                valued = [i for i in occupancy
                          if states.get(i, {}).get("state") in ("on", "off")]
                if valued:
                    break
                await asyncio.sleep(2)
            if not valued:
                sample = {i: states.get(i, {}).get("state") for i in occupancy[:5]}
                print("occupancy states:", sample)
            check("states arrive", bool(valued), f"{len(valued)}/{len(occupancy)} occupancy")

            switch = next((e["entity_id"] for e in ents if e["entity_id"].startswith("switch.")
                           and e["entity_id"].endswith("_object_detection")
                           and states.get(e["entity_id"], {}).get("state") == "on"), None)
            if switch is None:
                check("a command reaches OpenNVR", False, "no detection switch that is on")
            else:
                await ha.post("/api/services/switch/turn_off", {"entity_id": switch})
                changed = False
                for _ in range(30):
                    logs = await nvr(s, "GET", "/audit-logs/",
                                     params={"action": "entity.command", "limit": 5})
                    items = logs.get("items") or logs.get("logs") or []
                    if any("mqtt:ha-mqtt-e2e" in str(i.get("details")) for i in items):
                        changed = True
                        break
                    await asyncio.sleep(1)
                await ha.post("/api/services/switch/turn_on", {"entity_id": switch})
                check("a command reaches OpenNVR (audited as MQTT)", changed, switch)

            info = await nvr(s, "GET", "/system/info")
            check("/system/info reports MQTT discovery", info.get("mqtt_discovery") is True)

            # Deleting the integration takes its devices out of Home Assistant.
            await nvr(s, "DELETE", f"/integrations/{integration_id}")
            integration_id = None
            left: list[dict] = devices
            end = time.monotonic() + 20
            while time.monotonic() < end and left:
                await asyncio.sleep(2)
                left = [d for d in await ha.ws({"type": "config/device_registry/list"})
                        if d.get("manufacturer") == "OpenNVR"]
            check("deleting the integration removes its devices", not left,
                  f"{len(left)} left")
        finally:
            if integration_id is not None:
                await nvr(s, "DELETE", f"/integrations/{integration_id}")
            await nvr(s, "DELETE", f"/api-tokens/{tok['id']}")
            await ha.close_ws()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    print(f"Home Assistant: {HA}")
    sys.exit(asyncio.run(main()))
