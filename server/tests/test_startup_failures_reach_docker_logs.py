# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A process that dies at startup has to say so where someone looks. (#547)

WHAT WENT WRONG, AND WHY IT TOOK HOURS.

``supervisord.conf`` gives each program a ``stderr_logfile`` inside the
container. Nothing tees those files to PID 1, so they reach nobody:
``docker logs opennvr_core`` shows supervisord's own lines and nothing
else. When the backend died at import on every install (#547), that
produced a container reporting "Started", a published port still
accepting TCP — Docker binds it whether or not anything listens inside —
and a log that said only ``exited: opennvr-backend (exit status 1; not
expected)``, forever. The ValidationError that explained it was in a
file an operator has to already know about.

The project had found this twice and routed around it both times rather
than fixing it. ``tests/e2e/harness/evidence.py`` collects those files
specially, because "grepping core's Docker output returns nothing, every
time". ``tests/host-hardening/test_setup_token_banner.sh`` guards a
forwarder in ``docker-entrypoint.sh`` that surfaces exactly seven lines
— the setup-token banner — and leaves every other line where it was.
Two workarounds, one missing pipe.

WHY THIS TEST READS supervisord.conf INSTEAD OF NAMING THE FILES.

Naming ``opennvr-backend-error.log`` and ``kai-c-error.log`` would pass
today and say nothing about the third program somebody adds next year,
whose stderr would be invisible in exactly the same way. The rule is
"every program supervisord runs can be seen to fail", so the list of
programs has to come from the thing that defines them.

This lives in the server suite rather than next to the banner test on
purpose: ``tests/host-hardening/`` is not run by any CI workflow, so a
guard placed there protects nothing on a pull request.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_SUPERVISORD = (REPO_ROOT / "supervisord.conf").read_text(encoding="utf-8")
_ENTRYPOINT = (REPO_ROOT / "docker-entrypoint.sh").read_text(encoding="utf-8")

#: A program whose stderr deliberately does not reach docker logs needs a
#: reason here. "We forgot" is the bug this test exists to catch, so an
#: unexplained entry defeats it.
DELIBERATELY_NOT_FORWARDED: dict[str, str] = {}


def _programs() -> dict[str, str]:
    """``[program:name]`` -> its ``stderr_logfile`` path."""
    out: dict[str, str] = {}
    current: str | None = None
    for line in _SUPERVISORD.splitlines():
        header = re.match(r"^\[program:([a-z0-9_-]+)\]\s*$", line.strip())
        if header:
            current = header.group(1)
            continue
        match = re.match(r"^stderr_logfile\s*=\s*(\S+)\s*$", line.strip())
        if match and current:
            out[current] = match.group(1)
    return out


def _forwarded_paths() -> set[str]:
    """Log paths docker-entrypoint.sh tails to the container's stdout.

    Both spellings the file uses: an explicit ``tail -n 0 -F <path>``,
    and a ``for`` loop over several paths.
    """
    paths = set(re.findall(r"tail\s+-n\s+0\s+-F\s+(/\S+)", _ENTRYPOINT))
    for group in re.findall(r"^\s*for\s+\w+\s+in\s+(/[^;]+?);\s*do\s*$",
                            _ENTRYPOINT, re.M):
        paths.update(re.findall(r"(/\S+)", group))
    return paths


def test_the_config_was_actually_parsed():
    """Guard the guard: if either regex drifts, every assertion below
    becomes a comparison of two empty sets and passes silently."""
    programs = _programs()

    assert len(programs) >= 2, (
        f"parsed only {len(programs)} programs out of supervisord.conf — "
        f"the section regex has drifted")
    assert "opennvr-backend" in programs
    assert _forwarded_paths(), (
        "found no forwarded log paths in docker-entrypoint.sh — either the "
        "forwarders are gone or this test can no longer see them")


def test_every_program_can_be_seen_to_fail():
    """The rule. A traceback has to reach `docker logs`.

    Not "the backend's traceback" — every program supervisord runs. A
    process whose failure is invisible turns a five-minute diagnosis into
    the one #547 describes.
    """
    forwarded = _forwarded_paths()
    missing = {
        name: path for name, path in _programs().items()
        if path not in forwarded and name not in DELIBERATELY_NOT_FORWARDED
    }

    assert not missing, (
        "supervisord sends these programs' stderr to a file inside the "
        "container and docker-entrypoint.sh does not forward it, so a "
        "crash is invisible to `docker logs`: "
        + ", ".join(f"{n} -> {p}" for n, p in sorted(missing.items()))
        + ". Add the path to the forwarding loop in docker-entrypoint.sh, "
          "or to DELIBERATELY_NOT_FORWARDED with a reason.")


def test_the_files_are_still_written():
    """Forwarding must TEE, not move.

    ``tests/e2e/harness/evidence.py`` collects these files after a failed
    run, and supervisord rotates them. Replacing the log paths with
    /dev/stdout would satisfy the test above and quietly delete the
    evidence trail — a fix that breaks the thing built to compensate for
    the bug.
    """
    devices = {
        name: path for name, path in _programs().items()
        if not path.startswith("/app/logs/")
    }

    assert not devices, (
        "stderr_logfile must stay a real file under /app/logs/ so the e2e "
        "evidence collector and supervisord's rotation keep working: "
        + ", ".join(f"{n} -> {p}" for n, p in sorted(devices.items())))


def test_the_setup_token_banner_forwarder_survives():
    """The ISSUE-29 forwarder, guarded somewhere CI actually runs.

    ``start.sh`` greps docker logs for this banner with ``-A 6``. Its
    only existing guard is in ``tests/host-hardening/``, which no
    workflow invokes — so on a pull request that forwarder is protected
    by nothing. Losing it means an operator is told "First-time setup is
    already complete" while the token sits unread in a log file.
    """
    assert re.search(r'grep\s+--line-buffered\s+-A\s+6\s+"first-time setup token"',
                     _ENTRYPOINT), (
        "the setup-token banner forwarder is gone or its grep range "
        "changed; start.sh's `grep -A 6 ... | tail -7` contract depends "
        "on it")
