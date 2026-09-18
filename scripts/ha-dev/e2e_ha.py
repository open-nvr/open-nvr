"""End-to-end: a real Home Assistant against the running OpenNVR (HA-207).

Run by ``scripts/ha-dev/e2e-ha.ps1``, which starts a fresh dev Home Assistant
(``run-ha.ps1``, on Docker's default bridge: it reaches OpenNVR through the
host's nginx like a LAN install, never from inside ``opennvr_internal``) and
mints an OpenNVR admin JWT for the setup steps.

Checks, each reported PASS/FAIL:

1. onboard Home Assistant through its onboarding API;
2. mint an OpenNVR API token and add the integration through the config flow;
3. the entities appear (a camera per OpenNVR camera, descriptor entities,
   the alarm panel and the update entity);
4. occupancy follows the fakecams: some camera's "All occupancy" is seen on
   and off (pushed over the events socket);
5. after a Home Assistant restart the entities are back quickly;
5b. the media browser lists events, and a thumbnail and a clip play
   through Home Assistant's own URL; a relay link works without login;
5c. a card session reads OpenNVR directly but cannot write, and the
   passthrough relays reads;
6. a command from Home Assistant is audited in OpenNVR under Home
   Assistant's context id (``X-Correlation-Id``);
7. revoking the token raises the ``token_revoked`` repair.

The integration entry and the token are removed at the end. The command
check flips one camera's object detection off and back on within seconds.

Environment: HA_URL, NVR_URL (from this host), NVR_URL_FROM_HA (from the HA
container), NVR_JWT (admin), E2E_OCCUPANCY_S (default 150).
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
import time
from typing import Any

import aiohttp

HA = os.environ.get("HA_URL", "http://localhost:8123").rstrip("/")
NVR = os.environ.get("NVR_URL", "https://localhost").rstrip("/")
NVR_FROM_HA = os.environ.get("NVR_URL_FROM_HA", "https://host.docker.internal").rstrip("/")
NVR_JWT = os.environ["NVR_JWT"]
OCCUPANCY_S = float(os.environ.get("E2E_OCCUPANCY_S", "150"))
CLIENT_ID = f"{HA}/"
SCOPES = ["settings.view", "cameras.view", "cameras.manage", "live.view", "recordings.view",
          "alerts.view"]

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""),
          flush=True)
    return ok


class HAClient:
    def __init__(self, session: aiohttp.ClientSession) -> None:
        self.s = session
        self.token: str | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._id = 0

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    async def wait_up(self, timeout: float = 240) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                async with self.s.get(f"{HA}/api/onboarding") as r:
                    if r.status in (200, 401, 404):
                        return
            except aiohttp.ClientError:
                pass
            await asyncio.sleep(2)
        raise TimeoutError("Home Assistant did not come up")

    async def onboard(self) -> None:
        password = secrets.token_urlsafe(16)
        async with self.s.post(f"{HA}/api/onboarding/users", json={
                "client_id": CLIENT_ID, "name": "OpenNVR e2e", "username": "e2e",
                "password": password, "language": "en"}) as r:
            r.raise_for_status()
            code = (await r.json())["auth_code"]
        async with self.s.post(f"{HA}/auth/token", data={
                "grant_type": "authorization_code", "code": code,
                "client_id": CLIENT_ID}) as r:
            r.raise_for_status()
            self.token = (await r.json())["access_token"]
        for step, body in (("core_config", {}), ("analytics", {}),
                           ("integration", {"client_id": CLIENT_ID,
                                            "redirect_uri": f"{CLIENT_ID}?auth_callback=1"})):
            async with self.s.post(f"{HA}/api/onboarding/{step}", json=body,
                                   headers=self.h) as r:
                r.raise_for_status()

    async def post(self, path: str, body: Any = None) -> Any:
        async with self.s.post(f"{HA}{path}", json=body or {}, headers=self.h) as r:
            r.raise_for_status()
            return await r.json()

    async def states(self) -> dict[str, dict]:
        async with self.s.get(f"{HA}/api/states", headers=self.h) as r:
            r.raise_for_status()
            return {s["entity_id"]: s for s in await r.json()}

    async def ws(self, msg: dict) -> Any:
        if self._ws is None or self._ws.closed:
            self._ws = await self.s.ws_connect(f"{HA.replace('http', 'ws', 1)}/api/websocket")
            await self._ws.receive_json()                       # auth_required
            await self._ws.send_json({"type": "auth", "access_token": self.token})
            if (await self._ws.receive_json())["type"] != "auth_ok":
                raise RuntimeError("HA websocket auth failed")
        self._id += 1
        await self._ws.send_json({"id": self._id, **msg})
        while True:
            reply = await self._ws.receive_json()
            if reply.get("id") == self._id:
                if not reply.get("success"):
                    raise RuntimeError(f"{msg['type']}: {reply.get('error')}")
                return reply["result"]

    async def close_ws(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


async def nvr(session: aiohttp.ClientSession, method: str, path: str, **kw) -> Any:
    async with session.request(method, f"{NVR}/api/v1{path}", ssl=False,
                               headers={"Authorization": f"Bearer {NVR_JWT}"}, **kw) as r:
        r.raise_for_status()
        return await r.json() if r.content_type == "application/json" else None


async def main() -> int:
    async with aiohttp.ClientSession() as s:
        ha = HAClient(s)
        await ha.wait_up()
        try:
            await ha.onboard()
            check("onboard Home Assistant", True)
        except aiohttp.ClientResponseError as err:
            check("onboard Home Assistant", False, f"{err.status} {err.message} "
                  "(needs a fresh HA config dir)")
            return 1

        cameras = await nvr(s, "GET", "/cameras/")
        cameras = cameras.get("cameras", cameras) if isinstance(cameras, dict) else cameras
        tok = await nvr(s, "POST", "/api-tokens", json={
            "name": f"ha-e2e-{int(time.time())}", "scopes": SCOPES, "expires_in_days": 1})
        entry_id = None
        try:
            flow = await ha.post("/api/config/config_entries/flow", {"handler": "opennvr"})
            flow = await ha.post(f"/api/config/config_entries/flow/{flow['flow_id']}", {
                "url": NVR_FROM_HA, "api_token": tok["token"], "verify_ssl": False})
            if flow.get("step_id") == "cameras":
                flow = await ha.post(f"/api/config/config_entries/flow/{flow['flow_id']}",
                                     {"cameras": [str(c["id"]) for c in cameras]})
            ok = flow.get("type") == "create_entry"
            entry_id = (flow.get("result") or {}).get("entry_id")
            if not check("config flow creates the entry", ok and bool(entry_id),
                         str(flow.get("errors") or flow.get("reason") or "")):
                return 1
            await asyncio.sleep(10)
            await check_entities(ha, entry_id, cameras)
            await check_occupancy(ha, entry_id)
            await check_restart(ha, entry_id)
            await check_media(ha, entry_id, cameras)
            await check_card(s, ha, entry_id)
            await check_correlation(s, ha, entry_id)
            await check_revoke(s, ha, entry_id, tok["id"])
        finally:
            if entry_id:
                async with s.delete(f"{HA}/api/config/config_entries/entry/{entry_id}",
                                    headers=ha.h):
                    pass
            try:
                await nvr(s, "DELETE", f"/api-tokens/{tok['id']}")
            except aiohttp.ClientResponseError:
                pass
            await ha.close_ws()
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed"
          + (f"; FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


async def registry(ha: HAClient, entry_id: str) -> list[dict]:
    entries = await ha.ws({"type": "config/entity_registry/list"})
    return [e for e in entries if e.get("config_entry_id") == entry_id]


async def check_entities(ha: HAClient, entry_id: str, cameras: list[dict]) -> None:
    ents = await registry(ha, entry_id)
    by_domain: dict[str, int] = {}
    for e in ents:
        d = e["entity_id"].split(".", 1)[0]
        by_domain[d] = by_domain.get(d, 0) + 1
    check("entities appear", by_domain.get("camera", 0) == len(cameras)
          and by_domain.get("sensor", 0) > 0 and by_domain.get("binary_sensor", 0) > 0
          and by_domain.get("alarm_control_panel") == 1 and by_domain.get("update") == 1,
          str(dict(sorted(by_domain.items()))))
    states = await ha.states()
    unavailable = [e["entity_id"] for e in ents if e.get("disabled_by") is None
                   and states.get(e["entity_id"], {}).get("state") == "unavailable"]
    check("entities are available", not unavailable, ", ".join(unavailable[:5]))


def _occupancy_ids(ents: list[dict]) -> list[str]:
    return [e["entity_id"] for e in ents if e["entity_id"].startswith("binary_sensor.")
            and e["entity_id"].endswith("_all_occupancy") and "zone" not in e["entity_id"]]


async def check_occupancy(ha: HAClient, entry_id: str) -> None:
    ids = _occupancy_ids(await registry(ha, entry_id))
    seen: dict[str, set[str]] = {i: set() for i in ids}
    end = time.monotonic() + OCCUPANCY_S
    while time.monotonic() < end:
        states = await ha.states()
        for i in ids:
            seen[i].add(states.get(i, {}).get("state", "?"))
        if any({"on", "off"} <= v for v in seen.values()):
            break
        await asyncio.sleep(1)
    toggled = [i for i, v in seen.items() if {"on", "off"} <= v]
    check("occupancy follows the cameras (on and off)", bool(toggled),
          ", ".join(toggled) or str({i: sorted(v) for i, v in seen.items()}))


async def check_restart(ha: HAClient, entry_id: str) -> None:
    ids = _occupancy_ids(await registry(ha, entry_id))
    await ha.close_ws()
    try:
        await ha.post("/api/services/homeassistant/restart")
    except aiohttp.ClientError:
        pass
    await asyncio.sleep(5)
    end = time.monotonic() + 240
    while time.monotonic() < end:
        try:
            async with ha.s.get(f"{HA}/api/", headers=ha.h) as r:
                if r.status == 200:
                    break
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(0.5)
    up = time.monotonic()
    back = None
    while time.monotonic() - up < 60:
        try:
            states = await ha.states()
        except aiohttp.ClientError:
            await asyncio.sleep(0.5)
            continue
        if ids and all(states.get(i, {}).get("state") in ("on", "off") for i in ids):
            back = time.monotonic() - up
            break
        await asyncio.sleep(0.25)
    # HA answers its API before it has set up integrations, so "back" also
    # counts HA's own bootstrap; HA's per-integration timing isolates ours.
    setup_s = None
    try:
        timings = await ha.ws({"type": "integration/setup_info"})
        setup_s = next((t["seconds"] for t in timings if t.get("domain") == "opennvr"), None)
    except (RuntimeError, aiohttp.ClientError):
        pass
    check("state is back after a restart",
          back is not None and setup_s is not None and setup_s <= 5,
          (f"entities back {back:.1f} s after HA's API answered; the integration's own "
           f"setup took {setup_s:.1f} s (target 5 s)") if back is not None and setup_s
          is not None else f"back={back} setup={setup_s}")


async def check_media(ha: HAClient, entry_id: str, cameras: list[dict]) -> None:
    """The media browser lists events, and a thumbnail and a clip play
    through Home Assistant's own URL (the proxy), not OpenNVR's."""
    thumb = clip = None
    for cam in cameras:
        page = await ha.ws({"type": "media_source/browse_media",
                            "media_content_id": f"media-source://opennvr/{entry_id}/events/"
                                                f"{cam['id']}/all/0"})
        events = [c for c in page.get("children", []) if c.get("can_play")]
        with_thumb = [c for c in events if c.get("thumbnail")]
        if with_thumb:
            thumb, clip = with_thumb[0]["thumbnail"], with_thumb[0]["media_content_id"]
            break
    if thumb is None:
        check("media browser plays through HA", False, "no event with a thumbnail")
        return
    async with ha.s.get(f"{HA}{thumb}", headers=ha.h) as r:
        img_ok = r.status == 200 and r.content_type.startswith("image/")
        img = f"thumbnail {r.status} {r.content_type}"
    resolved = await ha.ws({"type": "media_source/resolve_media", "media_content_id": clip})
    async with ha.s.get(f"{HA}{resolved['url']}") as r:          # HA-signed path
        head = await r.content.read(64 * 1024)
        clip_ok = r.status == 200 and len(head) > 1000
        vid = f"clip {r.status} {r.content_type} {len(head)}+ bytes"
    check("media browser plays through HA", img_ok and clip_ok, f"{img}; {vid}")

    # A search result's thumbnail is a relay URL: fetched with NO login, as a
    # phone's notification fetcher does; OpenNVR's signature is the credential.
    async with ha.s.post(f"{HA}/api/services/opennvr/search_events?return_response",
                         json={"limit": 10}, headers=ha.h) as r:
        rows = ((await r.json()).get("service_response") or {}).get("results", [])
    relay = next((row["thumbnail_url"] for row in rows
                  if str(row.get("thumbnail_url", "")).startswith("/api/opennvr/")), None)
    if relay is None:
        check("notification relay works without login", False, "no relay thumbnail")
        return
    async with ha.s.get(f"{HA}{relay}") as r:                    # no Authorization
        ok = r.status == 200 and r.content_type.startswith("image/")
        detail = f"{r.status} {r.content_type}"
    async with ha.s.get(f"{HA}{relay[:-4]}AAAA") as r:          # tampered signature
        detail += f"; tampered {r.status}"
        ok = ok and r.status == 404
    check("notification relay works without login", ok, detail)


async def check_card(s: aiohttp.ClientSession, ha: HAClient, entry_id: str) -> None:
    """A dashboard card's session: reads OpenNVR directly, cannot write, and
    the passthrough relays reading for a browser that can't reach OpenNVR."""
    sess = await ha.ws({"type": "opennvr/card_session"})
    auth = {"Authorization": f"Bearer {sess['token']}"}
    async with s.get(f"{NVR}/api/v1/cameras/", ssl=False, headers=auth) as r:
        read = r.status
    first = (sess.get("camera_ids") or [1])[0]
    async with s.put(f"{NVR}/api/v1/cameras/{first}", ssl=False, headers=auth,
                     json={"detection_enabled": True}) as r:
        write = r.status
    async with ha.s.get(f"{HA}{sess['passthrough']}/api/v1/cameras/",
                        headers=ha.h) as r:
        relayed = r.status
    check("card session reads, cannot write; passthrough relays",
          read == 200 and write == 403 and relayed == 200,
          f"read {read}, write {write}, passthrough {relayed}, scopes {sess.get('scopes')}")


