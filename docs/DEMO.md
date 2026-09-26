# The 90-second demo

What OpenNVR is for, shown rather than explained: a camera becomes an AI
security application, then a question, then a Home Assistant device — in
one take on one laptop. This is the script for the video; until it is
recorded, it is also the fastest walk-through of the product.

Every step below was run, in this order, on a fresh install.

## Scene 1 — a camera (0:00–0:15)

> "This is a fresh OpenNVR on a laptop. One command, no cloud, no account."

```bash
./start.sh up
```

No camera to hand? The fake-camera rig turns any `.mp4` in
`data/fake-cameras/` into an RTSP stream:

```bash
docker exec -i opennvr_core python - < scripts/fakecams/register_fake_cameras.py
```

Show: **Cameras → cam1** live, recording, detection boxes appearing.

## Scene 2 — install an AI application (0:15–0:40)

> "Apps are installed from a catalog, like on a phone. This one reads plates."

**App Catalog → ANPR → Install** (the card gives the command):

```bash
docker compose -f docker-compose.yml -f docker-compose.apps.yml --profile apps up -d license-plate-recognition
```

**Applications → Vehicles → Configure → Cameras → select cam1.** That is
the whole assignment: picking a camera for the app is what puts the
plate-reading skill on that camera. Show the **Vehicles** page filling
with reads and photos.

> "The model runs on this laptop, through an adapter contract — swap it
> for your own model, nothing else changes."

## Scene 3 — ask a question (0:40–1:05)

> "Every visit is remembered with its best photo, so you can ask."

Open the camera agent (`examples/camera-agent/quickstart.sh --chat`,
then <http://localhost:9100/demo>) and type:

> *did you see any blue car in the last 15 minutes?*

Show the answer: the count, the times, the photos — and that the colour
came from the box's own descriptions (captions and VQA), not from a cloud.

## Scene 4 — Home Assistant, nothing to install (1:05–1:25)

> "And it is already in Home Assistant."

**Settings → Integrations → MQTT** (discovery on, acting as an API token).
Cut to Home Assistant: **Settings → Devices** shows *OpenNVR* and *cam1*
with motion, detection, counts, last plate. Flip the **detection** switch
in Home Assistant; show the camera's detection stop in OpenNVR.

## Close (1:25–1:30)

> "Cameras you already own. AI you choose. Video that never leaves.
> github.com/open-nvr — star it, or better, build on it."

## Recording notes

- 1080p, the browser at 125 % zoom; hide the bookmarks bar.
- Use the demo clips in `data/fake-cameras/` (dashcam-style footage gives
  constant traffic, so counts move on screen).
- Pre-pull the app images and the agent's LLM before recording; the
  first install builds them.
- Keep the terminal to two commands on screen; everything else is the UI.
