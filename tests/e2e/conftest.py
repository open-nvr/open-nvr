# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fixtures and reporting hooks for the OpenNVR E2E suite.

Read this file before adding a test — it is the whole contract. A test asks for
``client`` (and gets authentication, correlation and automatic teardown), asks
for ``sandbox`` if it needs to name something, and asserts. Everything else
here exists so that a test does not have to think about it.

Three behaviours are worth knowing about:

**The stack boots once.** Bootstrapping is session-scoped, so the admin account
is claimed a single time and every test reuses the session. The one-time setup
token would otherwise force ``test_bootstrap.py`` to run first; instead the
session fixture records what it observed in ``SetupEvidence`` and that test
asserts against the record. The suite therefore has no required test order at
all, and any test can be run alone.

**Failures collect their own evidence.** ``pytest_runtest_makereport`` writes a
bundle per failure — see ``harness/evidence.py``.

**A broken stack is reported as such.** If core stops answering, the remaining
tests error with "the stack degraded after <test>" instead of producing a wall
of unrelated assertion failures. One dead container should cost one diagnosis,
not thirty.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# The suite runs from /work/tests/e2e with `harness` alongside it. Adding the
# directory explicitly keeps `python -m pytest`, `pytest`, and an IDE runner
# all resolving the package the same way.
sys.path.insert(0, str(Path(__file__).parent))

from harness import compose, routes, waiting  # noqa: E402
from harness.bootstrap import AdminSession, BootstrapError, ensure_admin  # noqa: E402
from harness.client import OpenNVRClient  # noqa: E402
from harness.evidence import Evidence, TestContext  # noqa: E402
from harness.report import TestReport, write_html_report  # noqa: E402
from harness.screenshots import capture, start_tracing, stop_tracing  # noqa: E402
from harness.fakecams import (  # noqa: E402
    NoFootage,
    camera_for_stream,
    pick_stream,
    wait_for_stream,
)
from harness.guards import (  # noqa: E402
    adapter_registered,
    require_adapter,
    require_recorded_footage,
    require_stream_receiving,
)
from harness.sandbox import Sandbox, new_sandbox  # noqa: E402
from pages import (  # noqa: E402
    AlertsPage,
    ApiTokensPage,
    CamerasPage,
    LivePage,
    PlaybackPage,
    RecordingSettingsPage,
    Shell,
    VehiclesPage,
)

