# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""HA-601 spike: a minimal ONVIF device in front of OpenNVR. NOT product code.

One ONVIF device for the site; one media profile per camera. Answers the
Profile S subset Home Assistant's ``onvif`` integration calls, and a
PullPoint event service (the Profile M/T style most clients use) fed from
OpenNVR's ``/live-state``: motion, people and vehicles per camera.

It reads OpenNVR with an API token (cameras.view, live.view) and never
writes. Findings are in docs/design/spikes/onvif-server.md.

    NVR_URL=https://localhost NVR_TOKEN=onvr_... ONVIF_USER=ha ONVIF_PASS=secret \\
    PUBLIC_HOST=host.docker.internal RTSP_BASE=rtsps://host.docker.internal:8322 \\
    uv run --no-project --with aiohttp python scripts/spikes/onvif_server.py
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from xml.sax.saxutils import escape

import aiohttp
from aiohttp import web

NVR_URL = os.environ.get("NVR_URL", "https://localhost").rstrip("/")
NVR_TOKEN = os.environ["NVR_TOKEN"]
USER = os.environ.get("ONVIF_USER", "ha")
PASS = os.environ.get("ONVIF_PASS", "secret")
PORT = int(os.environ.get("PORT", "8099"))
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "host.docker.internal")
RTSP_BASE = os.environ.get("RTSP_BASE", "rtsps://host.docker.internal:8322")
BASE = f"http://{PUBLIC_HOST}:{PORT}"

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "tns1": "http://www.onvif.org/ver10/topics",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}
CALLS: list[str] = []          # every operation asked for, for the report


def now() -> datetime:
    return datetime.now(UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def envelope(body: str) -> str:
    decl = " ".join(f'xmlns:{k}="{v}"' for k, v in NS.items())
    return f'<?xml version="1.0" encoding="UTF-8"?><s:Envelope {decl}><s:Body>{body}</s:Body></s:Envelope>'


def fault(reason: str, code: str = "ter:ActionNotSupported") -> web.Response:
    body = (f'<s:Fault><s:Code><s:Value>s:Sender</s:Value><s:Subcode><s:Value xmlns:ter='
            f'"http://www.onvif.org/ver10/error">{code}</s:Value></s:Subcode></s:Code>'
            f'<s:Reason><s:Text xml:lang="en">{escape(reason)}</s:Text></s:Reason></s:Fault>')
    return web.Response(text=envelope(body), status=400, content_type="application/soap+xml")


def ok(body: str) -> web.Response:
    return web.Response(text=envelope(body), content_type="application/soap+xml")


# ── authentication: WS-UsernameToken (digest or text) ─────────────────────

def authenticated(root: ET.Element) -> bool:
    tok = root.find(".//wsse:UsernameToken", NS)
    if tok is None:
        return False
    user = tok.findtext("wsse:Username", default="", namespaces=NS)
    pw = tok.find("wsse:Password", NS)
    if user != USER or pw is None:
        return False
    if (pw.get("Type") or "").endswith("#PasswordText"):
        return pw.text == PASS
    nonce = base64.b64decode(tok.findtext("wsse:Nonce", default="", namespaces=NS) or "")
    created = tok.findtext("wsu:Created", default="", namespaces=NS)
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + PASS.encode())
                              .digest()).decode()
    return digest == (pw.text or "")


# ── OpenNVR ───────────────────────────────────────────────────────────────

class NVR:
    def __init__(self) -> None:
        self.session: aiohttp.ClientSession | None = None
        self.cameras: list[dict] = []
        self.live: dict[int, dict] = {}

    async def get(self, path: str):
        assert self.session
        async with self.session.get(f"{NVR_URL}/api/v1{path}", ssl=False,
                                    headers={"Authorization": f"Bearer {NVR_TOKEN}"}) as r:
            r.raise_for_status()
            if r.content_type == "application/json":
                return await r.json()
            return await r.read()

    async def refresh(self) -> None:
        data = await self.get("/cameras/?limit=200")
        items = data.get("cameras", []) if isinstance(data, dict) else data
        self.cameras = [c for c in items if c.get("is_active", True)]


nvr = NVR()


# ── device management ─────────────────────────────────────────────────────

def mac() -> str:
    h = hashlib.sha1(NVR_URL.encode()).hexdigest()
    return "02:" + ":".join(h[i:i + 2] for i in range(0, 10, 2))


