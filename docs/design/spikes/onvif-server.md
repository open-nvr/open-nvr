# Spike: OpenNVR as an ONVIF device (HA-601)

| | |
|---|---|
| **Status** | Spike complete. Recommendation: **go**, as an opt-in sidecar, after integration 1.0 |
| **Date** | 2026-09-19 |
| **Design** | [home-assistant-integration.md](../home-assistant-integration.md) §10, scenario S9 |
| **Prototype** | [`scripts/spikes/onvif_server.py`](../../../scripts/spikes/onvif_server.py) (not product code) |
| **Check** | [`scripts/spikes/onvif-ha-check.ps1`](../../../scripts/spikes/onvif-ha-check.ps1) |

## Question
Can OpenNVR present itself as an ONVIF device, so that VMSs, NVRs and Home
Assistant's own `onvif` integration add its cameras and its AI events without
anything written for OpenNVR? What would the product version cost?

## What was built
A standalone ONVIF device of about 400 lines (aiohttp, no ONVIF library) in
front of a running OpenNVR. It reads OpenNVR with a read-only API token
(`cameras.view`, `live.view`) and never writes. It presents:

- **one ONVIF device for the site**, with **one media profile per camera**;
- **device service:** `GetSystemDateAndTime` (unauthenticated),
  `GetServices`, `GetCapabilities`, `GetDeviceInformation`,
  `GetNetworkInterfaces` (a stable MAC derived from the site), `GetScopes`;
- **media service (Profile S):** `GetServiceCapabilities`, `GetProfiles` (one
  H264 profile per camera), `GetStreamUri` (MediaMTX RTSPS with an OpenNVR
  stream JWT), `GetSnapshotUri` (served through OpenNVR's `/snapshot`, HTTP
  Basic);
- **event service (PullPoint):** `CreatePullPointSubscription`,
  `PullMessages` (a 5-second long poll), `SetSynchronizationPoint`, `Renew`,
  `Unsubscribe`, `GetEventProperties`. Events are fed from OpenNVR's
  `/live-state`, once a second, and mapped per camera to:

  | OpenNVR fact | ONVIF topic | Home Assistant shows |
  |---|---|---|
  | motion | `tns1:RuleEngine/CellMotionDetector/Motion` | Cell Motion Detection |
  | a person present | `tns1:RuleEngine/MyRuleDetector/PeopleDetect` | Person Detection |
  | a vehicle present | `tns1:RuleEngine/MyRuleDetector/VehicleDetect` | Vehicle Detection |

- **WS-UsernameToken** authentication (digest or text) on every call except
  the time query.

## Result against Home Assistant 2026.9.2
A fresh Home Assistant with its stock `onvif` integration (added by hand: host,
port, user, password) against the prototype and the live dev stack:

| Check | Result |
|---|---|
| Config flow accepts OpenNVR as an ONVIF device | **pass** |
| One camera entity per OpenNVR camera | **pass** (5 of 5) |
| Snapshot through Home Assistant | **pass** (85 KB JPEG) |
| Live video through HA's `stream` component, from `GetStreamUri` | **pass** (HLS, `avc1.64001f`) |
| PullPoint events become binary sensors with live states | **pass** (15 sensors: motion, person, vehicle per camera) |

The operations Home Assistant called are:
- `GetSystemDateAndTime`, `GetServices`, `GetCapabilities`, `GetDeviceInformation`, `GetNetworkInterfaces`;
- `GetServiceCapabilities`, `GetProfiles`, `GetStreamUri`, `GetSnapshotUri`;
- `CreatePullPointSubscription`, `SetSynchronizationPoint`, `PullMessages`, `Renew`;
- `Subscribe` (WS-BaseNotification webhook). The prototype refuses it, and Home Assistant falls back to PullPoint by itself.

