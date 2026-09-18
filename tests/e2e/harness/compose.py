# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Everything that shells out to ``docker``.

Two jobs, both about *observing* the stack rather than driving it — lifecycle
belongs to ``run.py`` on the host, because a container cannot cleanly bring up
the compose project it is itself a member of:

1. **Log collection** for the evidence bundle. Scoped by time window and
   filtered by the correlation token, so a failure report carries the twenty
   lines that belong to one test instead of nine services' worth of noise.
2. **Reading the first-time-setup token**, which the server prints to stdout
   and exposes through no API at all.

The Docker socket is mounted read-only, so nothing here can mutate the stack
even by accident.

Every function degrades rather than raises when the socket is unavailable: a
missing socket must cost you the *evidence*, not the test run. ``run.py`` also
scrapes the token host-side and passes it through ``E2E_SETUP_TOKEN``, so even
bootstrap survives without it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone

log = logging.getLogger(__name__)

#: Container names carry this prefix in the E2E project. See the header of
#: docker-compose.e2e.yml for why they must be renamed at all.
NAME_PREFIX = os.environ.get("E2E_NAME_PREFIX", "opennvr_e2e")

#: compose service name -> container name. Only services the suite observes.
SERVICES: dict[str, str] = {
    "core": f"{NAME_PREFIX}_core",
    "db": f"{NAME_PREFIX}_db",
    "mediamtx": f"{NAME_PREFIX}_mediamtx",
    "detect-pipeline": f"{NAME_PREFIX}_detect_pipeline",
    "nats": f"{NAME_PREFIX}_nats",
    "nats-apps": f"{NAME_PREFIX}_nats_apps",
    "yolov8-adapter": f"{NAME_PREFIX}_yolov8_adapter",
    "nginx": f"{NAME_PREFIX}_nginx",
    "fakecams": f"{NAME_PREFIX}_fakecams",
}

_DOCKER_TIMEOUT = 60


class DockerUnavailable(RuntimeError):
    """The docker CLI or socket is not usable from inside the runner."""


def docker_available() -> bool:
    """True when we can collect evidence. Cheap enough to call per failure."""
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _run(args: list[str]) -> str:
    try:
        result = subprocess.run(
            args, capture_output=True, timeout=_DOCKER_TIMEOUT, check=False
        )
    except FileNotFoundError as exc:
        raise DockerUnavailable("the docker CLI is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise DockerUnavailable(f"`{' '.join(args)}` timed out") from exc

    # docker logs writes the container's stderr to our stderr; both halves are
    # wanted, and interleaving order is not something we can recover anyway.
    out = result.stdout.decode("utf-8", errors="replace")
    err = result.stderr.decode("utf-8", errors="replace")
    if result.returncode != 0 and not out and not err:
        raise DockerUnavailable(f"`{' '.join(args)}` exited {result.returncode}")
    return out + err


def utcnow() -> datetime:
    """Timestamp for bounding a later ``logs(since=...)`` call."""
    return datetime.now(timezone.utc)


def logs(
    container: str,
    *,
    since: datetime | None = None,
    grep: str | None = None,
    tail: int | None = None,
) -> str:
    """Return one container's logs, optionally windowed and filtered.

    Args:
        container: the container name (see ``SERVICES``).
        since: only lines after this instant — pass the test's start time so
            the bundle holds that test's activity and not the whole session's.
        grep: keep only lines containing this substring. The suite passes the
            sandbox namespace, which appears in the User-Agent of every request
            the test made and in every entity name it created.
        tail: cap the number of lines *before* filtering.

    Returns:
        The log text, or an explanatory line if it could not be read. Never
        raises: evidence collection runs while a test is already failing, and
        an exception here would replace the real diagnosis with a secondary one.
    """
    args = ["docker", "logs"]
    if since is not None:
        args += ["--since", since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")]
    if tail is not None:
        args += ["--tail", str(tail)]
    args.append(container)

    try:
        text = _run(args)
    except DockerUnavailable as exc:
        return f"<log collection unavailable: {exc}>"

    if grep:
        kept = [line for line in text.splitlines() if grep in line]
        if not kept:
            return f"<no lines mentioning {grep!r} in {container}>"
        return "\n".join(kept)
    return text


def container_file(container: str, path: str, tail: int = 400) -> str:
    """Read a file from inside a container.

    Needed because ``opennvr-core`` does not log to Docker at all:
    ``supervisord.conf`` routes both the backend and KAI-C to files under
    ``/app/logs/``, so ``docker logs opennvr_core`` shows only supervisord's
    own dozen startup lines. Anything that matters — tracebacks, boot errors —
    is inside the container.
    """
    try:
        return _run(
            ["docker", "exec", container, "sh", "-c", f"tail -n {int(tail)} {path} 2>/dev/null"]
        )
    except DockerUnavailable as exc:
        return f"<could not read {path} from {container}: {exc}>"


def health(container: str) -> str:
    """``healthy`` / ``unhealthy`` / ``starting`` / ``none`` / ``absent``.

    Mirrors the readiness loop in ``start.sh``: a container with no healthcheck
    reports ``none``, and one that does not exist yet reports ``absent`` — both
    are ordinary states while a stack is coming up, not errors.
    """
    fmt = "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}"
    try:
        text = _run(["docker", "inspect", "--format", fmt, container]).strip()
    except DockerUnavailable:
        return "unknown"
    return text.splitlines()[0].strip() if text else "absent"


def running(container: str) -> bool:
    try:
        text = _run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container]
        ).strip()
    except DockerUnavailable:
        return False
    return text.lower().startswith("true")


