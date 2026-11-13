# OpenNVR User Manual

This page covers using OpenNVR after the install is up. Install lives in
[DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md); the bare-metal dev shell
lives in [`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md).

## First-boot setup token

The very first time the core starts it prints a one-time setup token to
its log:

```bash
docker compose -f docker-compose.yml logs opennvr-core | grep -i 'setup token'
```

Open <http://localhost:8000>, paste the token on the setup screen, then
choose an admin username and password. The token is single-use; subsequent
restarts skip this flow because an admin already exists.

There are no shipped default credentials. If you lose the password before
adding a second admin account, the recovery path is to reset the
`opennvr_db_data` Docker volume and start over — see
[Recovery](#recovery) below.

## Web UI tour

The dashboard is organised into five main areas, each backed by a section
in the left-side navigation.

### Cameras

This is where you add and manage the RTSP / ONVIF sources OpenNVR records
from.

**Adding an ONVIF camera (recommended):**

1. Click **Cameras → Add camera**.
2. Choose **Discover via ONVIF**.
3. Enter the camera's ONVIF username and password (most IP cameras print
   these on a label; default `admin / admin` is common but vendor-
   specific).
4. Pick the camera from the discovered list. OpenNVR auto-fills the RTSP
   URL, codec, and resolution from the ONVIF profile.
5. Click **Save**. The camera should turn green in the dashboard within
   ~30 seconds.

**Adding a camera by RTSP URL (fallback):**

1. Click **Cameras → Add camera**.
2. Choose **Manual RTSP**.
3. Paste the RTSP URL (typically `rtsp://user:pass@camera.ip:554/stream1`
   — check your camera's documentation).
4. Click **Test connection** before saving — OpenNVR probes the stream
   and reports back whether it can decode it.
5. Click **Save**.

**Adding many cameras at once (import):**

**Cameras → Import** takes a CSV or Excel file. Download the template from
the same dialog: its second sheet explains every column.

- **One row per camera:** name, location, address, login, and the RTSP URL
  (or leave the URL blank and OpenNVR finds it by ONVIF).
- **One row per NVR or DVR:** fill in `channels` (`1-16`, `1,3,5-8`) and
  `vendor` (`hikvision`, `uniview`, `dahua` or `cpplus`) and the row becomes
  one camera per channel, named "<name> Ch 1", "Ch 2"…, each with that
  channel's main stream and substream. For any other recorder, give an
  `rtsp_url` with `{ch}` where the channel number goes.

Before anything is saved, the import shows every row as New, Update
(matches a camera already here), Unchanged, Duplicate (twice in the file)
or Error, with the reason. Choose whether matched cameras are updated, then
import: cameras are added a few at a time in the background, and the
results can be downloaded at the end. Rows with errors are skipped, never
the whole file. Importing the same file again changes nothing, a blank cell
never changes a camera, and a camera left out of the file is not deleted.
A camera you may not manage is refused. **Read each camera's details and
set its clock** makes the import slower (seconds per camera) but fills in
make and model straight away.

**Cameras → Export** writes the cameras you manage, filtered as the list is
(search, location, Active only), in the import file's columns, so an export
can be edited and imported back. Passwords, and the logins inside RTSP URLs,
are left out unless you tick **Include passwords**, which asks for your MFA
code; every export is recorded in the audit log.

**Per-camera settings worth visiting:**

- **Recording → Retention** — days to keep recorded segments before
  rotating them out. Default 7. Set per-camera based on how much disk
  you've allocated.
- **Recording → Schedule** — 24/7 by default. Switch to motion-triggered
  via the "AI detection" checkbox if you've enabled inference on that
  camera.
- **AI detection → Enabled** — toggles per-camera inference. standard stack
  defaults to YOLOv8 detection on every frame; disable here if you don't
  want detection on a particular camera.
- **TLS → Allow plaintext RTSP** — off by default. Turning this on
  requires a confirmation dialog and lands in the audit log.

### Live view

The live-view page shows a grid of all enabled cameras. Click any tile to
go fullscreen on that camera. Detection overlays (bounding boxes, class
labels) render in real time when AI detection is enabled for that camera.

WebRTC is the default transport. If the browser can't establish a WebRTC
connection (corporate networks, restrictive firewalls), the player falls
back to HLS — same content, ~3 second latency penalty.

### Camera tours (display boards)

A tour cycles a screen through sets of cameras, for example 20 North-zone
cameras for a minute, then 20 South-zone cameras, then back to the start.
Build tours under **Settings → More Settings → Camera Tours** (needs the
`cameras.manage` permission); anyone with `live.view` can run one from the
**Tours** button in Live View.

- **Steps.** Each step is a layout, a list of cameras and how long to show
  them (10 seconds to 1 hour). Plain grids such as 5×4 are offered next to
  the Live View layouts, so a 20-camera wall needs no custom layout.
- **More cameras than tiles.** A step pages through them, a full duration
  per page: "North, all 60 cameras, 5×4" shows three pages of 20.
- **Picking cameras.** The editor is a page of its own: steps on the left
  (drag to reorder; the panel and its sections fold away), the selected step
  on the right, split in two. On its left, pick cameras: search by name or
  IP, filter by the camera's **Location** field, or add a whole location at
  once. On its right, **Preview** lays each page out exactly as the wall
  does, from still pictures: the layout you chose, every tile in its place,
  and tiles without a camera left empty (numbered here, dark on the wall).
  Drag a tile onto another to swap the two, ✕ to remove one (its tile is
  left empty and no other camera moves; **Close gaps** packs them up again).
  A camera ticked in the list fills the first
  empty tile. To
  put a camera on a particular tile, drag it from the list onto the tile, or
  click the tile, then the camera. A camera already in the step moves there
  instead of showing twice, and the editor says so. Pictures are stills; **▶** on any camera opens it live, only
  when asked. Filling in each camera's Location (North, South, Branch 12…)
  is what makes this quick.
- **Checks while you edit.** The top bar always counts how many of your
  cameras are on the tour, and the camera list marks each one that is **Not on
  tour**. **Save tour** warns you if any are left out, but you can still save.
  Each page shows how many of its tiles have cameras; empty tiles stay dark on
  the wall.
- **Fit or Fill.** Per tour: Fit shows each camera's whole picture; Fill
  crops it to fill its tile, which suits portrait and fisheye cameras.
- **Page changes.** **Load the next page in advance** (on by default) plays the
  next page's cameras, hidden, for the last 5 seconds of a page, so they are
  already playing when it shows; the display PC plays two pages for those
  seconds. **When the page changes** is a short **Crossfade** (default) or a
  straight **Cut**. A tile whose video has not started yet shows that
  camera's last frame from this wall rather than a black tile.
- **Who sees what.** Every viewer sees only the cameras they are allowed
  to see; the bar says how many were left out. Changing or deleting a tour
  needs the right to manage every camera in it; anyone else can run it.

**Alarms on the wall.** Turn on **Interrupt the tour when an alarm happens** in a tour's
settings and an alert on one of its cameras takes over the wall: the
camera fills the screen with a red frame and a banner saying what happened
and when, and up to four alarming cameras share the wall. After **Show
each alarm for** (30 s by default) the tour carries on exactly where it was; **Back to
tour** or **Esc** returns at once. Choose what counts — alerts from apps and
camera analytics (with a lowest severity), the camera's own alarms
(motion, tamper, line crossing), or both — and whether only the tour's
cameras or any camera the screen can see may take over. **Same camera can interrupt again after** stops one busy camera from holding the wall.

Running a tour on a display board:

1. Copy the tour's **display link** from the Camera Tours page
   (`https://<host>/display/tour/<id>`). It shows the wall alone, with no
   sidebar or header. (Live View's **Tours** button opens the same tour
   inside the app instead.)
2. On the display PC, sign in with an account that only has `live.view`
   and the cameras that board should show.
3. Start the browser in kiosk mode, e.g.
   `chrome --kiosk https://<host>/display/tour/<id>`. Kiosk mode makes the
   browser window fill the screen with no address bar, and the display
   link fills that window. Turn off the operating system's screen sleep.

On the wall: a chip in the bottom corner always says what is on screen and
what comes next ("North → South in 18 s"). **Space** pauses and resumes (a
paused wall says so until it is resumed — moving the mouse never pauses
it), **← / →** step, **F** toggles fullscreen, and clicking a tile shows
that camera on its own until it is clicked again. Someone who may edit the
tour can open the page on screen from the wall's bar (**Edit this page**). It
opens in the same page editor the tour uses, with ‹ › to move between pages and
**Edit whole tour** for everything else; saving or cancelling brings them back
to the wall, on that page. Saving changes the tour, so every screen running it
changes too. Removing a camera leaves its tile empty (dark on the wall): no
other camera moves. Edits to a tour reach running walls within five
minutes; a turned-off tour shows a notice and starts again by itself when
it is turned back on. A running board keeps itself signed in; one left
switched off for more than 30 days has to sign in again.

Tiles play each camera's low-resolution substream to keep a large wall
light on the display PC, and switch to the main stream for any camera
whose substream does not work. Each tile on screen opens one extra
connection from OpenNVR to that camera's recorder; with many recorders
this is negligible, but a single NVR with a low connection limit may
need fewer tiles per page.

### Playback

The playback page lists recorded segments for a date range you pick, with filters by camera (single or all), by event type (all recordings, motion-triggered, AI-triggered), and by time range (calendar picker or quick presets — last hour, today, yesterday, last seven days).

Click a segment to play it. The scrubber respects the seek window the recording was indexed with; for finer-grained seeking on long recordings, zoom the scrubber via the magnifier icon.

Export is via the **⋯** menu on a segment row → **Download MP4**. Exports
are remuxed (not re-encoded) so they preserve original quality.

### AI models

This page lists every AI adapter KAI-C has registered. For each adapter
you can see:

- **Status** — `healthy`, `loading`, or `error` based on the adapter's
  `/health` endpoint.
- **Model fingerprint** — sha256 of the weights file. If you've enabled
  drift detection (default), this is polled every 60 seconds; a
  fingerprint change between polls fires an `adapter.fingerprint_mismatch`
  audit event.
- **Capabilities** — declared body shape, advertised tasks, sovereignty
  posture, fair-queuing intent.
- **Per-camera enable / disable** — toggle which cameras the adapter
  runs against.

The standard stack install ships YOLOv8 only. Additional adapters (Whisper,
Piper, fast-plate-ocr, InsightFace, BLIP) are pulled in by the
camera-agent overlay or by enabling them manually in `docker-compose.yml`
and adding KAI-C registry entries.

### Audit log

Every inference, every registration, every adapter refusal lands here
with an `X-Correlation-Id` that joins the alert → middleware → adapter
chain. Useful for:

- **Investigating "why did this alert fire at 22:14?"** — filter on the
  alert's correlation_id, see the exact adapter call and the model
  fingerprint at the time.
- **Verifying no cloud calls happened** — filter on
  `inference.refused_sovereignty`. Empty result = the local-only policy
  held.
- **Tracking model drift** — filter on `adapter.fingerprint_mismatch`.

Audit events also publish to NATS on the `opennvr.audit.*` subject scheme
for downstream consumers (SIEM, custom dashboards). See the
[`alerts-subscriber` example](examples/alerts-subscriber) for a copy-as-
template subscriber.

## Common operations

### Change your admin password

Click your username in the top-right → **Profile** → **Change password**.

Admin credentials live in the database, not in environment variables.
Setting `DEFAULT_ADMIN_PASSWORD` in `.env` has no effect after first
boot.

### Add another admin

**User management → Add user** → role `admin`. The new admin signs in
with the credentials you set, then changes their password on first
login.

### Connect Home Assistant

**Settings → API Tokens → New token** (the *Home Assistant* preset), then
**Settings → Integrations → Add → MQTT** with *Home Assistant discovery*
on and that token as *Act as API token*. Home Assistant's own MQTT
integration then lists OpenNVR, every camera, zone and app as devices —
nothing to install on the Home Assistant side. Step by step, automation
examples and troubleshooting:
[docs/HOME_ASSISTANT_USER_GUIDE.md](docs/HOME_ASSISTANT_USER_GUIDE.md).

### View logs

```bash
# Everything
docker compose -f docker-compose.yml logs -f

# A specific service
docker compose -f docker-compose.yml logs -f opennvr-core
docker compose -f docker-compose.yml logs -f yolov8-adapter
docker compose -f docker-compose.yml logs -f mediamtx
```

The core service logs are also visible in the web UI under **Settings →
Server logs** if you don't want to drop to a shell.

### Update to the latest images

```bash
docker compose -f docker-compose.yml pull
docker compose -f docker-compose.yml up -d
```

The database schema migrates automatically on core startup. Manual
migrations are never required for a normal upgrade.

### Recovery

If you've lost the admin password and there's no second admin to reset
it through the UI, the only recovery is to reset the database volume and
re-run the first-boot setup flow:

```bash
docker compose -f docker-compose.yml down -v
docker compose -f docker-compose.yml up -d
```

**This deletes the camera list, user accounts, and audit log.**
Recordings on disk are kept — the volume that holds them is separate.
After reset, add your cameras back via the UI.

## Troubleshooting

### "Camera offline" but the camera itself works

- Verify the RTSP URL with `ffprobe -v error -rtsp_transport tcp <url>`
  from outside Docker.
- Check whether the camera requires `rtsp_transport=tcp` (some do — it's
  a common cause of "works in VLC, not in OpenNVR"). The per-camera
  settings page lets you switch.

### Detection overlays don't render

- Confirm the YOLOv8 adapter is healthy on **AI models**. If `loading`,
  wait — first inference call triggers weight load (~5-10 seconds).
- Confirm per-camera **AI detection** is enabled.
- Open the browser console — overlay-render errors usually log a useful
  message.

### Database connection errors

```bash
docker compose -f docker-compose.yml restart db
docker compose -f docker-compose.yml ps          # wait for db to be Up (healthy)
docker compose -f docker-compose.yml restart opennvr-core
```

### Disk filling up

Recordings under `RECORDINGS_PATH` are the most common culprit. Per-
camera retention defaults to 7 days; lower it via **Cameras → per-camera
settings → Recording → Retention** if disk pressure is high.

```bash
docker system df               # see where the rest of the space is going
```

## Support

Operational questions land in [Discussions](https://github.com/open-nvr/open-nvr/discussions), bugs in [Issues](https://github.com/open-nvr/open-nvr/issues), security via [SECURITY.md](SECURITY.md). The install flow is in [DOCKER_QUICKSTART.md](DOCKER_QUICKSTART.md); if you want to send patches back, [CONTRIBUTING.md](CONTRIBUTING.md) covers the flow.
