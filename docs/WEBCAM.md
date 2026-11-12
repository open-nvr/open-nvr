# Webcams as cameras

OpenNVR can record a webcam like any IP camera: live view, recording,
playback, snapshots and detection all work the same. Add one from
**Cameras → Add Camera → Manual → Webcam**. There are two kinds:

| | In this browser | On the OpenNVR server |
|---|---|---|
| What | The camera of the laptop, PC or phone you're using | A webcam plugged into the computer OpenNVR runs on |
| How it streams | From the browser, over WebRTC (WHIP) | The [host agent](HOST_AGENT.md), with ffmpeg |
| Records | Only while the publishing tab is open | Around the clock, also after restarts and re-plugging |
| Setup | Nothing | The host agent and ffmpeg, once per machine |

## How it works

An IP camera is *pulled*: MediaMTX connects to its RTSP URL. A webcam is
*pushed*: something publishes into the camera's MediaMTX path (`cam-<id>`).
That path is created with `source: publisher` when the camera is added. Everything that
reads the path (recording, Live View, the detection pipeline) works as for any
camera. A webcam has no IP address, stream URL or credentials, and the
IP-camera features (ONVIF, PTZ, camera settings, substreams, the RTSPS probe)
don't apply to it.

Each publisher gets a **publish token** that may only publish, only to that
one camera's path, and expires in 5 minutes. A new one is fetched on every
(re)connect. MediaMTX checks the token only when the publisher connects, so a
long session outlives it.

The video is H.264 at up to 720p: 15 fps from a browser, 25 fps from a USB webcam on the server. A browser webcam's
sound is **off** unless you switch it on; a USB webcam records no sound.
Recording a room's conversations may need consent where you are.

## In this browser

1. **Add Camera → Manual → Webcam**, source **This browser**. Allow camera
   access when the browser asks.
2. Pick the camera from the list (it is previewed next to it), set the
   quality and sound, name it, then click **Add webcam**.
3. The dialog shows the camera's own stream coming up, then **Done**. It keeps
   publishing while you use OpenNVR in the same tab; the header shows it, with
   a Stop button. Closing or reloading the tab stops it.
4. Keep the device awake. The page holds a screen wake lock and reconnects by
   itself after a network drop or a server restart.

Browsers only open the camera on a secure page. Use OpenNVR's `https://`
address and accept the self-signed certificate warning once. `http://` won't
work, except on `localhost`.

The page can be reopened at any time from the **Webcam** button on the camera's
row in **Cameras**, or at `/cameras/<id>/publish`. Only users who can manage
cameras can publish.

## On the OpenNVR server (USB webcam)

### Linux: MediaMTX serves it, nothing to install

On Linux the webcam's device is passed into the MediaMTX container and
MediaMTX runs the capture itself (`runOnInit`, restarted when the camera is
unplugged and plugged back). No host agent, no sudo, nothing outside Docker.

1. `WEBCAMS=on` in `.env` (the installer asks), then `./start.sh up`. This
   adds `docker-compose.webcams.yml`: MediaMTX gets the host's video devices
   and an ffmpeg, core gets the stable device names (`/dev/v4l/by-id`) to
   list them. `VIDEO_GID` is filled in from the host's `video` group.
2. **Add Camera → Manual → Webcam**, source **OpenNVR server**. The webcams
   plugged into the machine are listed by name. Pick one, name the camera,
   click **Add webcam**. It is live within a few seconds.

Like an IP camera with a substream, the webcam gets two paths: `cam-<id>`
(the capture, up to 720p) for recording and evidence, and `cam-<id>-sub`
(640 wide, 10 fps) for detection, both from one ffmpeg. The encoder is
`libx264`; set `WEBCAM_ENCODER=h264_v4l2m2m` on a board with a V4L2
encoder after measuring it. A webcam is named by its `/dev/v4l/by-id`
entry, which does not change when it moves to another USB port.

What the overlay changes: the MediaMTX image becomes the `-ffmpeg` variant
(the default image has no shell and no ffmpeg on purpose), `/dev` is
bind-mounted with a cgroup rule that admits only video devices, and the
container joins the host's `video` group. Docker Desktop on macOS and
Windows cannot pass USB devices; there the host agent below does the job.

### macOS and Windows: the host agent

1. Once per machine, install the [host agent](HOST_AGENT.md) in the OpenNVR
   folder:

   ```powershell
   .\start.ps1 host-agent install        # Windows
   ```

   ```bash
   ./start.sh host-agent install         # Linux / macOS (asks for sudo once on Linux)
   ```

   It also offers to install ffmpeg, which does the capture.

2. **Add Camera → Manual → Webcam**, source **OpenNVR server**. The webcams
   plugged into that machine are listed. Pick one, name the camera, then
   click **Add webcam**. It is live within a few seconds.

There's nothing else to do:
- A webcam plugged in while the dialog is open appears by itself.
- Unplugging a webcam takes its camera offline, and it doesn't raise a recording alarm.
- Plugging it back in, into the same or another USB port, resumes the stream.

To use another webcam for the same camera, choose it in the camera's **Edit**
dialog. A browser webcam can be switched to a USB one there too.

### Things that trip it up

- **The webcam is in use.** A webcam serves one app at a time. While it
  streams, a video call can't use it, and the other way round.
- **Windows.** The agent runs while a user is logged in, because Windows only
  gives the camera to a logged-in session. An NVR PC should sign in
  automatically. Camera privacy must also allow desktop apps: Settings →
  Privacy & security → Camera → *Let desktop apps access your camera*.
- **macOS.** Grant the agent camera access once. See [HOST_AGENT.md](HOST_AGENT.md#macos).
- **Two identical webcams.** They are told apart by their USB port. After
  moving one to another port, choose it again in the camera's Edit dialog.
- **Offline vs stalled.** A webcam with nobody publishing shows as offline,
  and that is not a recording alarm. The "recording stalled" alarm fires only
  when a publisher is connected but no segments are being written.

## API

| | |
|---|---|
| `POST /api/v1/cameras/` with `{"name": "...", "source_type": "publisher"}` | add a browser webcam (no `ip_address`, `rtsp_url` or credentials) |
| … with `"source_device": "<id>"` too | add a USB webcam; the id comes from `GET /api/v1/host-agent` |
| `PUT /api/v1/cameras/{id}` with `{"source_device": "<id>"}` | switch a webcam camera to another USB webcam |
| `POST /api/v1/streams/{id}/whip` | `{whip_url, token}` for a browser publisher (`cameras.manage`) |
| `GET /api/v1/host-agent` | the host agent: connected, webcams found, stream states (`cameras.manage`) |