_MARKERS = {
    "smoke": "fast, no detection — the tier CI runs on every PR",
    "media": "needs a camera that is actually publishing (the fake-camera rig)",
    "detection": "additionally needs Tier-0 to detect a real object",
    "lpr": "needs the fast_plate_ocr adapter and a vehicle clip",
    "ui": "drives the browser with Playwright",
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    """Endpoints, all reached by DNS on the internal Docker network.

    Nothing here is a published host port. That is deliberate: it keeps the
    suite immune to the port-binding lottery that makes host-side testing
    unreliable on Windows (#298).
    """

    core: str
    kaic: str
    nginx: str
    mediamtx: str
    detect: str
    nats: str
    fakecam_api: str
    fakecam_ip: str
    internal_key: str
    artifacts: Path

    @property
    def urls(self) -> dict[str, str]:
        return {
            "core": self.core,
            "kaic": self.kaic,
            "mediamtx": self.mediamtx,
            "detect": self.detect,
        }


@pytest.fixture(scope="session")
def config() -> Config:
    artifacts = Path(os.environ.get("E2E_ARTIFACTS", "/work/tests/e2e/.artifacts"))
    artifacts.mkdir(parents=True, exist_ok=True)
    return Config(
        core=os.environ.get("E2E_CORE_URL", "http://opennvr-core:8000"),
        kaic=os.environ.get("E2E_KAIC_URL", "http://opennvr-core:8100"),
        nginx=os.environ.get("E2E_NGINX_URL", "https://nginx"),
        mediamtx=os.environ.get("E2E_MEDIAMTX_API", "http://mediamtx:9997"),
        detect=os.environ.get("E2E_DETECT_METRICS", "http://detect-pipeline:9109"),
        nats=os.environ.get("E2E_NATS_URL", "nats://nats:4222"),
        fakecam_api=os.environ.get("E2E_FAKECAM_API", "http://fakecams:9997"),
        fakecam_ip=os.environ.get("E2E_FAKECAM_IP", "172.29.90.10"),
        internal_key=os.environ.get("INTERNAL_API_KEY", ""),
        artifacts=artifacts,
    )


# ---------------------------------------------------------------------------
# Session-scoped state
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def evidence(config: Config) -> Evidence:
    return Evidence(config.artifacts / "runs", config.urls)


@pytest.fixture(scope="session")
def admin(config: Config) -> AdminSession:
    """A logged-in admin. Claims the account on a fresh stack, else re-logs in.

    A failure here is fatal for the whole run, so the message has to say what
    to do next rather than just what went wrong.
    """
    try:
        return ensure_admin(
            config.core,
            config.artifacts,
            setup_token=os.environ.get("E2E_SETUP_TOKEN") or None,
            token_reader=compose.read_setup_token,
        )
    except BootstrapError as exc:
        pytest.exit(f"Could not obtain an admin session.\n\n{exc}", returncode=2)


@dataclass
class _StackState:
    """Tracks whether the shared stack is still usable."""

    degraded_after: str | None = None
    reason: str = ""
    results: list[dict] = field(default_factory=list)


@pytest.fixture(scope="session")
def stack_state() -> _StackState:
    return _StackState()


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def sandbox(request: pytest.FixtureRequest) -> Sandbox:
    """A private namespace with guaranteed teardown.

    Anything created through ``client`` is registered here automatically and
    removed in reverse order afterwards — including when the test fails. See
    ``harness/sandbox.py``.
    """
    box = new_sandbox(request.node.nodeid)
    yield box
    leaked = box.close()
    if leaked:
        # Loud, because leaked state is exactly what silently breaks a later
        # test in a shared-stack suite.
        request.node.warn(
            pytest.PytestWarning(
                f"sandbox {box.namespace} could not clean up:\n  "
                + "\n  ".join(leaked)
            )
        )


@pytest.fixture
def ctx(
    request: pytest.FixtureRequest, evidence: Evidence, sandbox: Sandbox
) -> TestContext:
    """Evidence bookkeeping for this test. Autoused via ``client``."""
    context = evidence.begin(request.node.nodeid, sandbox.namespace)
    request.node.stash[_CTX_KEY] = context
    return context


@pytest.fixture
def client(
    config: Config, admin: AdminSession, sandbox: Sandbox, ctx: TestContext
) -> OpenNVRClient:
    """An authenticated, sandbox-bound API client. The default way in."""
    api = OpenNVRClient(
        config.core,
        token=admin.access_token,
        device_token=admin.device_token,
        internal_key=config.internal_key,
        sandbox=sandbox,
        request_id_sink=ctx.request_ids,
    )
    # Lets a failure bundle read the audit trail as this test's principal.
    ctx.auth_token = admin.access_token
    ctx.device_token = admin.device_token
    # Closing the client is registered on the sandbox rather than done here,
    # and registered FIRST so the LIFO stack runs it LAST. pytest finalises
    # fixtures in reverse setup order, so a plain `api.close()` in this
    # teardown runs BEFORE the sandbox's cleanups — which then fail with
    # "client has been closed" and leak every entity the test created. That
    # bug shipped once and leaked five cameras in a single run.
    sandbox.track("close the API client", api.close)
    yield api


@pytest.fixture
def kaic(config: Config, sandbox: Sandbox) -> OpenNVRClient:
    """A client for KAI-C on :8100, which authenticates with the internal key."""
    api = OpenNVRClient(
        config.kaic,
        api_prefix="",
        internal_key=config.internal_key,
        sandbox=sandbox,
    )
    sandbox.track("close the KAI-C client", api.close)
    yield api


# ---------------------------------------------------------------------------
# Cameras that are actually publishing
# ---------------------------------------------------------------------------
# Two kinds, and the difference is not cosmetic. A generated clip is enough for
# anything on the media path — streaming, recording, playback. Detection and
# LPR need real footage, because Tier-0 runs YOLOv8 and it will not classify a
# drawn rectangle as anything, so no track and no visit is ever produced. A
# test asks for the kind it needs and skips with an explanation when the rig
# has none, instead of failing a minute later on an empty list.


def _stream_or_skip(config, *, real: bool):
    try:
        return pick_stream(config.fakecam_api, real=real)
    except NoFootage as exc:
        pytest.skip(str(exc))
    except Exception as exc:  # the rig is not running at all
        pytest.skip(
            f"The fake-camera rig is unreachable at {config.fakecam_api} ({exc}). "
            "Run through `python tests/e2e/run.py`, which starts it."
        )


def _camera_that_publishes(client, config, ctx, stream, label: str) -> dict:
    wait_for_stream(config.fakecam_api, stream.name)
    camera = camera_for_stream(client, config, stream, label=label)
    client.post(routes.CAMERA_PROVISION(camera["id"]))
    require_stream_receiving(client, camera["id"], ctx)
    return camera


@pytest.fixture
def publishing_camera(client, config, ctx) -> dict:
    """A camera on a generated clip, confirmed to be receiving video."""
    stream = _stream_or_skip(config, real=False)
    return _camera_that_publishes(client, config, ctx, stream, "media")


@pytest.fixture
def recorded_camera(client, config, ctx) -> tuple[dict, dict]:
    """A camera with at least one completed segment, and that segment.

    Distinct from ``publishing_camera``, which only guarantees frames are
    arriving. Recorded *history* additionally needs a segment to close, which
    takes a segment length — and a camera deleted seconds after it starts
    publishing never gets one at all.
    """
    stream = _stream_or_skip(config, real=False)
    camera = _camera_that_publishes(client, config, ctx, stream, "recorded")
    segment = require_recorded_footage(client, camera["id"], ctx)
    return camera, segment


@pytest.fixture
def detectable_camera(client, config, ctx) -> dict:
    """A camera on real footage, confirmed to be receiving video.

    For anything that needs Tier-0 to actually detect something.
    """
    stream = _stream_or_skip(config, real=True)
    return _camera_that_publishes(client, config, ctx, stream, "detect")


@pytest.fixture(autouse=True)
def _stack_gate(
    request: pytest.FixtureRequest, config: Config, stack_state: _StackState
):
    """Refuse to run against a stack that already broke, and notice when it does.

    Without this, a container that dies mid-run turns every later test red and
    the real event is buried. With it, the first casualty is named and
    everything after it says why it did not run.
    """
    # Pure-harness tests (harness/ parsers, sandbox ordering) never touch the
    # stack. Gating them would turn "no stack running" into a cascade of
    # confusing failures in tests that could not care less whether one exists.
    touches_stack = bool({"client", "kaic", "admin"} & set(request.fixturenames))
    if not touches_stack:
        yield
        return

    if stack_state.degraded_after:
        pytest.fail(
            "Not run: the stack degraded earlier in this session.\n"
            f"  first casualty : {stack_state.degraded_after}\n"
            f"  reason         : {stack_state.reason}\n"
            "  This is an infrastructure failure, not a defect in this test.\n"
            "  Inspect the stack, then re-run:  python tests/e2e/run.py --fresh",
            pytrace=False,
        )

    yield

    # Cheap post-check. Only core, because if core is answering the rest of the
    # stack's problems will surface as ordinary test failures with evidence.
    import httpx

    try:
        resp = httpx.get(f"{config.core.rstrip('/')}/health", timeout=10, verify=False)
        healthy = resp.status_code == 200
        detail = f"GET /health returned {resp.status_code}"
    except httpx.HTTPError as exc:
        healthy = False
        detail = f"GET /health raised {type(exc).__name__}: {exc}"

    if not healthy:
        stack_state.degraded_after = request.node.nodeid
        stack_state.reason = detail


# ---------------------------------------------------------------------------
# Browser (Playwright)
# ---------------------------------------------------------------------------
# The UI runs against nginx rather than core directly, deliberately: nginx is
# what proxies /hls/, /webrtc/ and /playback/ to MediaMTX, and a live view that
# works against core and not through the edge is not a working live view.
# Its certificate is self-signed by nginx-certs-init, hence ignore_https_errors.


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args, browser_name: str):
    """Drive real Google Chrome, because the product is H.264 end to end.

    Playwright's bundled ``chromium`` is built from open-source Chromium with
    the proprietary codecs left out. It therefore never offers H.264 in a
    WebRTC SDP and cannot decode it in a ``<video>`` element either. Every
    camera in this stack publishes H.264, so on that browser MediaMTX answers
    *"codecs not supported by client"*, the tile reads **"WHEP connection
    failed: 400"**, and the HLS fallback fares no better -- against a pipeline
    that is working correctly. Nothing in the browser-side error mentions a
    codec, which is what makes it expensive to diagnose; the screenshot in the
    report is what gives it away.

    ``--headless=new`` does not help. It selects the full headless browser
    rather than chrome-headless-shell, but the codec is absent from the binary
    either way -- this was tried, and MediaMTX went on refusing every offer.
    Only a build carrying licensed codecs works, so the runner image installs
    Chrome stable (see tests/e2e/Dockerfile) and this selects it.

    Muting audio keeps CI quiet: the alert bell plays a tone on unacknowledged
    criticals. The autoplay flag lets a tile start without a click, which is
    how the app behaves for a logged-in operator.
    """
    args = list(browser_type_launch_args.get("args", []))
    for flag in ("--mute-audio", "--autoplay-policy=no-user-gesture-required"):
        if flag not in args:
            args.append(flag)
    launch = {**browser_type_launch_args, "args": args}
    # "chrome" is a Chromium channel; asking Firefox or WebKit for it is an
    # immediate launch error. Those browsers are not part of the tier today,
    # but --browser is a standard pytest-playwright flag and someone will
    # eventually pass it.
    if browser_name == "chromium":
        launch["channel"] = "chrome"
    return launch


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args, config: Config):
    return {
        **browser_context_args,
        "base_url": config.nginx,
        "ignore_https_errors": True,
        "viewport": {"width": 1400, "height": 900},
        # The SPA registers a service worker (vite-plugin-pwa), which can serve
        # precached chunks from an older build and make a test assert against
        # code that is no longer deployed. Blocking it costs nothing here and
        # removes a whole class of "works locally, fails in CI" confusion.
        "service_workers": "block",
    }


