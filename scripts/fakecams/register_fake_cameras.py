"""Register the fake-camera rig's streams as OpenNVR cameras.

Runs INSIDE the opennvr-core container (it needs the app's signing key and
database session), driving the ordinary public camera API so every camera is
provisioned into MediaMTX exactly like a real one:

    docker exec -i opennvr_core python - < scripts/fakecams/register_fake_cameras.py

It asks the fake-camera rig which paths it is currently serving, then creates
one camera per path, skipping paths that already have a camera. The new cameras
behave like any other: live view, recording, playback and Tier-0 detection all
work on them with no further configuration.

The rig picks up new clips on its own, so re-run this whenever you have added
some — or leave it running with FAKECAM_WATCH=1 and it registers each new
stream as it appears:

    docker exec -e FAKECAM_WATCH=1 -i opennvr_core \
        python - < scripts/fakecams/register_fake_cameras.py

Environment knobs:
    FAKECAM_HOST   host:port of the rig's API      (default fakecams:9997)
    FAKECAM_IP     address cameras are stored with (default 172.28.90.10)
    FAKECAM_PORT   RTSP port                       (default 8554)
    FAKECAM_PREFIX camera-name prefix              (default "fake-")
    FAKECAM_WATCH  keep running, registering new streams as the rig serves
                   them (default off; Ctrl-C to stop)
    FAKECAM_WATCH_INTERVAL  seconds between checks in watch mode (default 10)
    FAKECAM_SKILL  optional skill to assign to every new camera (default: none).
                   Set it when testing a capability that scopes itself by
                   assignment, e.g. FAKECAM_SKILL=license_plate_recognition
                   or FAKECAM_SKILL=occupancy_counting.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import warnings

# The app's SQLAlchemy models emit relationship-overlap warnings on first
# mapper configuration; they are noise here and drown the actual output.
warnings.filterwarnings("ignore")

sys.path.insert(0, "/app/server")
os.chdir("/app/server")

from core.auth import create_access_token  # noqa: E402
from core.database import SessionLocal  # noqa: E402
from models import User  # noqa: E402

API = "http://127.0.0.1:8000/api/v1"
RIG = os.environ.get("FAKECAM_HOST", "fakecams:9997")
CAM_IP = os.environ.get("FAKECAM_IP", "172.28.90.10")
CAM_PORT = int(os.environ.get("FAKECAM_PORT", "8554"))
SKILL = os.environ.get("FAKECAM_SKILL", "")
PREFIX = os.environ.get("FAKECAM_PREFIX", "fake-")
WATCH = os.environ.get("FAKECAM_WATCH", "").lower() in ("1", "true", "yes", "on")
WATCH_INTERVAL = int(os.environ.get("FAKECAM_WATCH_INTERVAL", "10"))


def request(method, url, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, raw


def admin_token(quiet=False):
    """Mint an access token for the first active superuser (else any user)."""
    db = SessionLocal()
    try:
        user = (
            db.query(User)
            .filter(User.is_active.is_(True), User.is_superuser.is_(True))
            .order_by(User.id)
            .first()
        ) or db.query(User).filter(User.is_active.is_(True)).order_by(User.id).first()
        if user is None:
            sys.exit("No active user in the database — finish the setup wizard first.")
        if not quiet:
            print(f"acting as user '{user.username}'")
        return create_access_token({"sub": user.username})
    finally:
        db.close()


def rig_paths(fatal=True):
    """Streams the rig is serving, as (name, ready) pairs.

    Returns None instead of exiting when fatal is False — watch mode has to
    survive the rig being restarted underneath it.
    """
    status, body = request("GET", f"http://{RIG}/v3/paths/list?itemsPerPage=1000")
    if status != 200 or not isinstance(body, dict):
        msg = (
            f"Could not read the fake-camera rig at {RIG} (HTTP {status}). "
            "Is it up?  docker compose ... --profile fakecams up -d fakecams"
        )
        if fatal:
            sys.exit(msg)
        print(msg)
        return None
    names = []
    for item in body.get("items") or []:
        name = item.get("name")
        # A path with no publisher is a stream whose ffmpeg is still starting.
        if name and name != "all_others":
            names.append((name, bool(item.get("ready"))))
    return sorted(names)


def register(token, paths):
    """Create a camera for every rig path that does not have one yet."""
    status, existing = request(
        "GET", f"{API}/cameras/?limit=500&active_only=false", token
    )
    if status != 200:
        sys.exit(f"Could not list existing cameras (HTTP {status}): {existing}")
    have = {
        c.get("rtsp_url")
        for c in (existing.get("cameras") or existing.get("items") or [])
    }

    created, skipped = [], []
    for name, ready in paths:
        url = f"rtsp://{CAM_IP}:{CAM_PORT}/{name}"
        if url in have:
            skipped.append(name)
            continue
        payload = {
            "name": f"{PREFIX}{name}"[:100],
            "description": "Fake camera served from a video file (test rig)",
            "ip_address": CAM_IP,
            "port": CAM_PORT,
            "rtsp_url": url,
            "location": "fake-cameras",
        }
        # force=true: every fake camera shares the rig's IP, which the
        # duplicate guard would otherwise reject after the first one.
        status, body = request("POST", f"{API}/cameras/?force=true", token, payload)
        if status not in (200, 201):
            print(f"  !! {name}: create failed (HTTP {status}): {body}")
            continue
        cam_id = body["id"]
        note = "" if ready else "  (rig reports no publisher yet)"
        print(f"  ++ camera {cam_id}: {payload['name']} -> {url}{note}")
        created.append((cam_id, payload["name"], url))

        if SKILL:
            status, body = request(
                "PUT",
                f"{API}/cameras/{cam_id}",
                token,
                {"assignments": [{"skill": SKILL}]},
            )
            if status != 200:
                print(f"     !! could not assign '{SKILL}' (HTTP {status}): {body}")

    return created, skipped


def main():
    token = admin_token()

    if not WATCH:
        paths = rig_paths()
        if not paths:
            sys.exit(
                f"The rig at {RIG} is serving no streams — check that your video "
                "files are in the folder bound to /videos, then restart it."
            )
        created, skipped = register(token, paths)
        print()
        print(f"created {len(created)} camera(s); {len(skipped)} already present")
        if SKILL and created:
            print(f"assigned skill '{SKILL}' to every new camera")
        if skipped:
            print("already present: " + ", ".join(skipped))
        return

    print(f"watching {RIG} every {WATCH_INTERVAL}s — Ctrl-C to stop")
    try:
        while True:
            paths = rig_paths(fatal=False)
            if paths:
                # Re-minted every pass: a watch left running overnight outlives
                # the access token's expiry.
                created, _ = register(admin_token(quiet=True), paths)
                if created and SKILL:
                    print(f"     assigned '{SKILL}' to {len(created)} new camera(s)")
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