## Findings
1. **It works with a small surface.** About 15 operations cover Home Assistant
   completely. Zeep (Home Assistant's ONVIF client) accepted hand-written
   responses without an ONVIF library on the server side.
2. **Stream credentials are the hard problem.** ONVIF clients cache the
   stream URI and put *their own* username and password into it. OpenNVR's
   MediaMTX authenticates with a short-lived JWT (1 h) carried in the query.
   It worked in the spike because the JWT was fresh. A client that keeps the
   URI (Home Assistant does, for the life of the entry) fails an hour later.
   Options for the product:
   - **a.** MediaMTX `authMethod: http` pointed at core, which accepts the ONVIF
     credentials for read on the ONVIF paths. That changes MediaMTX's
     security model for everything, so it is the riskiest option.
   - **b.** A second MediaMTX path set (`onvif-cam-N`) sourced from `cam-N`,
     with `authInternalUsers` holding the one ONVIF user, read-only. It is
     contained, but MediaMTX has one `authMethod` for the whole server, so it
     needs a second MediaMTX instance, or a small RTSP relay in the sidecar.
   - **c.** Long-lived stream JWTs minted per ONVIF credential. They are
     revocable only by rotating keys, so they are **rejected**.

   Recommendation: **b**, with a second MediaMTX instance in the sidecar's
   compose profile. It uses no new core code, and turning the profile off
   removes it entirely.
3. **H264 only.** Home Assistant ignores non-H264 profiles, and many ONVIF
   clients do. OpenNVR passes camera streams through unchanged, so an H265
   camera would be invisible there. The product must report each camera's
   real codec (from MediaMTX's track list) and offer H264 only when it is
   H264. Transcoding is out of scope; it costs too much CPU on the target
   hardware.
4. **Events map well, but only as states.** ONVIF event topics that clients
   parse are about presence (motion, people, vehicles, line crossing,
   intrusion). OpenNVR's richer facts (plates, faces, zones, severity-graded
   app alerts) have no standard topic that clients understand. Profile M's
   metadata stream (`tt:MetadataStream` over RTSP) could carry boxes and
   labels, but consumer clients don't read it. For a VMS (scenario S9), the
   useful additions are `tns1:RuleEngine/LineDetector/Crossed` and
   `tns1:RuleEngine/FieldDetector/ObjectsInside` for zones.
5. **Discovery.** WS-Discovery needs UDP multicast on port 3702. It has the
   same Docker Desktop limit as mDNS (see HA-117): Linux hosts only, and
   manual host and port entry everywhere else. The spike used manual entry.
6. **PTZ is straightforward.** Home Assistant calls `GetPresets`, `GotoPreset`,
   `ContinuousMove`, `RelativeMove`, `AbsoluteMove` and `Stop`. OpenNVR already
   has presets and continuous move/stop (HA-107). Relative and absolute moves
   would be refused as unsupported. The spike didn't implement PTZ.
7. **Security posture.** An ONVIF device is a second front door with
   long-lived shared credentials, and WS-UsernameToken digests are SHA-1
   based. It must be:
   - **off by default**;
   - bound to an API token that sets what it may show;
   - its own compose profile;
   - LAN-only;
   - audited on every stream and snapshot.

   It must never be on by default in a hardened deployment.

## Recommendation
**Go**, scoped as an **opt-in sidecar** (`COMPOSE_PROFILES=onvif`) after
integration 1.0, for VMS interoperability (scenario S9). It isn't for Home
Assistant users; the native integration gives them far more. Estimated
effort is **M (2–3 sessions)**:

- the sidecar, grown from the prototype: PTZ, zones as `FieldDetector`,
  line crossing, `Subscribe` (optional), faults per the ONVIF error codes;
- option **b** for stream credentials (a second MediaMTX, read-only ONVIF user);
- the codec check;
- tests against Home Assistant's `onvif` integration (as here) and against a
  second client, such as ONVIF Device Manager or `onvif-zeep` scripted checks.

The official ONVIF conformance test tool needs ONVIF membership, which is
the owner's decision. Without it, OpenNVR may say "works with ONVIF clients",
not "ONVIF conformant".

**Not recommended:** Profile G (recording search and replay over ONVIF). It
is large, and few clients use it.