@pytest.fixture
def ui_artifacts(config: Config, ctx: TestContext) -> Path:
    """Where this test's screenshots and trace go.

    Under the evidence root so CI already uploads it, and named by the same
    slug the failure bundle uses so the HTML report can link the two.
    """
    target = config.artifacts / "runs" / ctx.slug
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def traced_context(context, ui_artifacts: Path, request: pytest.FixtureRequest):
    """Record a Playwright trace, and keep it only if the test failed.

    Tracing captures DOM snapshots, network and a full action timeline. That is
    exactly what you want for a failure and far too heavy to keep for every
    pass, so it is always started and discarded unless something went wrong --
    much cheaper than trying to predict which tests will fail.
    """
    started = start_tracing(context)
    yield context
    if not started:
        return
    report = getattr(request.node, "rep_call", None)
    failed = bool(report and report.failed) or bool(
        getattr(request.node, "rep_setup", None) and request.node.rep_setup.failed
    )
    path = stop_tracing(context, ui_artifacts if failed else None)
    if path:
        request.node.stash[_TRACE_KEY] = str(path)


@pytest.fixture
def ui_page(page, ui_artifacts: Path, traced_context, request: pytest.FixtureRequest):
    """A browser page that leaves evidence, without assuming a session.

    Every GUI test should take this or something built on it, because the
    screenshot happens as it unwinds -- a test that takes Playwright's ``page``
    directly silently contributes nothing to the report. ``test_login.py`` uses
    this one, since its whole point is to sign in by hand.
    """
    yield page

    shot = capture(page, ui_artifacts, caption="at the end of the test")
    if shot is not None:
        request.node.stash[_SHOTS_KEY] = [(shot.caption, shot.data_uri())]


