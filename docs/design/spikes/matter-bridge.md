# Spike: OpenNVR cameras over Matter (HA-602)

| | |
|---|---|
| **Status** | Spike complete (desk research; no prototype). Recommendation: **no-go for now**, revisit when two things hold (below) |
| **Date** | 2026-09-19 |
| **Design** | [home-assistant-integration.md](../home-assistant-integration.md) §10 |

## Question
The design asked this first: **may a Matter bridge expose cameras at all?**
If it may, can OpenNVR act as a Matter bridge (an Aggregator with Bridged Nodes)
that puts its cameras into Apple Home, Google Home, SmartThings and Home
Assistant through Matter 1.5, and what would that cost?

## What Matter 1.5 / 1.5.1 offers for cameras
From the CSA's release notes and trade coverage (sources below):

- **Device types:** Camera, Snapshot Camera, Intercom, Video Doorbell (a Camera
  plus a Doorbell), Audio Doorbell, and Floodlight and Chime.
- **Live video over WebRTC**, negotiated through Matter clusters (WebRTC
  Transport Provider/Requestor). Stream resources are allocated by the
  **Camera AV Stream Management** cluster, for live view, recording and
  analysis. End-to-end encryption uses SFrame. STUN/TURN allow remote viewing.
- **Recorded clips** are pushed by the camera (**Push AV Stream Transport**,
  CMAF ingest) to a store the controller chooses.
- PTZ, detection and privacy zones, snapshots, and multiple streams (1.5.1).

## Answers
1. **Bridged cameras: not settled from a primary source.** Trade coverage
   describes a Matter bridge on an NVR as the way existing IP cameras reach
   Matter ("Bridged Node" devices). I could not confirm from the Matter 1.5
   Device Library or the certification policy whether the Camera device types
   may be **certified** behind an Aggregator. That document is the CSA's, and
   the answer decides the project. **Owner action:** read the 1.5.1 Device
   Library (Camera and Bridged Node conformance), or ask the CSA. Until then,
   treat bridged cameras as uncertifiable.
2. **Controllers aren't ready.** SmartThings announced Matter camera
   support. Home Assistant's Matter server (matter.js, since the June 2026
   move) can view, stream and control Matter cameras, but as of May 2026 it
   called live view experimental. The article's own test camera streamed
   audio and snapshots but no video. Apple and Google have made no camera
   announcements in the sources found.
3. **OpenNVR would have to be a Matter camera device, not just translate.**
   Each bridged camera would have to implement, on OpenNVR's side:
   - WebRTC Transport Provider, answering offers from a Matter controller.
     MediaMTX speaks WHEP, not Matter's WebRTC signalling, so a signalling
     adapter is needed, plus SFrame if the controller requires it;
   - Camera AV Stream Management, with stream allocation mapped to MediaMTX
     paths;
   - Push AV Stream Transport, uploading CMAF clips to the controller's store.
     OpenNVR records to its own disk and has no push-to-external clip
     pipeline;
   - commissioning: a Matter fabric, device attestation (a DAC chain from a
     CSA-registered vendor ID for certified products), and operational
     certificates.

   matter.js implements the Matter stack in both roles. Whether its
   device-side camera clusters are complete isn't established here; the only
   reports found are controller-side.
4. **For Home Assistant users, Matter adds nothing.** The native integration
   already gives WebRTC live view, snapshots, PTZ, events and media, with
   OpenNVR's tokens and audit. Matter's value is reaching **Apple Home and
   Google Home**, which have no OpenNVR integration.

## Recommendation
**No-go now.** Revisit when **both** are true:

- the CSA confirms that bridged Camera device types can be certified (or the
  owner accepts an uncertified, "works with" bridge);
- at least two major controllers (for example Apple Home and Google Home) ship
  Matter camera viewing, so there is an audience beyond Home Assistant.

If it becomes a go, the likely shape is:
- a **separate sidecar** in its own compose profile, built on matter.js;
- one Aggregator with a Bridged Node per camera, bound to an OpenNVR API
  token as the MQTT bridge is;
- live view first (WebRTC signalling adapted to MediaMTX's WHEP), then
  snapshots, then motion/occupancy as Matter sensor device types (**these
  are bridgeable today**);
- clip push last.

Estimated effort for live view alone: **L (4+ sessions)**, plus certification
costs.

**A cheaper step, available now:** expose occupancy, motion and contact-style
states (not video) as **bridged Matter sensors**. Those device types are
long-standing and bridgeable, and they would put OpenNVR's detections into
Apple and Google automations. It is worth its own spike if Apple/Google
reach is wanted before Matter camera support matures.

## Sources
- [CSA: Matter 1.5 introduces cameras, closures and energy management](https://csa-iot.org/newsroom/matter-1-5-introduces-cameras-closures-and-enhanced-energy-management-capabilities/)
- [CSA: Matter 1.5.1, camera performance and device flexibility](https://csa-iot.org/newsroom/matter-1-5-1-enhancing-camera-performance-and-expanding-device-flexibility/)
- [Matter Alpha: What is a Matter camera, and how does it work?](https://www.matteralpha.com/explainer/what-is-a-matter-camera-and-how-does-it-work)
- [Matter Alpha: OHF Matter.js Server now supports cameras with 1.5.1 (2026-05-19)](https://www.matteralpha.com/industry-news/ohf-matter-js-server-now-supports-cameras-with-1-5-1-spec)
- [Home Assistant blog: the Matter upgrade you've been waiting for (2026-06-23)](https://www.home-assistant.io/blog/2026/06/23/the-matter-upgrade-youve-been-waiting-for/)
- [Matter Alpha: SmartThings announces Matter 1.5 camera support](https://www.matteralpha.com/industry-news/smartthings-announces-Matter-1-5-camera-support-update)