def op_GetSystemDateAndTime(_):
    t = now()
    return ok(f"""<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>
<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>
<tt:TimeZone><tt:TZ>UTC</tt:TZ></tt:TimeZone><tt:UTCDateTime>
<tt:Time><tt:Hour>{t.hour}</tt:Hour><tt:Minute>{t.minute}</tt:Minute><tt:Second>{t.second}</tt:Second></tt:Time>
<tt:Date><tt:Year>{t.year}</tt:Year><tt:Month>{t.month}</tt:Month><tt:Day>{t.day}</tt:Day></tt:Date>
</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>""")


def _service(ns: str, path: str) -> str:
    return (f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{BASE}{path}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>60</tt:Minor></tds:Version></tds:Service>")


def op_GetServices(_):
    return ok("<tds:GetServicesResponse>"
              + _service(NS["tds"], "/onvif/device_service")
              + _service(NS["trt"], "/onvif/media_service")
              + _service(NS["tev"], "/onvif/events_service")
              + "</tds:GetServicesResponse>")


def op_GetCapabilities(_):
    return ok(f"""<tds:GetCapabilitiesResponse><tds:Capabilities>
<tt:Device><tt:XAddr>{BASE}/onvif/device_service</tt:XAddr></tt:Device>
<tt:Events><tt:XAddr>{BASE}/onvif/events_service</tt:XAddr>
<tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>
<tt:WSPullPointSupport>true</tt:WSPullPointSupport>
<tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport></tt:Events>
<tt:Media><tt:XAddr>{BASE}/onvif/media_service</tt:XAddr><tt:StreamingCapabilities>
<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>
</tt:StreamingCapabilities></tt:Media></tds:Capabilities></tds:GetCapabilitiesResponse>""")


def op_GetDeviceInformation(_):
    return ok("<tds:GetDeviceInformationResponse><tds:Manufacturer>OpenNVR</tds:Manufacturer>"
              "<tds:Model>OpenNVR ONVIF spike</tds:Model><tds:FirmwareVersion>0.0.1</tds:FirmwareVersion>"
              f"<tds:SerialNumber>{mac().replace(':', '')}</tds:SerialNumber>"
              "<tds:HardwareId>opennvr</tds:HardwareId></tds:GetDeviceInformationResponse>")


def op_GetNetworkInterfaces(_):
    return ok(f"""<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token="eth0">
<tt:Enabled>true</tt:Enabled><tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>{mac()}</tt:HwAddress>
<tt:MTU>1500</tt:MTU></tt:Info><tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>
<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>""")


def op_GetScopes(_):
    return ok("<tds:GetScopesResponse><tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef>"
              "<tt:ScopeItem>onvif://www.onvif.org/name/OpenNVR</tt:ScopeItem></tds:Scopes>"
              "</tds:GetScopesResponse>")


# ── media (Profile S) ─────────────────────────────────────────────────────

def op_GetServiceCapabilities(_):
    return ok('<trt:GetServiceCapabilitiesResponse><trt:Capabilities SnapshotUri="true" '
              'Rotation="false" VideoSourceMode="false" OSD="false">'
              f'<trt:ProfileCapabilities MaximumNumberOfProfiles="{max(len(nvr.cameras), 1)}"/>'
              '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true"/>'
              '</trt:Capabilities></trt:GetServiceCapabilitiesResponse>')


def _profile(cam: dict) -> str:
    cid = cam["id"]
    return f"""<trt:Profiles token="cam{cid}" fixed="true"><tt:Name>{escape(cam.get('name') or f'Camera {cid}')}</tt:Name>
<tt:VideoSourceConfiguration token="vsc{cid}"><tt:Name>vsc{cid}</tt:Name><tt:UseCount>1</tt:UseCount>
<tt:SourceToken>vs{cid}</tt:SourceToken><tt:Bounds x="0" y="0" width="1920" height="1080"/></tt:VideoSourceConfiguration>
<tt:VideoEncoderConfiguration token="vec{cid}"><tt:Name>vec{cid}</tt:Name><tt:UseCount>1</tt:UseCount>
<tt:Encoding>H264</tt:Encoding><tt:Resolution><tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
<tt:Quality>5</tt:Quality><tt:RateControl><tt:FrameRateLimit>15</tt:FrameRateLimit>
<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>4096</tt:BitrateLimit></tt:RateControl>
<tt:H264><tt:GovLength>30</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>
<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>
<tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>
<tt:SessionTimeout>PT60S</tt:SessionTimeout></tt:VideoEncoderConfiguration></trt:Profiles>"""


def op_GetProfiles(_):
    return ok("<trt:GetProfilesResponse>" + "".join(_profile(c) for c in nvr.cameras)
              + "</trt:GetProfilesResponse>")


def _media_uri(uri: str) -> str:
    return (f"<trt:MediaUri><tt:Uri>{escape(uri)}</tt:Uri><tt:InvalidAfterConnect>false"
            "</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri>")


def _profile_camera(root: ET.Element) -> int | None:
    token = root.findtext(".//trt:ProfileToken", default="", namespaces=NS)
    return int(token[3:]) if token.startswith("cam") and token[3:].isdigit() else None


async def op_GetStreamUri(root):
    cid = _profile_camera(root)
    if cid is None:
        return fault("no such profile", "ter:NoProfile")
    info = await nvr.get(f"/streams/{cid}/info")
    # MediaMTX takes its JWT in the query; it expires (1 h), and ONVIF
    # clients cache this URI: see the report.
    return ok(f"<trt:GetStreamUriResponse>{_media_uri(f'{RTSP_BASE}/cam-{cid}?jwt={info['token']}')}"
              "</trt:GetStreamUriResponse>")


def op_GetSnapshotUri(root):
    cid = _profile_camera(root)
    if cid is None:
        return fault("no such profile", "ter:NoProfile")
    return ok(f"<trt:GetSnapshotUriResponse>{_media_uri(f'{BASE}/snapshot/{cid}.jpg')}"
              "</trt:GetSnapshotUriResponse>")


async def snapshot(request: web.Request) -> web.Response:
    """HTTP Basic with the ONVIF credentials (clients try Digest, then Basic)."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic ") or base64.b64decode(auth[6:]).decode() != f"{USER}:{PASS}":
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="onvif"'})
    CALLS.append("HTTP snapshot")
    jpeg = await nvr.get(f"/cameras/{int(request.match_info['cid'])}/snapshot")
    return web.Response(body=jpeg, content_type="image/jpeg")


# ── events: PullPoint ────────────────────────────────────────────────────

class Subscription:
    def __init__(self) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self.expires = now() + timedelta(seconds=60)
        self.last: dict[tuple[int, str], bool] = {}


SUBS: dict[str, Subscription] = {}
TOPICS = {  # OpenNVR fact -> (ONVIF topic, source item name)
    "motion": ("tns1:RuleEngine/CellMotionDetector/Motion", "VideoSourceConfigurationToken"),
    "person": ("tns1:RuleEngine/MyRuleDetector/PeopleDetect", "Source"),
    "vehicle": ("tns1:RuleEngine/MyRuleDetector/VehicleDetect", "Source"),
}
VEHICLES = {"car", "truck", "bus", "motorcycle", "bicycle"}


def _message(cid: int, fact: str, value: bool, operation: str) -> str:
    topic, source = TOPICS[fact]
    data = "IsMotion" if fact == "motion" else "State"
    return f"""<wsnt:NotificationMessage><wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">{topic}</wsnt:Topic>
<wsnt:Message><tt:Message UtcTime="{iso(now())}" PropertyOperation="{operation}"><tt:Source>
<tt:SimpleItem Name="{source}" Value="vsc{cid}"/></tt:Source><tt:Data>
<tt:SimpleItem Name="{data}" Value="{'true' if value else 'false'}"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>"""


def _facts(state: dict) -> dict[str, bool]:
    objects = state.get("objects") or {}
    active = {k for k, v in objects.items() if (v or {}).get("total")}
    return {"motion": bool(state.get("motion")), "person": "person" in active,
            "vehicle": bool(active & VEHICLES)}


def _emit(sub: Subscription, initial: bool) -> None:
    for cid, state in nvr.live.items():
        for fact, value in _facts(state).items():
            if initial or sub.last.get((cid, fact)) != value:
                with contextlib.suppress(asyncio.QueueFull):
                    sub.queue.put_nowait(_message(cid, fact, value,
                                                  "Initialized" if initial else "Changed"))
                sub.last[(cid, fact)] = value


async def poll_live() -> None:
    while True:
        try:
            data = await nvr.get("/live-state")
            nvr.live = {c["camera_id"]: c for c in data.get("cameras", [])}
            for sub in list(SUBS.values()):
                if sub.expires < now():
                    SUBS.pop(sub.id, None)
                else:
                    _emit(sub, initial=False)
        except Exception as exc:  # the spike keeps going
            print("live-state:", exc)
        await asyncio.sleep(1)


def _times(sub: Subscription) -> str:
    return (f"<wsnt:CurrentTime>{iso(now())}</wsnt:CurrentTime>"
            f"<wsnt:TerminationTime>{iso(sub.expires)}</wsnt:TerminationTime>")


def op_CreatePullPointSubscription(root):
    sub = Subscription()
    SUBS[sub.id] = sub
    return ok(f"""<tev:CreatePullPointSubscriptionResponse><tev:SubscriptionReference>
<wsa:Address>{BASE}/onvif/pullpoint/{sub.id}</wsa:Address></tev:SubscriptionReference>
<wsnt:CurrentTime>{iso(now())}</wsnt:CurrentTime><wsnt:TerminationTime>{iso(sub.expires)}</wsnt:TerminationTime>
</tev:CreatePullPointSubscriptionResponse>""")


def op_GetEventProperties(_):
    return ok("<tev:GetEventPropertiesResponse><tev:TopicNamespaceLocation>"
              "http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>"
              "<wsnt:FixedTopicSet>true</wsnt:FixedTopicSet><wstop:TopicSet "
              'xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"/>'
              "<wsnt:TopicExpressionDialect>http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
              "</wsnt:TopicExpressionDialect><tev:MessageContentFilterDialect>"
              "http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter"
              "</tev:MessageContentFilterDialect><tev:MessageContentSchemaLocation>"
              "http://www.onvif.org/onvif/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation>"
              "</tev:GetEventPropertiesResponse>")


def _sub_of(request: web.Request) -> Subscription | None:
    return SUBS.get(request.match_info.get("sid", ""))


def op_SetSynchronizationPoint(root, request):
    if (sub := _sub_of(request)) is not None:
        _emit(sub, initial=True)
    return ok("<tev:SetSynchronizationPointResponse/>")


async def op_PullMessages(root, request):
    sub = _sub_of(request)
    if sub is None:
        return fault("unknown subscription", "ter:InvalidArgVal")
    limit = int(root.findtext(".//tev:MessageLimit", default="100", namespaces=NS) or 100)
    msgs: list[str] = []
    try:
        msgs.append(await asyncio.wait_for(sub.queue.get(), 5))
        while len(msgs) < limit and not sub.queue.empty():
            msgs.append(sub.queue.get_nowait())
    except TimeoutError:
        pass
    return ok(f"<tev:PullMessagesResponse><tev:CurrentTime>{iso(now())}</tev:CurrentTime>"
              f"<tev:TerminationTime>{iso(sub.expires)}</tev:TerminationTime>{''.join(msgs)}"
              "</tev:PullMessagesResponse>")


def op_Renew(root, request):
    sub = _sub_of(request)
    if sub is None:
        return fault("unknown subscription", "ter:InvalidArgVal")
    sub.expires = now() + timedelta(seconds=60)
    return ok(f"<wsnt:RenewResponse>{_times(sub)}</wsnt:RenewResponse>")


def op_Unsubscribe(root, request):
    SUBS.pop(request.match_info.get("sid", ""), None)
    return ok("<wsnt:UnsubscribeResponse/>")


# ── dispatch ─────────────────────────────────────────────────────────────

UNAUTHENTICATED = {"GetSystemDateAndTime"}
NEEDS_REQUEST = {"SetSynchronizationPoint", "PullMessages", "Renew", "Unsubscribe"}


async def soap(request: web.Request) -> web.Response:
    root = ET.fromstring(await request.read())
    body = root.find("s:Body", NS)
    if body is None or not len(body):
        return fault("no body", "ter:InvalidArgVal")
    op = body[0].tag.rsplit("}", 1)[-1]
    CALLS.append(op)
    if op not in UNAUTHENTICATED and not authenticated(root):
        if os.environ.get("SPIKE_TRACE"):
            print(op, "refused: not authorized", flush=True)
        return fault("Sender not authorized", "ter:NotAuthorized")
    handler = globals().get(f"op_{op}")
    if handler is None:
        print("unsupported:", op)
        return fault(f"{op} is not supported")
    result = handler(root, request) if op in NEEDS_REQUEST else handler(root)
    response = await result if asyncio.iscoroutine(result) else result
    if os.environ.get("SPIKE_TRACE"):
        print(op, response.status, flush=True)
    return response


async def calls(_request: web.Request) -> web.Response:
    return web.json_response(CALLS)


async def main() -> None:
    nvr.session = aiohttp.ClientSession()
    await nvr.refresh()
    print(f"{len(nvr.cameras)} cameras; ONVIF at {BASE}/onvif/device_service")
    app = web.Application()
    app.router.add_post("/onvif/pullpoint/{sid}", soap)
    app.router.add_post("/onvif/{service}", soap)
    app.router.add_get("/snapshot/{cid}.jpg", snapshot)
    app.router.add_get("/spike/calls", calls)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await poll_live()


if __name__ == "__main__":
    asyncio.run(main())