@pytest.fixture
def authed_page(ui_page, admin: AdminSession):
    """A browser already logged in.

    The SPA reads its token out of localStorage synchronously on first render
    (``app/src/auth/AuthContext.tsx``), so seeding those keys before any page
    script runs is genuinely equivalent to having logged in -- and it shares
    the ONE device enrolment the API client uses, which matters: the device
    firewall auto-approves the first browser to authenticate, so minting a
    second identity would leave it pending and 403.

    Only ``ui/test_login.py`` drives the real form. Every other UI test starts
    from here, because re-authenticating in each test is the single biggest
    source of slowness and flake in a browser suite.
    """
    seed = json.dumps(
        {
            "access": admin.access_token,
            "refresh": admin.refresh_token or "",
            "device": admin.device_token or "",
        }
    )
    ui_page.add_init_script(
        f"""(() => {{
            const t = {seed};
            try {{
                localStorage.setItem('opennvr.token', t.access);
                if (t.refresh) localStorage.setItem('opennvr.refresh_token', t.refresh);
                if (t.device) localStorage.setItem('opennvr.device_token', t.device);
            }} catch (e) {{ /* private mode: the assert will fail visibly */ }}
        }})()"""
    )
    return ui_page


# ---------------------------------------------------------------------------
# LPR availability
# ---------------------------------------------------------------------------
#: The OCR adapter, registered by the apps overlay's one-shot registrar.
PLATE_ADAPTER = "fast_plate_ocr"


