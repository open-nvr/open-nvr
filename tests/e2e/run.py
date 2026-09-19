#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bring up an isolated OpenNVR and run the E2E suite against it.

    python tests/e2e/run.py                    # smoke tier, reusing a warm stack
    python tests/e2e/run.py --fresh            # wipe first: the only way to
                                               # exercise first-time setup
    python tests/e2e/run.py -m detection
    python tests/e2e/run.py -- -k camera_lifecycle -x

Everything after ``--`` goes to pytest untouched.

**Why a script rather than documented compose commands.** The isolation that
keeps this suite away from your real deployment is not one flag, it is a dozen
environment variables applied together — project name, container names, host
ports, subnet, recordings path. Miss one and the E2E stack adopts your real
volumes. The failure mode is losing recordings, so it is not left to memory.

**Why Python rather than run.sh + run.ps1.** The repo already carries
``start.sh`` and ``start.ps1``, and a host-hardening test exists purely to stop
them drifting apart. One cross-platform script needs no such test.

The host side owns stack lifecycle because a container cannot cleanly bring up
the compose project it is itself a member of. Everything after that — auth,
fixtures, assertions, evidence — happens inside the runner.
"""

from __future__ import annotations

import argparse
import base64
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness.clips import (  # noqa: E402
    FFMPEG_IMAGE,
    SYNTHETIC_CLIPS,
    ffmpeg_command,
    real_clips,
)
from harness.compose import parse_setup_token  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
E2E = REPO / "tests" / "e2e"
ARTIFACTS = E2E / ".artifacts"
ENV_FILE = ARTIFACTS / "e2e.env"

PROJECT = "opennvr-e2e"
CORE_CONTAINER = "opennvr_e2e_core"

#: Files that make up the fake-camera rig. It lives on its own branch by
#: design (docs/FAKE_CAMERAS.md), so a clone that has never fetched it needs
#: these materialised before compose can even parse the overlay.
RIG_BRANCH = "origin/fake-camera"
RIG_FILES = (
    "docker-compose.fakecams.yml",
    "scripts/fakecams/entrypoint.sh",
    "scripts/fakecams/register_fake_cameras.py",
)

COMPOSE_FILES = (
    "docker-compose.yml",
    "docker-compose.fakecams.yml",  # always passed: the e2e overlay renames its
                                    # container, which compose cannot do for a
                                    # service nothing else defines
    "docker-compose.e2e.yml",
)


# ---------------------------------------------------------------------------
# Shell helpers
# ---------------------------------------------------------------------------
def say(message: str) -> None:
    print(f"[e2e] {message}", flush=True)


def die(message: str, code: int = 1) -> None:
    print(f"[e2e] ERROR: {message}", file=sys.stderr, flush=True)
    raise SystemExit(code)


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(REPO), **kwargs)


def capture(args: list[str]) -> str:
    result = run(args, capture_output=True, check=False)
    return result.stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# The fake-camera rig
# ---------------------------------------------------------------------------
def ensure_rig() -> None:
    """Materialise the rig files if this clone does not have them.

    ``git show <branch>:<path> > <dest>`` rather than ``git checkout <branch>
    -- <path>``: the checkout form STAGES the files into the developer's index,
    which is a rude thing for a test runner to do to someone's work in
    progress. ``git show`` just writes bytes.
    """
    missing = [name for name in RIG_FILES if not (REPO / name).exists()]
    if not missing:
        return

    say(f"fake-camera rig missing {len(missing)} file(s); fetching from {RIG_BRANCH}")
    fetch = run(
        ["git", "fetch", "--quiet", "origin", "fake-camera"],
        capture_output=True,
        check=False,
    )
    if fetch.returncode != 0:
        die(
            "could not fetch the fake-camera branch, and these files are "
            "missing:\n  " + "\n  ".join(missing) + "\n"
            "  With no network, copy them from another clone, or see "
            "docs/FAKE_CAMERAS.md."
        )

    for name in missing:
        dest = REPO / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        blob = run(
            ["git", "show", f"{RIG_BRANCH}:{name}"], capture_output=True, check=False
        )
        if blob.returncode != 0:
            die(f"{name} is not present on {RIG_BRANCH}")
        # newline="" keeps the LF endings the container needs; .gitattributes
        # pins *.sh to LF for the same reason, and a CRLF entrypoint.sh fails
        # to exec inside the rig with a bare "not found".
        dest.write_bytes(blob.stdout)
        say(f"  wrote {name}")

    script = REPO / "scripts" / "fakecams" / "entrypoint.sh"
    if script.exists():
        script.chmod(0o755)


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
def fernet_key() -> str:
    """A valid Fernet key without depending on `cryptography` being installed.

    Fernet wants the url-safe base64 of exactly 32 random bytes — the same
    44-character shape ``openssl rand -base64 32 | tr '+/' '-_'`` produces in
    .github/workflows/publish-images.yml. ``core/secret_policy.py`` rejects
    anything else at boot.
    """
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def write_env(force: bool) -> dict[str, str]:
    """Create (or reuse) the hermetic env file the E2E stack boots from.

    Deliberately independent of the developer's ``.env``: the suite must behave
    the same on this laptop and on a CI runner, and inheriting somebody's local
    tuning is how "works on my machine" gets into an E2E suite. Passed to
    compose with ``--env-file``, which replaces ``.env`` discovery entirely.

    Reused across runs unless ``--fresh``, because the secrets have to match
    the ones already baked into the running stack's database.
    """
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    if ENV_FILE.exists() and not force:
        return _backfill_env(read_env())

    recordings = ARTIFACTS / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)

    values = {
        # --- identity -----------------------------------------------------
        "COMPOSE_PROJECT_NAME": PROJECT,
        # --- secrets (boot aborts on weak or placeholder values) ----------
        "SECRET_KEY": secrets.token_urlsafe(48),
        "INTERNAL_API_KEY": secrets.token_urlsafe(48),
        "MEDIAMTX_SECRET": secrets.token_hex(32),
        "CREDENTIAL_ENCRYPTION_KEY": fernet_key(),
        "POSTGRES_USER": "opennvr",
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "POSTGRES_DB": "opennvr_e2e",
        # --- isolation ----------------------------------------------------
        # A bridge cannot share a subnet with another bridge on the same host,
        # so the E2E network needs its own.
        "OPENNVR_DOCKER_SUBNET": "172.29.0.0/16",
        "FAKECAM_IP": "172.29.90.10",
        # MediaMTX's segment-complete hook curls the backend BY CONTAINER
        # NAME, and the E2E stack renames core. Left at its default the hook
        # resolves nothing, no segment is ever indexed from the webhook, and
        # the only reason footage still appears is the reconciler backfilling
        # from disk minutes later — which looks exactly like "recording is
        # broken" to any test that queries footage by wall-clock time.
        "BACKEND_HOST": "opennvr_e2e_core",
        # The rig streams whatever is in here. Never ./data/fake-cameras:
        # the suite writes to this directory, and that one is yours.
        "FAKECAM_VIDEO_DIR": "./tests/e2e/.artifacts/clips",
        # Host publications are useless to this suite (the runner is on the
        # internal network) but compose CONCATENATES `ports` across files, so
        # they cannot be removed — only moved somewhere harmless.
        "CORE_HOST_PORT": "28000",
        "MEDIAMTX_HOST_RTSPS_PORT": "28322",
        "NGINX_HOST_HTTPS_PORT": "20443",
        "NGINX_HOST_HTTP_PORT": "20080",
        "WEBRTC_ICE_PORT": "28189",
        "LOG_VIEWER_HOST_PORT": "29999",
        "FAKECAM_HOST_RTSP_PORT": "28554",
        # Never ./recordings — that is where the real deployment writes.
        "RECORDINGS_PATH": "./tests/e2e/.artifacts/recordings",
        # --- determinism --------------------------------------------------
        # mediamtx and core must agree, or recording paths stop resolving at
        # the day boundary. UTC removes the DST edge case entirely.
        "TZ": "UTC",
        "E2E_TZ": "UTC",
        # Stream-copy looping emits corrupt packets at the loop seam, which can
        # wedge Tier-0's motion detector. Transcoding rebuilds clean keyframes.
        # 60s segments (the product default) would put a full minute of
        # dead wait into every recording assertion. This is a supported
        # knob, and core pushes it per-path when it provisions a camera.
        "RECORDING_SEGMENT_SECONDS": "10",
        # Tier-0 does not persist visits unless asked. Left off, detection
        # runs and produces nothing the API can show -- the timeline is empty
        # and the pipeline logs "visit persistence = off" once at startup and
        # never mentions it again. The detection tier exists to assert on
        # those rows, so it has to be on.
        "DETECT_VISITS_ENABLED": "true",
        "FAKECAM_MODE": "transcode",
        "FAKECAM_FPS": "10",
        "DEPLOYMENT_MODE": "offline",
        "OPENNVR_DEFAULT_APPS": "off",
        "E2E_GIT_SHA": git_sha(),
    }

    ENV_FILE.write_text(
        "# Generated by tests/e2e/run.py. Throwaway secrets for a throwaway\n"
        "# stack — regenerated by --fresh. Do not reuse anywhere real.\n"
        + "".join(f"{k}={v}\n" for k, v in values.items()),
        encoding="utf-8",
    )
    try:
        ENV_FILE.chmod(0o600)
    except OSError:
        pass
    say(f"wrote {ENV_FILE.relative_to(REPO)}")
    return values


#: Keys safe to add to an existing env file. Secrets are absent on purpose:
#: rewriting one would no longer match the database the running stack booted
#: with, and every login would start failing for a very confusing reason.
_BACKFILLABLE = (
    "FAKECAM_VIDEO_DIR",
    "RECORDING_SEGMENT_SECONDS",
    "BACKEND_HOST",
    "DETECT_VISITS_ENABLED",
    "FAKECAM_MODE",
    "FAKECAM_FPS",
    "E2E_TZ",
    "E2E_GIT_SHA",
)


def _backfill_env(values: dict[str, str]) -> dict[str, str]:
    """Add keys a newer version of this script expects, keeping the secrets.

    Without this, an env file written before a knob existed silently falls back
    to that knob's compose default — which for FAKECAM_VIDEO_DIR would point
    the test rig at the developer's own clips folder.
    """
    defaults = {
        "FAKECAM_VIDEO_DIR": "./tests/e2e/.artifacts/clips",
        "BACKEND_HOST": "opennvr_e2e_core",
        "RECORDING_SEGMENT_SECONDS": "10",
        "DETECT_VISITS_ENABLED": "true",
        "RECORDING_SEGMENT_SECONDS": "10",
        "FAKECAM_MODE": "transcode",
        "FAKECAM_FPS": "10",
        "E2E_TZ": "UTC",
        "E2E_GIT_SHA": git_sha(),
    }
    missing = {k: defaults[k] for k in _BACKFILLABLE if k not in values and k in defaults}
    # E2E_GIT_SHA is provenance, not configuration: refresh it every run.
    values["E2E_GIT_SHA"] = defaults["E2E_GIT_SHA"]
    if missing:
        with ENV_FILE.open("a", encoding="utf-8") as handle:
            handle.write("\n# backfilled by run.py\n")
            for key, value in missing.items():
                handle.write(f"{key}={value}\n")
        say(f"backfilled {', '.join(missing)} into the existing env file")
        values.update(missing)
    return values


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


#: Host ports the E2E stack publishes, and the protocols each must be free on.
#: The runner never uses them (it is on the internal network), but compose
#: still has to bind them, and ONE unbindable port fails the whole service.
_HOST_PORTS: dict[str, tuple[str, ...]] = {
    "CORE_HOST_PORT": ("tcp",),
    "MEDIAMTX_HOST_RTSPS_PORT": ("tcp",),
    "NGINX_HOST_HTTPS_PORT": ("tcp",),
    "NGINX_HOST_HTTP_PORT": ("tcp",),
    "WEBRTC_ICE_PORT": ("tcp", "udp"),
    "LOG_VIEWER_HOST_PORT": ("tcp",),
    "FAKECAM_HOST_RTSP_PORT": ("tcp",),
}


def port_is_free(port: int, protocols: tuple[str, ...] = ("tcp",)) -> bool:
    """True if every protocol can bind 0.0.0.0:``port`` right now.

    A real bind rather than a lookup, because on Windows a port can be
    unbindable for reasons no listing shows together: another process
    holding it (Docker's own backend grabs arbitrary local ports), or a
    WinNAT reserved range that moves on every reboot.
    """
    for proto in protocols:
        kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
        with socket.socket(socket.AF_INET, kind) as probe:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows: no sharing
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                return False
    return True


def e2e_stack_running() -> bool:
    """Whether any container of the E2E project is up (it then owns its ports)."""
    return bool(capture([
        "docker", "ps", "-q", "--filter",
        f"label=com.docker.compose.project={PROJECT}",
    ]).strip())


def _set_env_value(key: str, value: str) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    out = [f"{key}={value}" if ln.split("=", 1)[0].strip() == key else ln
           for ln in lines]
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


def ensure_free_host_ports(values: dict[str, str]) -> dict[str, str]:
    """Move any unbindable host port to the next free one, before compose up.

    The fixed +20000 band collided in practice (Docker's backend held 20080 as
    a local port), failing ``compose up`` on a port the suite never even uses.
    Skipped while the E2E stack is running: its own publications hold these
    ports, and moving them would needlessly recreate containers.
    """
    if e2e_stack_running():
        return values
    taken: set[int] = set()
    for key, protocols in _HOST_PORTS.items():
        if key not in values:
            continue
        port = int(values[key])
        if port not in taken and port_is_free(port, protocols):
            taken.add(port)
            continue
        for candidate in range(port + 1, port + 500):
            if candidate not in taken and port_is_free(candidate, protocols):
                say(f"host port {port} ({key}) is not bindable; using {candidate}")
                values[key] = str(candidate)
                _set_env_value(key, str(candidate))
                taken.add(candidate)
                break
        else:
            die(f"no free host port near {port} for {key}")
    return values


def git_sha() -> str:
    sha = capture(["git", "rev-parse", "--short", "HEAD"]).strip()
    dirty = capture(["git", "status", "--porcelain"]).strip()
    return f"{sha}{'-dirty' if dirty else ''}" if sha else "<unknown>"


# ---------------------------------------------------------------------------
# Test footage
# ---------------------------------------------------------------------------
#: Where the developer's own clips live. Copied in, never streamed from in
#: place, so the suite can never write into the folder you curate by hand.
DEFAULT_CLIP_SOURCE = REPO / "data" / "fake-cameras"


def prepare_clips() -> tuple[int, int]:
    """Fill the rig's video directory. Returns (synthetic, real) counts.

    Runs BEFORE the stack comes up, because the rig enumerates its folder once
    at startup and supervises one ffmpeg per file it finds. A clip that appears
    later is simply not served.

    Synthetic clips are rendered with the rig's own ffmpeg image, so neither
    the host nor the runner image needs ffmpeg for this step. Real clips —
    which the detection and LPR tiers require, because YOLOv8 will not
    classify a drawn rectangle as anything — are copied from
    ``data/fake-cameras`` unless E2E_CLIP_SOURCE points elsewhere.
    """
    clips = ARTIFACTS / "clips"
    clips.mkdir(parents=True, exist_ok=True)

    made = 0
    for spec in SYNTHETIC_CLIPS:
        target = clips / f"{spec.name}.mp4"
        if target.exists() and target.stat().st_size > 0:
            continue
        say(f"rendering {target.name} ({spec.seconds}s)")
        # --entrypoint: the image's default entrypoint is mediamtx itself,
        # which would try to parse ffmpeg's flags as its own and exit.
        argv = ffmpeg_command(spec)
        result = run(
            [
                "docker", "run", "--rm",
                "-v", f"{clips.resolve()}:/out",
                "--entrypoint", argv[0],
                FFMPEG_IMAGE,
            ]
            + argv[1:],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not target.exists():
            die(
                f"could not render {target.name}:\n"
                + result.stderr.decode("utf-8", errors="replace")[-800:]
            )
        made += 1

    # Every staged clip costs the rig a decode and a transcode, and costs the
    # stack a decode plus a motion pass per camera built on it. Staging a whole
    # footage folder is an easy way to melt a laptop that is also running a
    # real deployment, and the tests only ever need one. E2E_REAL_CLIPS raises
    # the cap when you want to exercise several cameras at once.
    limit = int(os.environ.get("E2E_REAL_CLIPS", "1"))
    source = Path(os.environ.get("E2E_CLIP_SOURCE") or DEFAULT_CLIP_SOURCE)
    available = real_clips(source)
    if len(available) > limit:
        # Smallest first: shortest decode, and enough for any assertion here.
        available = sorted(available, key=lambda p: p.stat().st_size)[:limit]
        say(f"staging {limit} of {len(real_clips(source))} real clips (E2E_REAL_CLIPS to change)")
    copied = 0
    for path in available:
        target = clips / path.name
        if target.exists() and target.stat().st_size == path.stat().st_size:
            continue
        shutil.copy2(path, target)
        copied += 1
    if copied:
        say(f"copied {copied} real clip(s) from {source}")

    real_total = len(real_clips(clips)) - len(SYNTHETIC_CLIPS)
    if real_total <= 0:
        say(
            f"no real footage in {source} — detection and LPR tests will skip. "
            "Point E2E_CLIP_SOURCE at a folder of clips to enable them."
        )
    return made, max(real_total, 0)


# ---------------------------------------------------------------------------
# Compose
# ---------------------------------------------------------------------------
def compose_args() -> list[str]:
    args = ["docker", "compose", "-p", PROJECT, "--env-file", str(ENV_FILE)]
    for name in COMPOSE_FILES:
        args += ["-f", name]
    return args


def teardown(volumes: bool) -> None:
    # Guard the destructive path. -v deletes volumes, and pointing that at the
    # wrong project would delete a real database. The project name is fixed in
    # this file, but an edit or a stray COMPOSE_PROJECT_NAME could change it,
    # so assert rather than assume.
    if volumes and PROJECT != "opennvr-e2e":
        die(f"refusing `down -v` for project {PROJECT!r}")
    say("tearing down" + (" and removing volumes" if volumes else ""))
    run(compose_args() + ["down", "--remove-orphans"] + (["-v"] if volumes else []))


def bring_up(profiles: list[str]) -> None:
    args = compose_args()
    for profile in profiles:
        args += ["--profile", profile]
    say("starting the stack (first run pulls images; this can take a while)")
    result = run(args + ["up", "-d", "--remove-orphans"])
    if result.returncode != 0:
        die("compose up failed; see the output above")


def wait_healthy(timeout: float) -> None:
    """Block until core reports healthy, mirroring start.sh's readiness loop."""
    say(f"waiting for {CORE_CONTAINER} to become healthy")
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        status = capture(
            [
                "docker",
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}",
                CORE_CONTAINER,
            ]
        ).strip()
        status = status or "absent"
        if status == "healthy":
            say("core is healthy")
            return
        if status == "none":
            say("core has no healthcheck; continuing")
            return
        if status == "unhealthy":
            run(compose_args() + ["logs", "--tail", "100", "opennvr-core"])
            die("core reported unhealthy")
        if status != last:
            say(f"  core: {status}")
            last = status
        time.sleep(3)

    run(compose_args() + ["logs", "--tail", "100", "opennvr-core"])
    die(
        f"core did not become healthy within {timeout:.0f}s. "
        "A cold first run pulls several images and migrates an empty database; "
        "retry, or raise --boot-timeout."
    )


