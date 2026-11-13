# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Turn a failure into a report someone can act on.

An E2E suite earns its keep only if a red run explains itself. Otherwise every
failure costs a re-run with extra logging, and people quietly stop trusting it.

Each failing test leaves ``artifacts/runs/<test>/`` containing:

    ISSUE.md          the whole story, ready to paste into a bug report
    logs/<svc>.log    Docker output per service, windowed to this test
    logs/core.log     core's in-container files — see below
    audit.txt         the product's own audit trail and system events
    metrics.txt       Tier-0 counters, MediaMTX paths, adapter registry
    nats.jsonl        bus events seen during the test, when a probe was running

**Core needs special handling, and finding that out cost a run.** The obvious
design is to tag every request with a distinctive User-Agent and grep the
container logs for it. That does not work here: ``supervisord.conf`` routes the
backend and KAI-C to files under ``/app/logs/``, so ``docker logs`` shows only
supervisord's own startup lines — and the request log does not reach those
files either. Grepping core's Docker output returns nothing, every time.

So core contributes two things instead. Its in-container log files, which is
where tracebacks land, and the product's own audit trail from ``/audit-logs``
and ``/system/events`` — structured, timestamped and queryable, which is
better evidence than a text scrape would ever have been. The User-Agent tag is
still sent, and still useful for the services that *do* log to Docker.

Provenance matters as much as logs. A stack bug reported without image digests
is nearly untriageable weeks later, so ``ISSUE.md`` always names the tags,
digests and git SHA the run actually used.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import compose, waiting

#: Services that actually write to Docker's log stream. ``core`` is
#: deliberately absent: supervisord routes the backend and KAI-C to files
#: inside the container (/app/logs/), so `docker logs opennvr_core` returns
#: only supervisord's own startup lines. Core is collected separately, from
#: those files and from the product's own audit trail.
_LOG_SERVICES = ("mediamtx", "detect-pipeline", "nats", "db", "nginx", "fakecams")

#: The files supervisord writes inside the core container.
_CORE_LOG_FILES = (
    "/app/logs/opennvr-backend-error.log",  # tracebacks land here first
    "/app/logs/opennvr-backend.log",
    "/app/logs/server.log",
    "/app/logs/kai-c-error.log",
    "/app/logs/kai-c.log",
)

_METRIC_TIMEOUT = 10.0


@dataclass
class TestContext:
    """Per-test bookkeeping, opened at setup and consumed on failure."""

    node_id: str
    namespace: str
    started_at: datetime
    request_ids: list[str] = field(default_factory=list)
    #: Set by the ``client`` fixture so the bundle can query the audit trail
    #: as the same principal the test used.
    auth_token: str | None = None
    device_token: str | None = None
    guard_notes: list[str] = field(default_factory=list)
    nats_messages: list[dict] = field(default_factory=list)

    @property
    def slug(self) -> str:
        """A filesystem-safe directory name for this test."""
        keep = []
        for char in self.node_id:
            keep.append(char if (char.isalnum() or char in "-_.") else "_")
        return "".join(keep)[:120]