@pytest.fixture
def plate_ocr(config, ctx):
    """Skip unless the OCR adapter is available; then assert it stays available.

    Two different situations look identical from a single check, and only one
    of them is a defect:

    * The apps overlay is not running at all, so the adapter was never
      registered. Nothing is broken; this tier is simply not configured here,
      and a skip says so.
    * The overlay *is* running and the adapter has vanished anyway -- which is
      what happens when core restarts, because KAI-C holds the registry in
      memory. That is the silent failure worth shouting about.

    Distinguishing them is the difference between a suite people trust and one
    that is permanently red on a machine that was never set up for LPR.
    """
    if not adapter_registered(config, PLATE_ADAPTER):
        pytest.skip(
            f"the {PLATE_ADAPTER!r} adapter is not registered with KAI-C, so "
            "no plate can be read. It is registered by the apps overlay's "
            "one-shot registrar (docker-compose.apps.yml), which this stack "
            "is not running."
        )
    require_adapter(config, PLATE_ADAPTER, ctx)


# ---------------------------------------------------------------------------
# Page objects
# ---------------------------------------------------------------------------
# One fixture per view. A GUI test asks for the page it is about and never
# constructs a locator, so a UI change is a one-line edit in selectors.py.


@pytest.fixture
def shell(authed_page) -> Shell:
    return Shell(authed_page)


@pytest.fixture
def cameras_page(authed_page) -> CamerasPage:
    return CamerasPage(authed_page)


@pytest.fixture
def live_page(authed_page) -> LivePage:
    return LivePage(authed_page)


@pytest.fixture
def playback_page(authed_page) -> PlaybackPage:
    return PlaybackPage(authed_page)


@pytest.fixture
def vehicles_page(authed_page) -> VehiclesPage:
    return VehiclesPage(authed_page)


@pytest.fixture
def alerts_page(authed_page) -> AlertsPage:
    return AlertsPage(authed_page)


@pytest.fixture
def recording_settings_page(authed_page) -> RecordingSettingsPage:
    return RecordingSettingsPage(authed_page)


@pytest.fixture
def api_tokens_page(authed_page) -> ApiTokensPage:
    return ApiTokensPage(authed_page)