# ---------------------------------------------------------------------------
# The first-time-setup token
# ---------------------------------------------------------------------------
_BANNER_MARKER = "first-time setup token"

# secrets.token_urlsafe(32) -> 43 url-safe characters. The shape alone is not
# enough to identify it: the banner draws rules out of 64 '-' characters, and
# those match a naive [A-Za-z0-9_-]{20,} just as well as the token does. That
# exact bug shipped once and cost a full stack boot to find, so the alphanumeric
# floor below is load-bearing and test_harness.py pins it.
_TOKEN_SHAPE = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_MIN_ALNUM = 8


def parse_setup_token(text: str) -> str | None:
    """Pull the one-time setup token out of core's startup banner.

    Kept as a pure function so both callers share one implementation — the
    runner (through ``read_setup_token``) and ``run.py`` on the host, which
    scrapes the token before the runner container exists.

    The banner looks like::

        ================================================================
         OpenNVR first-time setup token (one-time use)
        ----------------------------------------------------------------
          RW0my12O9RNN5s8ZkSrB3GWk46E2T7paBtC19Kzpfo0
        ----------------------------------------------------------------

    Returns None when no banner is present, which normally just means the
    admin account was already claimed.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if _BANNER_MARKER not in line.lower():
            continue
        # Scan forward rather than assuming a fixed offset, so a change to the
        # banner's decoration does not silently break this.
        for candidate in lines[index + 1 : index + 8]:
            token = candidate.strip()
            if _TOKEN_SHAPE.match(token) and _alnum_count(token) >= _MIN_ALNUM:
                return token
    return None


def _alnum_count(value: str) -> int:
    return sum(1 for char in value if char.isalnum())


def read_setup_token(container: str | None = None) -> str | None:
    """Scrape the one-time setup token from the core container's logs.

    ``server/main.py`` prints it at startup and keeps it only in process
    memory, so the log genuinely is the only place to read it from.
    """
    name = container or SERVICES["core"]
    try:
        text = _run(["docker", "logs", name])
    except DockerUnavailable as exc:
        log.info("Could not read setup token from %s: %s", name, exc)
        return None
    return parse_setup_token(text)


__all__ = [
    "SERVICES",
    "NAME_PREFIX",
    "DockerUnavailable",
    "docker_available",
    "logs",
    "container_file",
    "health",
    "running",
    "read_setup_token",
    "parse_setup_token",
    "utcnow",
]