class Evidence:
    """Collects and writes failure bundles. One instance per session."""

    def __init__(self, artifacts_root: Path, urls: dict[str, str]) -> None:
        self.root = artifacts_root
        self.urls = urls
        # Start each run from an empty directory. A bundle left over from an
        # earlier failure would otherwise sit next to a green run's report,
        # and the first thing anyone does with an artifacts directory is open
        # the ISSUE.md in it — finding one that describes a failure that no
        # longer happens is worse than finding nothing.
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- lifecycle -------------------------------------------------------
    def begin(self, node_id: str, namespace: str) -> TestContext:
        waiting.reset_records()
        return TestContext(
            node_id=node_id, namespace=namespace, started_at=compose.utcnow()
        )

    # -- the bundle ------------------------------------------------------
    def capture(self, ctx: TestContext, failure_text: str) -> Path:
        """Write the full bundle for one failed test. Returns its directory.

        Never raises. This runs while a test is already failing, and an
        exception here would replace the real diagnosis with a secondary one
        about the diagnostic machinery — the most frustrating possible outcome.
        """
        target = self.root / ctx.slug
        try:
            target.mkdir(parents=True, exist_ok=True)
            self._write_logs(ctx, target)
            self._write_core_logs(target)
            self._write_audit_trail(ctx, target)
            self._write_metrics(target)
            self._write_nats(ctx, target)
            (target / "ISSUE.md").write_text(
                self._issue_markdown(ctx, failure_text, target), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001 - deliberate catch-all
            try:
                (target / "EVIDENCE-FAILED.txt").write_text(
                    f"Collecting evidence failed: {type(exc).__name__}: {exc}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
        return target

    def _write_logs(self, ctx: TestContext, target: Path) -> None:
        log_dir = target / "logs"
        log_dir.mkdir(exist_ok=True)
        for service in _LOG_SERVICES:
            container = compose.SERVICES.get(service)
            if not container or not compose.running(container):
                continue
            # Two views, because each answers a different question. The
            # filtered one says "what did MY test do"; the tail says "what was
            # this service doing at the time", which is where an unrelated
            # crash or a restart shows up.
            filtered = compose.logs(
                container, since=ctx.started_at, grep=ctx.namespace
            )
            window = compose.logs(container, since=ctx.started_at, tail=400)
            (log_dir / f"{service}.log").write_text(
                f"=== lines mentioning {ctx.namespace} ===\n{filtered}\n\n"
                f"=== everything since the test started ===\n{window}\n",
                encoding="utf-8",
            )

    def _write_core_logs(self, target: Path) -> None:
        """Copy core's log files out of the container.

        Core writes nothing useful to Docker's stream, so without this the
        service most likely to explain a failure would contribute nothing at
        all to the bundle.
        """
        container = compose.SERVICES.get("core")
        if not container or not compose.running(container):
            return
        chunks = [
            f"===== {path} =====\n{compose.container_file(container, path)}\n"
            for path in _CORE_LOG_FILES
        ]
        (target / "logs" / "core.log").write_text("\n".join(chunks), encoding="utf-8")

    def _write_audit_trail(self, ctx: TestContext, target: Path) -> None:
        """Pull the product's own record of what happened.

        This is the real substitute for grepping core's request log. OpenNVR
        persists audited actions to the database and serves them at
        ``/audit-logs``, with ``/system/events`` alongside — structured,
        timestamped, and far more useful than a text scrape would have been.
        """
        if not ctx.auth_token:
            return
        headers = {"Authorization": f"Bearer {ctx.auth_token}"}
        if ctx.device_token:
            headers["X-Device-Token"] = ctx.device_token
        base = self.urls.get("core", "").rstrip("/")
        if not base:
            return

        chunks = []
        for label, path in (
            ("audit log", "/api/v1/audit-logs?limit=100"),
            ("system events", "/api/v1/system/events?limit=100"),
        ):
            chunks.append(
                f"===== {label} (newest 100) =====\n"
                + self._fetch(f"{base}{path}", headers=headers)
                + "\n"
            )
        (target / "audit.txt").write_text("\n".join(chunks), encoding="utf-8")

    def _write_metrics(self, target: Path) -> None:
        chunks: list[str] = []
        for label, url in (
            ("Tier-0 metrics", f"{self.urls.get('detect', '')}/metrics"),
            ("Tier-0 health", f"{self.urls.get('detect', '')}/health"),
            ("MediaMTX paths", f"{self.urls.get('mediamtx', '')}/v3/paths/list"),
            ("KAI-C health", f"{self.urls.get('kaic', '')}/health"),
            ("Core health", f"{self.urls.get('core', '')}/health"),
        ):
            chunks.append(f"===== {label} =====\n{self._fetch(url)}\n")

        # The adapter registry needs the internal key, and its absence is the
        # single most common cause of a silent LPR failure — always capture it.
        key = os.environ.get("INTERNAL_API_KEY")
        if key and self.urls.get("kaic"):
            chunks.append(
                "===== KAI-C adapters =====\n"
                + self._fetch(
                    f"{self.urls['kaic']}/api/v1/adapters",
                    headers={"X-Internal-Api-Key": key},
                )
                + "\n"
            )
        (target / "metrics.txt").write_text("\n".join(chunks), encoding="utf-8")

    def _write_nats(self, ctx: TestContext, target: Path) -> None:
        if not ctx.nats_messages:
            return
        with (target / "nats.jsonl").open("w", encoding="utf-8") as handle:
            for message in ctx.nats_messages:
                handle.write(json.dumps(message, default=str) + "\n")

    @staticmethod
    def _fetch(url: str, headers: dict[str, str] | None = None) -> str:
        if not url or url.startswith("/"):
            return "<not configured>"
        try:
            resp = httpx.get(url, timeout=_METRIC_TIMEOUT, verify=False, headers=headers)
        except httpx.HTTPError as exc:
            return f"<unreachable: {type(exc).__name__}: {exc}>"
        return f"HTTP {resp.status_code}\n{resp.text}"

    # -- the report ------------------------------------------------------
    def _issue_markdown(self, ctx: TestContext, failure_text: str, target: Path) -> str:
        failed_wait = waiting.failed_record()
        lines: list[str] = [
            f"# E2E failure: `{ctx.node_id}`",
            "",
            f"*Captured {datetime.now(timezone.utc).isoformat(timespec='seconds')}*",
            "",
            "## What happened",
            "",
        ]

        if failed_wait is not None:
            lines += [
                f"A wait for **{failed_wait.describe}** never came true.",
                "",
                f"- budget: `{failed_wait.budget:.0f}s`, exhausted after "
                f"`{failed_wait.elapsed:.1f}s` over `{failed_wait.attempts}` attempts",
            ]
            if failed_wait.last_error:
                lines.append(f"- last error: `{failed_wait.last_error}`")
            else:
                lines.append(
                    f"- last value seen: `{_short(failed_wait.last_value)}`"
                )
            lines.append("")
        else:
            lines += ["The test failed on an assertion rather than a timeout.", ""]

        lines += ["```", failure_text.strip() or "<no traceback captured>", "```", ""]

        if ctx.guard_notes:
            lines += ["## Preconditions", ""]
            lines += [f"- {note}" for note in ctx.guard_notes]
            lines.append("")

        lines += [
            "## Reproduce",
            "",
            "```bash",
            f"python tests/e2e/run.py -- -k {_pytest_selector(ctx.node_id)}",
            "```",
            "",
            "## Environment",
            "",
            self._provenance_table(),
            "",
            "## Timeline of waits",
            "",
        ]

        records = waiting.records()
        if records:
            lines.append("| outcome | waited for | elapsed | attempts |")
            lines.append("|---|---|---|---|")
            for rec in records:
                mark = "ok" if rec.satisfied else "**TIMEOUT**"
                lines.append(
                    f"| {mark} | {rec.describe} | {rec.elapsed:.1f}s | {rec.attempts} |"
                )
        else:
            lines.append("_No waits were performed._")
        lines.append("")

        if ctx.request_ids:
            shown = ctx.request_ids[-25:]
            lines += [
                "## Server request ids",
                "",
                "The server mints its own id per request and returns it in "
                "`X-Request-ID`. Grep the core log for any of these to find the "
                "exact records:",
                "",
                "```",
                *shown,
                "```",
                "",
            ]

        lines += [
            "## Attached files",
            "",
            f"- `logs/` — Docker output per service since this test started, "
            f"filtered to `{ctx.namespace}` where it appears; `logs/core.log` "
            "holds core's in-container files, which is where its tracebacks go",
            "- `audit.txt` — the product's own audit trail and system events",
            "- `metrics.txt` — Tier-0 counters, MediaMTX paths, adapter registry",
        ]
        if (target / "nats.jsonl").exists():
            lines.append("- `nats.jsonl` — bus events observed during the test")
        lines.append("")
        return "\n".join(lines)

    def provenance_rows(self) -> list[tuple[str, str]]:
        """Which build this run actually exercised, as raw rows.

        Shared by the markdown and HTML reports so the two can never disagree.
        Image digests and the git SHA are the difference between a stack bug
        that is triageable weeks later and one that is not.
        """
        rows = [
            ("git sha", os.environ.get("E2E_GIT_SHA", "<not recorded>")),
            ("core tag", os.environ.get("CORE_TAG", "<default>")),
            ("adapter tag", os.environ.get("ADAPTER_TAG", "<default>")),
            ("compose project", os.environ.get("E2E_COMPOSE_PROJECT", "<unset>")),
            ("runner platform", f"{platform.system()} {platform.release()}"),
            ("python", platform.python_version()),
        ]
        core = compose.SERVICES.get("core")
        if core:
            rows.append(("core image", _image_ref(core)))
            rows.append(("core health", compose.health(core)))
        detect = compose.SERVICES.get("detect-pipeline")
        if detect:
            rows.append(("detect-pipeline image", _image_ref(detect)))
        return rows

    def _provenance_table(self) -> str:
        """The same rows, as a markdown table."""
        out = ["| | |", "|---|---|"]
        out += [f"| {name} | `{value}` |" for name, value in self.provenance_rows()]
        return "\n".join(out)

    # -- run-level summary ----------------------------------------------
    def write_run_report(self, results: list[dict]) -> Path:
        """One summary for the whole run; also usable as a CI job summary."""
        path = self.root / "report.md"
        passed = sum(1 for r in results if r["outcome"] == "passed")
        failed = [r for r in results if r["outcome"] == "failed"]
        errored = [r for r in results if r["outcome"] == "error"]
        skipped = sum(1 for r in results if r["outcome"] == "skipped")

        lines = [
            "# OpenNVR E2E run",
            "",
            f"*{datetime.now(timezone.utc).isoformat(timespec='seconds')}*",
            "",
            f"**{passed} passed, {len(failed)} failed, {len(errored)} errored, "
            f"{skipped} skipped**",
            "",
            self._provenance_table(),
            "",
        ]

        if failed or errored:
            lines += ["## Needs attention", "", "| test | outcome | evidence |", "|---|---|---|"]
            for result in failed + errored:
                slug = result.get("slug", "")
                link = f"[`{slug}/ISSUE.md`]({slug}/ISSUE.md)" if slug else "—"
                lines.append(f"| `{result['node_id']}` | {result['outcome']} | {link} |")
            lines.append("")

        lines += ["## All tests", "", "| test | outcome | duration |", "|---|---|---|"]
        for result in results:
            lines.append(
                f"| `{result['node_id']}` | {result['outcome']} | "
                f"{result.get('duration', 0.0):.1f}s |"
            )
        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        return path


def _image_ref(container: str) -> str:
    """The image a container actually runs, by digest where one exists."""
    try:
        digest = compose._run(
            ["docker", "inspect", "--format", "{{index .Config.Image}}", container]
        ).strip()
    except compose.DockerUnavailable:
        return "<unknown>"
    return digest or "<unknown>"


def _short(value: object, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _pytest_selector(node_id: str) -> str:
    """The ``-k`` expression that selects just this test."""
    return node_id.rsplit("::", 1)[-1].split("[", 1)[0]


__all__ = ["Evidence", "TestContext"]
