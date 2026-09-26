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