async def check_correlation(s: aiohttp.ClientSession, ha: HAClient, entry_id: str) -> None:
    ents = await registry(ha, entry_id)
    switches = [e["entity_id"] for e in ents if e["entity_id"].startswith("switch.")
                and e["entity_id"].endswith("_object_detection")]
    states = await ha.states()
    target = next((i for i in switches if states.get(i, {}).get("state") == "on"), None)
    if target is None:
        check("command audited under HA's context id", False, "no detection switch that is on")
        return
    try:
        changed = await ha.post("/api/services/switch/turn_off", {"entity_id": target})
    finally:
        await asyncio.sleep(1)
        await ha.post("/api/services/switch/turn_on", {"entity_id": target})
    ctx = next((c["context"]["id"] for c in changed if c["entity_id"] == target), None)
    rows = await nvr(s, "GET", "/audit-logs/", params={"correlation_id": ctx or "-"})
    items = rows.get("items") or rows.get("logs") or []
    total = rows.get("total", len(items))
    check("command audited under HA's context id", bool(ctx) and total >= 1,
          f"{target}: correlation_id={ctx}, audit rows={total}")


async def check_revoke(s: aiohttp.ClientSession, ha: HAClient, entry_id: str,
                       token_id: int) -> None:
    await nvr(s, "DELETE", f"/api-tokens/{token_id}")
    want = f"token_revoked_{entry_id}"
    end = time.monotonic() + 120
    found = False
    while time.monotonic() < end and not found:
        issues = (await ha.ws({"type": "repairs/list_issues"}))["issues"]
        found = any(i["domain"] == "opennvr" and i["issue_id"] == want for i in issues)
        if not found:
            await asyncio.sleep(3)
    check("revoking the token raises a repair", found, want)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
