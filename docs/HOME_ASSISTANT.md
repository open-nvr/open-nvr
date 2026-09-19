# Home Assistant: setup

Home Assistant talks to OpenNVR over the same HTTPS address your browser uses
(nginx on port 443). This page lists what to open, and why, when Home
Assistant runs on **another machine** on your LAN. When it runs on the same
host, the defaults already work.

## Two ways in

| | OpenNVR integration (recommended) | MQTT discovery |
|---|---|---|
| Install | the `opennvr` integration in Home Assistant (HACS) | nothing in Home Assistant beyond its MQTT integration |
| Needs | an OpenNVR API token | an MQTT broker both can reach, and an API token |
| Live video, snapshots, media browser, actions, notifications with media, Assist | yes | no |
| Sensors, switches, selects, buttons, numbers, events, site mode | yes | yes (the same entities) |

Use **one of the two**, not both: every entity would appear twice. The
integration raises a repair (*"OpenNVR entities may appear twice"*) if
it sees both running. The older `examples/home-assistant-relay` app is
deprecated in favour of either.

### MQTT discovery
In OpenNVR, go to *Settings > Integrations* and add an **MQTT** integration:

- **Broker URL**: `mqtt://host:1883`, or `mqtts://host:8883` for TLS, plus a
  username and password if the broker needs them;
- **Home Assistant discovery**: on;
- **Act as API token**: the token whose scopes and cameras decide what is
  published and which commands Home Assistant may send. Create a dedicated
  token under *Settings > API Tokens*: `cameras.view`, and `settings.view`
  for site entities; add `cameras.manage` (switches), `ptz.control`,
  `settings.manage` (site mode as an alarm panel; without it, a read-only
  sensor) only if Home Assistant should control them. A token limited to
  certain addresses can't be used: commands come from the broker;
- **Discovery prefix**: Home Assistant's, `homeassistant` unless you
  changed it.

*Test* publishes one message. Once saved, Home Assistant's MQTT integration
lists a device for the server and one for each camera, zone and app.

What goes where (`<site>` is the start of the site id):

| Topic | Content |
|---|---|
| `homeassistant/device/opennvr_<site>_<device>/config` | discovery, retained |
| `opennvr/<site>/status` | `online`, or `offline` (the broker's Last Will) |
| `opennvr/<site>/<key>/state`, `.../attributes` | the value and its attributes, retained |
| `opennvr/<site>/<key>/set` | commands from Home Assistant, run as the token and audited as `mqtt:<integration name>` |
| `opennvr/<site>/<key>/event` | events, as CloudEvents 1.0 JSON |
| `opennvr/alerts` | every alert, as with the webhook integrations |

**The broker is the trust boundary.** Anyone who can publish to it can send
every command the token allows, so give the broker usernames and passwords
(and ACLs, if it has them), and give the token no more than Home Assistant
needs. Retained command messages are ignored, and commands are rate-limited.

Revoking the token takes the devices offline within a minute. Deleting the
integration, turning discovery off, or moving it to another broker, prefix
or token removes its devices from Home Assistant and clears what it left on
the broker. That needs the broker to be reachable at that moment; if it
isn't, delete the devices in Home Assistant by hand.

## What Home Assistant uses

| Purpose | Path | Default | To reach it from the LAN |
|---|---|---|---|
| API, events WebSocket, signed media | `https://<host>/api/v1/...` | open on all interfaces (`NGINX_BIND_HOST=0.0.0.0`) | nothing to do |
| Live video (WebRTC over WHEP) signalling | `https://<host>/webrtc/...` | through nginx | nothing to do |
| Live video media (ICE) | UDP/TCP `8189` | published on all interfaces | set `MEDIAMTX_WEBRTC_HOSTS` (below) |
| RTSPS stream for HA's `stream` component (optional) | `rtsps://<host>:8322/...` | **loopback only** | set `RTSPS_BIND_HOST` (below) |
| Discovery (optional) | mDNS `_opennvr._tcp` | off | `COMPOSE_PROFILES=mdns`, Linux only |

## Settings (in `.env`)

### `MEDIAMTX_WEBRTC_HOSTS`: live video from another machine
WebRTC offers the addresses in this list as ICE candidates. Without the host's
LAN address, Home Assistant (or a phone) on another machine can signal but
never receives video. Put the address Home Assistant uses to reach this host;
separate several with commas (multiple NICs, a VPN):

```
MEDIAMTX_WEBRTC_HOSTS=192.168.1.100
```

`start.sh` / `start.ps1` fill this from the detected LAN address. Set it by
hand only if detection picked the wrong interface.

### `RTSPS_BIND_HOST`: RTSPS for Home Assistant's stream component (optional)
Home Assistant plays cameras over WebRTC and needs nothing else. RTSPS is only
for recording in Home Assistant or for other RTSP clients. Opening it:

```
RTSPS_BIND_HOST=0.0.0.0          # or one LAN address
MEDIAMTX_EXTERNAL_RTSPS_URL=rtsps://192.168.1.100:8322
```

Each stream still needs an OpenNVR-signed token, so opening the port does not
make the streams public. The certificate is OpenNVR's own; clients must
accept it or trust the OpenNVR CA.

### `CORS_ORIGINS`: only for the dashboard card
The integration itself calls the API server-to-server and needs no CORS. A
Lovelace card that talks to OpenNVR from the browser does. Add Home
Assistant's origin:

```
CORS_ORIGINS=http://localhost:5173,https://homeassistant.local:8123
```

## Discovery (mDNS), optional and Linux only
With `COMPOSE_PROFILES=mdns`, the `opennvr-mdns` service announces
`_opennvr._tcp` so Home Assistant shows "OpenNVR discovered". It announces
only public facts: the version (from `/health`), the HTTPS port and the API
path. Setup still needs an API token.

It needs `network_mode: host`, so it works **only on Linux hosts**. Docker
Desktop on Windows and macOS keeps multicast inside its VM; there, enter the
URL (`https://<host>`) in Home Assistant. Manual entry is the supported default
everywhere.

## Tokens
Create a token for Home Assistant under **Settings > API Tokens**. The form
starts with what Home Assistant needs (cameras, live video, recordings, alerts,
system info). Add `ptz.control`, `events.create` or `settings.manage` (arming)
only if you want Home Assistant to do those things. Limit the token to cameras
or to Home Assistant's address if you like; revoking it stops Home Assistant
at once.

Home Assistant cannot run without `settings.view` (system info) and
`cameras.view`. It reads what the token may do from `GET /api/v1/system/info`
(the `caller` block: effective scopes, cameras, expiry), so its setup names any
scope that is missing.