def scrape_setup_token() -> str:
    """Read the one-time setup token from the core container's stdout.

    The server mints it at boot, prints it, and keeps it only in process
    memory — no API returns it. Scraping the log really is the only way in.

    Parsing lives in ``harness.compose`` so the host and the runner share one
    implementation. That module imports nothing outside the standard library
    precisely so this script can use it without the suite's dependencies.

    Absent is normal: it means the admin account is already claimed.
    """
    token = parse_setup_token(capture(["docker", "logs", CORE_CONTAINER]))
    if token:
        say("captured the first-time setup token")
    return token or ""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="tests/e2e/run.py",
        description="Run the OpenNVR end-to-end suite against an isolated stack.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="destroy the E2E stack and its volumes first. Required to exercise "
        "first-time setup, since the one-time token is only armed for an "
        "unclaimed admin account.",
    )
    parser.add_argument(
        "--down",
        action="store_true",
        help="tear the stack down when the run finishes (default: leave it up, "
        "so the next run starts in seconds).",
    )
    parser.add_argument(
        "-m",
        "--marker",
        default="smoke",
        help="pytest marker expression: smoke, detection, lpr, ui (default: smoke). "
        "Use '' to run everything.",
    )
    parser.add_argument(
        "--build", action="store_true", help="rebuild the runner image first."
    )
    parser.add_argument(
        "--boot-timeout",
        type=float,
        default=600.0,
        help="seconds to wait for core to become healthy (default: 600).",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="arguments passed through to pytest (put them after --).",
    )
    args = parser.parse_args(argv)

    if shutil.which("docker") is None:
        die("docker is not on PATH")
    if not (REPO / "docker-compose.e2e.yml").exists():
        die("docker-compose.e2e.yml is missing; are you in the right repo?")

    ensure_rig()

    if args.fresh:
        if ENV_FILE.exists():
            teardown(volumes=True)
        # The saved admin credentials belong to the database that was just
        # destroyed. Leaving them makes the next run try a login that cannot
        # work, which succeeds only in putting a misleading 403 in the log
        # before bootstrap falls back to claiming the account properly.
        (ARTIFACTS / "credentials.json").unlink(missing_ok=True)
        # RECORDINGS_PATH is a bind mount, so `down -v` does not touch it and
        # every run's footage piles up on a disk that is not large. Old cam-N
        # directories also carry identity markers from cameras that no longer
        # exist, which is exactly the orphaned-recordings state the product
        # goes out of its way to preserve — correct behaviour, but not what a
        # test run should start from.
        shutil.rmtree(ARTIFACTS / "recordings", ignore_errors=True)
        (ARTIFACTS / "recordings").mkdir(parents=True, exist_ok=True)
        env_values = write_env(force=True)
    else:
        env_values = write_env(force=False)
    env_values = ensure_free_host_ports(env_values)

    prepare_clips()

    # fakecams is profile-gated: passing the profile always is harmless when no
    # detection test runs, and avoids a second compose invocation when one does.
    bring_up(profiles=["fakecams"])
    wait_healthy(args.boot_timeout)

    child_env = os.environ.copy()
    child_env["E2E_SETUP_TOKEN"] = scrape_setup_token()
    # compose interpolates the runner service's own environment block from
    # these, so they have to be visible to the compose process too.
    child_env.update(env_values)

    pytest_args = list(args.pytest_args)
    if args.marker:
        pytest_args = ["-m", args.marker] + pytest_args
    if not pytest_args:
        # `docker compose run` with no arguments falls back to the service's
        # own CMD, which is `-m smoke`. Asking for every tier (-m "") would
        # then silently run just the smoke one and report a confident green.
        # Naming the test directory keeps the argument list non-empty.
        pytest_args = ["tests"]

    invocation = compose_args() + ["run", "--rm"]
    if args.build:
        invocation.append("--build")
    invocation += ["e2e"] + pytest_args

    say("running: pytest " + " ".join(pytest_args))
    result = subprocess.run(invocation, cwd=str(REPO), env=child_env)

    report = ARTIFACTS / "runs" / "report.md"
    if report.exists():
        say(f"run report: {report.relative_to(REPO)}")

    if args.down:
        teardown(volumes=False)
    else:
        say("stack left running; re-run in seconds, or tear down with --down")

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