# ---------------------------------------------------------------------------
# Reporting hooks
# ---------------------------------------------------------------------------
_CTX_KEY = pytest.StashKey[TestContext]()
_EVIDENCE_KEY = pytest.StashKey[Evidence]()
_RESULTS_KEY = pytest.StashKey[list]()
_SHOTS_KEY = pytest.StashKey[list]()
_TRACE_KEY = pytest.StashKey[str]()


def pytest_configure(config: pytest.Config) -> None:
    for name, description in _MARKERS.items():
        config.addinivalue_line("markers", f"{name}: {description}")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report: pytest.TestReport = outcome.get_result()

    # Publish each phase on the item so fixtures can see the verdict during
    # teardown. The trace fixture needs this: it has to decide whether to keep
    # or discard the recording, and that is only knowable after the call phase.
    setattr(item, f"rep_{report.when}", report)

    if report.when != "call" and not (report.when == "setup" and report.failed):
        return

    session_evidence = item.session.stash.get(_EVIDENCE_KEY, None)
    context = item.stash.get(_CTX_KEY, None)

    # Snapshot the wait timeline NOW. waiting._RECORDS is cleared at the start
    # of every test, so a run-level report that read it later would find only
    # the last test's waits -- and nothing at all for passing tests.
    waits = [
        (rec.describe, rec.elapsed, rec.attempts, rec.satisfied)
        for rec in waiting.records()
    ]

    entry = TestReport(
        node_id=item.nodeid,
        outcome=_outcome_of(report),
        duration=report.duration,
        slug=context.slug if context else "",
        waits=waits,
        guard_notes=list(context.guard_notes) if context else [],
    )
    _results(item).append(entry)

    if not report.failed or session_evidence is None or context is None:
        return

    context.request_ids = list(dict.fromkeys(context.request_ids))
    entry.failure_text = str(report.longrepr)
    target = session_evidence.capture(context, str(report.longrepr))
    report.sections.append(
        ("E2E evidence", f"Failure bundle written to {target}\n  -> {target}/ISSUE.md")
    )


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    """Attach artifacts produced during teardown to this test's report entry.

    Screenshots and traces are only written as the fixtures unwind, which is
    after ``makereport`` has already built the entry -- so they are stitched on
    here rather than being lost.
    """
    entries = item.session.stash.get(_RESULTS_KEY, [])
    entry = next((e for e in reversed(entries) if e.node_id == item.nodeid), None)
    if entry is None:
        return
    shots = item.stash.get(_SHOTS_KEY, None)
    if shots:
        entry.screenshots = shots
    trace = item.stash.get(_TRACE_KEY, None)
    if trace:
        entry.trace = trace


def _outcome_of(report: pytest.TestReport) -> str:
    if report.skipped:
        return "xfailed" if getattr(report, "wasxfail", None) is not None else "skipped"
    if report.failed:
        return "error" if report.when == "setup" else "failed"
    return "passed"


def _results(item: pytest.Item) -> list:
    return item.session.stash.setdefault(_RESULTS_KEY, [])


@pytest.fixture(scope="session", autouse=True)
def _register_session_evidence(request: pytest.FixtureRequest, evidence: Evidence):
    """Publish the session Evidence where the report hooks can reach it.

    A hook is not a fixture and cannot request one, so the object is stashed on
    the session instead.
    """
    request.session.stash[_EVIDENCE_KEY] = evidence
    request.session.stash.setdefault(_RESULTS_KEY, [])
    yield

    entries = request.session.stash.get(_RESULTS_KEY, [])
    if not entries:
        return

    terminal = request.config.pluginmanager.get_plugin("terminalreporter")

    # The markdown report stays: it reads well in a terminal and on GitHub.
    markdown = evidence.write_run_report(
        [
            {
                "node_id": e.node_id,
                "outcome": e.outcome,
                "duration": e.duration,
                "slug": e.slug,
            }
            for e in entries
        ]
    )
    html_path = write_html_report(
        evidence.root, entries, evidence.provenance_rows()
    )

    if terminal:
        terminal.write_line("")
        terminal.write_line(f"E2E run report : {markdown}")
        terminal.write_line(f"E2E HTML report: {html_path}")
