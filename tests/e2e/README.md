# OpenNVR end-to-end test framework

Drives a **real, isolated OpenNVR stack** — real MediaMTX, real Postgres, real
Tier-0, real OCR — through the journeys an operator actually performs.

The rest of the repo has ~2,700 unit tests. They cover components. This covers
the seams *between* components, which is where the failures that reach users
live. In this system those failures are also overwhelmingly **silent**: Tier-0
that never leaves motion calibration, an OCR adapter that unregistered when
core restarted, a webhook aimed at a hostname that stopped existing. Each shows
up as an empty page with nothing in any log, and survives a green unit matrix
indefinitely.

So the framework is built around one idea: **turn silence into a diagnosis.**

---

## Table of contents

1. [Quick start](#quick-start)
2. [How it is put together](#how-it-is-put-together)
3. [Why the runner lives inside Docker](#why-the-runner-lives-inside-docker)
4. [Isolation: it cannot touch your real deployment](#isolation-it-cannot-touch-your-real-deployment)
5. [Tiers, and which need real footage](#tiers-and-which-need-real-footage)
6. [Adding a test](#adding-a-test)
7. [The harness, module by module](#the-harness-module-by-module)
8. [When something fails](#when-something-fails)
9. [Tuning for a slow or busy machine](#tuning-for-a-slow-or-busy-machine)
10. [Known issues the suite documents](#known-issues-the-suite-documents)
11. [Traps worth knowing about](#traps-worth-knowing-about)

---

## Quick start

```bash
python tests/e2e/run.py                    # smoke tier, warm stack (~2 min)
python tests/e2e/run.py --fresh            # wipe and start clean
python tests/e2e/run.py -m media           # needs a publishing camera
python tests/e2e/run.py -m detection       # needs real footage
python tests/e2e/run.py -m ui              # Playwright
python tests/e2e/run.py -m ""              # everything
python tests/e2e/run.py --down             # tear the stack down afterwards
```

Anything after `--` goes to pytest untouched:

```bash
python tests/e2e/run.py -- -k camera_lifecycle -x
python tests/e2e/run.py -- --randomly-seed=12345      # reproduce an order
```

The stack is left running by default, so a re-run starts in seconds. Use
`--fresh` when you need a clean database — it is the only way to exercise
first-time setup, because the one-time token is armed only while the admin
account is unclaimed.

**Requirements:** Docker, Python 3.11+, and a checkout with git history (the
runner fetches the fake-camera rig from the `fake-camera` branch when absent).

---

## How it is put together

```
                    HOST                     │           DOCKER
                                             │
  run.py ──┬─ stage clips (ffmpeg image)     │   ┌─────────────────────────┐
           ├─ write .artifacts/e2e.env       │   │  opennvr_e2e_core       │
           ├─ docker compose up ─────────────┼──►│  _mediamtx  _db  _nats  │
           ├─ wait for core healthy          │   │  _detect_pipeline       │
           ├─ scrape the setup token         │   │  _nginx  _fakecams      │
           └─ docker compose run e2e ────────┼──►│  opennvr_e2e_runner     │
                                             │   │   └─ pytest + Playwright │
                                             │   └─────────────────────────┘
```

**`run.py` owns the host side** — isolation, stack lifecycle, clip staging, and
scraping the first-time-setup token that core prints only to stdout. It exists
rather than a documented compose command line because the isolation is a dozen
environment variables applied *together*; miss one and the test stack adopts
your real volumes.

**The runner container owns everything else** — authentication, fixtures,
assertions, evidence.

---

## Why the runner lives inside Docker

Every app's contract surface is `expose:`-only, core binds `127.0.0.1`, and the
MediaMTX and NATS admin APIs are internal. Reaching them from the host means
publishing ports — which on Windows is an intermittent, machine-specific flake
source: WinNAT re-rolls its reserved ranges at every boot, and a **single**
unbindable port silently aborts *all* of a container's publications (#298).

From inside `opennvr_internal` every service is reachable by DNS name, nothing
is published, and behaviour is identical on a laptop and a CI runner. It also
puts the runner in an internal CIDR, which `DeviceFirewallMiddleware` admits
without a device token.

The runner is Python + `pytest-playwright` rather than a JS toolchain: `app/`
has no test runner at all, and one language means the API and browser tiers
share fixtures.

---

## Isolation: it cannot touch your real deployment

You can run this while your normal stack is up. `--fresh` destroys only the
E2E one. Verified: 18 dev containers untouched across a `down -v`, and zero
`e2e-` rows left in the database after a full run.

Compose isolates *some* things by project name and not others, so `run.py`
closes the gaps by hand:

| | |
|---|---|
| volumes, networks | project-prefixed automatically → already isolated |
| `container_name` | **not** prefixed — it is absolute, so two projects both declaring `opennvr_core` collide. Every service is renamed `opennvr_e2e_*`. |
| published ports | compose **concatenates** `ports` across `-f` files. An override can add a publication but never remove or replace one, so the base file's ports are parameterised and shifted into a +20000 band. |
| subnet | two bridges cannot share one → `172.29.0.0/16` |
| `RECORDINGS_PATH` | a scratch dir under `.artifacts/`, never `./recordings` |

Because the E2E stack renames containers, anything that addresses core **by
container name** must be redirected too — `BACKEND_HOST` (the segment-complete
webhook) and `MTX_AUTHJWTJWKS` (stream JWT validation). Both were found the
hard way; see [Traps](#traps-worth-knowing-about).

---

## Tiers, and which need real footage

Tiers are markers, not directories. A test joins one by declaring it.

| Marker | Tests | Needs | Runtime |
|---|---|---|---|
| `smoke` | 47 | nothing but the stack | ~1.5 min |
| `media` | 7 | a camera publishing a generated clip | ~3 min |
| `detection` | 4 | **real footage** + Tier-0 detecting an object | ~6 min |
| `lpr` | 3 | the above + the `fast_plate_ocr` adapter | several min |
| `ui` | 10 | Playwright against nginx | ~45 s |

74 tests in total. `smoke` covers bootstrap, camera lifecycle, RBAC, security
posture, the alerts inbox, occupancy and the harness's own guards.

**Generated clips cannot drive detection, and this is not a limitation worth
working around.** Tier-0 gates on motion and then runs YOLOv8, which classifies
COCO objects. A drawn rectangle is never a person or a car, so a synthetic clip
produces motion, no detection, no track, and no visit — and every downstream
assertion would fail for a reason that has nothing to do with OpenNVR.

So:

- **Streaming, recording, playback** use generated clips (`harness/clips.py`).
  Tiny, deterministic, no binaries in the repo.
- **Detection and LPR** use real clips. Drop a few in `data/fake-cameras/`, or
  point `E2E_CLIP_SOURCE` at a folder. Without them those tiers **skip** with a
  message saying exactly why.

Only one real clip is staged by default — each costs the rig a transcode and
the stack a decode plus a motion pass. Raise it with `E2E_REAL_CLIPS=3`.

---

## Adding a test

Copy `_template_test.py` into `tests/` and change the middle. **Five rules**,
and only the first two need thought.

### 1. Take `client`

It is authenticated, it tags every request so a failure can find its log
lines, and **everything it creates is deleted afterwards, pass or fail**. Do
not build your own HTTP client.

```python
def test_something(client, sandbox, config):
    camera = client.create_camera(
        label="mine",
        rtsp_url=f"rtsp://{config.fakecam_ip}:8554/{sandbox.name('stream')}",
        ip_address=config.fakecam_ip,
    )
    # no teardown — see harness/sandbox.py
```

### 2. Wait with `eventually()` and a **named budget**

Never `time.sleep`. Nothing in OpenNVR reports completion — there is no job
queue and no callback — so polling is correct. A bare sleep is slow when it
works and silent when it does not.

```python
from harness.budgets import BUDGETS
from harness.waiting import eventually

visit = eventually(
    lambda: client.json(routes.EVENTS, params={"camera_id": cam_id})["events"],
    budget=BUDGETS.VISIT_APPEARS,
    describe=f"Tier-0 to record a visit for camera {cam_id}",
)
```

`describe` is the headline of the failure report — make it specific. On
timeout the message carries **the last value actually seen**, which is usually
the diagnosis by itself.

### 3. Never depend on another test's data

Seed what you need. The suite shuffles order (`pytest-randomly`) on purpose,
so a test that needs a predecessor will fail — which is the point. Any test
must run alone:

```bash
python tests/e2e/run.py -- tests/test_rbac.py -k granted
```

### 4. Declare a marker

`smoke`, `media`, `detection`, `lpr`, `ui`. Markers are strict — a typo is a
collection error, not a test that quietly never runs.

```python
pytestmark = pytest.mark.smoke        # whole module
@pytest.mark.detection                # or one test
```

### 5. Write no cleanup code

If you create something `client` does not model yet, **add a helper to
`harness/client.py`** rather than a teardown here. Then every future test gets
correct teardown for it for free. The helper pattern:

```python
def create_thing(self, *, label: str, **extra) -> dict:
    sandbox = self._require_sandbox("create_thing")
    thing = self.post(routes.THINGS, json_body={...}).json()
    sandbox.track(
        f"thing {thing['id']}",
        lambda: self.delete(routes.THING(thing["id"]), expect=None),
    )
    return thing
```

### Guarding a precondition

If your test depends on something that can be silently absent, assert it
**first** with a guard. `require_adapter` turns "timed out waiting for
plate_text" into "the fast_plate_ocr adapter is not registered with KAI-C".

```python
from harness.guards import require_adapter

def test_plates(client, detectable_camera, config, ctx):
    require_adapter(config, "fast_plate_ocr", ctx)
    ...
```

Use the **non-raising** `adapter_registered()` and `pytest.skip` when the thing
is legitimately *not configured* rather than broken — see the `plate_ocr`
fixture in `tests/test_lpr.py` for the distinction, which is the difference
between a suite people trust and one that is permanently red.

### Fixtures available

| Fixture | Gives you |
|---|---|
| `client` | authenticated, sandbox-bound API client |
| `sandbox` | unique namespace + guaranteed teardown |
| `config` | endpoints, the rig's IP, the internal key |
| `admin` | the logged-in session (username, password, TOTP) |
| `ctx` | evidence context — pass to guards |
| `kaic` | client for KAI-C on :8100 (internal key auth) |
| `publishing_camera` | a camera confirmed to be receiving video |
| `recorded_camera` | `(camera, segment)` with a completed segment |
| `detectable_camera` | a camera on **real** footage (skips if none) |
| `page` / `authed_page` | Playwright page, the second already signed in |

---

## The harness, module by module

```
run.py                 host: isolation, lifecycle, clips, setup-token scrape
conftest.py            fixtures, the infra gate, failure reporting
_template_test.py      copy me

harness/
  client.py            OpenNVRClient — the only way tests talk to the API
  sandbox.py           per-test namespace + LIFO teardown
  bootstrap.py         setup token → first-time-setup → TOTP → login
  waiting.py           eventually() — the only permitted wait
  budgets.py           every timeout, named once
  routes.py            every API path, named once
  guards.py            preconditions that fail loudly
  clips.py             ffmpeg clip specs; real-vs-generated
  fakecams.py          the rig: list streams, make cameras from them
  images.py            small JPEGs on demand (evidence payloads)
  compose.py           docker: logs, health, the setup-token parser
  evidence.py          failure bundles and the run report
```

Two rules hold this together:

**Nothing is named twice.** API paths live only in `routes.py`; timeouts only
in `budgets.py`. A renamed route is a one-line fix, not a grep across 40 files.

**The sandbox owns teardown.** Its undo stack is LIFO, because later entities
reference earlier ones — a camera permission has to go before the user and the
camera it names. The client's own `close` is registered *first* so it runs
*last*; pytest finalises fixtures in reverse setup order, so a plain
`api.close()` in the client fixture would run before the sandbox cleanups and
leak everything the test created.

---

## Proving the suite can go red

A GUI suite that cannot fail is worse than none, so these were run
deliberately rather than assumed. Re-run them after any change to the harness.

**Break one selector.** Point `ALERT_BELL` in `harness/selectors.py` at names
that do not exist, then `python tests/e2e/run.py -m ui -- -k bell`:

```
1 failed, 99 deselected in 57.93s
E  - waiting for get_by_test_id("alert-bell-DELIBERATELY-BROKEN")
       .or_(get_by_title("NoSuchAlarmsTitle"))
       .or_(get_by_role("button", name="NoSuchAlarmsButton")).first
```

Exactly one test fails, and it prints the whole broken chain — every value
greppable straight back to the one entry to fix. The captured trace labels
each step `<name> [selectors.py]` as well.

**Kill the stack mid-run.** `docker stop opennvr_e2e_core` after the first
test:

```
2 passed, 88 deselected, 10 errors in 21.19s
  Not run: the stack degraded earlier in this session.
    first casualty : tests/ui/test_shell.py::...[chromium-/cameras]
    reason         : GET /health raised ConnectError: [Errno 111] Connection refused
    This is an infrastructure failure, not a defect in this test.
```

Errors, not failures, and 21 seconds rather than ten minutes of 30s selector
timeouts. The distinction matters: a wall of red selector failures is exactly
what a stack outage would otherwise look like, and it sends you hunting the
wrong bug.

**Without the product hooks.** The full 100-test run above was green on
`test/e2e-suite` with `feat/ui-test-hooks` *not* merged. That is the property
that keeps the two branches independent — every selector is a fallback chain,
so the hooks make the tests robust rather than possible.

---

## When something fails

Each failure writes `.artifacts/runs/<test>/`:

| File | Contents |
|---|---|
| `ISSUE.md` | the whole story, ready to paste into a bug report |
| `logs/*.log` | Docker output per service, windowed to this test |
| `logs/core.log` | core's **in-container** log files — see below |
| `audit.txt` | the product's own audit trail and system events |
| `metrics.txt` | Tier-0 counters, MediaMTX paths, adapter registry |
| `nats.jsonl` | bus events seen during the test, when a probe ran |

`ISSUE.md` leads with the wait that never came true **and the last value it
saw**, then the repro command, then provenance — image digests and git SHA,
without which a stack bug is close to untriageable weeks later.

The directory is emptied at the start of every run, so what is in it always
belongs to the last one.

**Core is a special case.** `docker logs opennvr_e2e_core` shows only
supervisord's startup lines: `supervisord.conf` routes the backend and KAI-C
to files under `/app/logs/`, and the HTTP request log reaches neither those
files nor Docker. So the bundle collects core's in-container files (where
tracebacks land) plus the audit trail from `/audit-logs` and `/system/events`
— structured and queryable, and better evidence than a text scrape.

**Infra failure is reported as infra failure.** A health gate runs between
tests; if core stops answering, the remaining tests report *"the stack degraded
after `<test>`"* rather than a wall of unrelated assertion failures.

---

## Tuning for a slow or busy machine

Every timeout is a named budget, overridable per environment **without editing
a test**:

```bash
E2E_BUDGET_TIER0_CALIBRATED=900 python tests/e2e/run.py -m detection
```

The failure message always names the knob to turn.

**Tier-0 is the first thing starved on a busy host**, and it degrades in a way
that looks like a product failure. Running this alongside another OpenNVR stack
on an 8-core laptop produced 13s frame latency against a 0.5s budget, with the
detector cutting its regions from 8 to 2.

That matters because of how the motion gate works: on footage with continuous
motion the gate never settles, and Tier-0 force-opens it after **150 frames**
rather than blocking forever. At its default 2 fps that is a little over a
minute idle — and several minutes starved.

If detection times out, look at the counters before suspecting the code:

```bash
docker exec opennvr_e2e_detect_pipeline python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9109/metrics').read().decode())" | grep -E "frames_total|calibrating|detections_total"
```

- `tier0_frames_total` **absent entirely** → no worker ever decoded a frame.
  That is a *source* problem (check ffprobe in the pipeline logs), not a gate
  problem, and the guard says so.
- present but equal to the calibrating count → the gate has not opened yet.
  Raise the budget.

---

## Known issues the suite documents

An `xfail` here is a real defect, recorded so the suite stays green and tells
you the moment it is fixed — it flips to XPASS. It is **never** a weakened
assertion.

| Test | Defect |
|---|---|
| `test_a_frame_can_be_extracted_from_past_footage` | `ffmpeg` is not installed in the `opennvr-core` image, so `GET /api/v1/recordings/frame` returns **502 on every install**. `_extract_recording_frame` shells out to it; the runtime stage of the root `Dockerfile` installs supervisor, curl, gosu, libpq5 and a few X libraries, and no ffmpeg. Adding it should turn this XPASS. |
| `test_the_camera_search_filters_the_table` | The Cameras page search box is **inert**. `Cameras.tsx` debounces the text into `useCameras({ q })` and it is serialised onto `GET /api/v1/cameras/`, but `get_cameras` declares only `skip`, `limit` and `active_only`. FastAPI drops undeclared query parameters silently, the route never reads `request.query_params`, and nothing filters client-side. Typing narrows nothing — while looking alive, because the query key changes, a request goes out and the table dims and repopulates with the same rows. Accepting `q` on the endpoint, or removing the box, turns this XPASS. |

---

## Traps worth knowing about

Each of these cost real time to find, and each is the same shape: something
that *looks* like it works while doing nothing.

**Compose passes an explicit env allowlist per service.** A variable a service
block does not name is invisible inside the container no matter what the env
file says. `RECORDING_SEGMENT_SECONDS` silently stayed at 60s this way, putting
a full minute of dead wait into every recording assertion.

**Things addressed by container name break when containers are renamed.**
`BACKEND_HOST` (segment-complete webhook) and `MTX_AUTHJWTJWKS` (stream JWT
validation). The first failed invisibly because the recording reconciler
backfills from disk minutes later and covered for it; the second made every
authenticated RTSP read 401, which Tier-0 reports only as *"source
unavailable"*. Nothing in either chain mentions the real cause.

**`DETECT_VISITS_ENABLED` defaults to off.** Tier-0 then detects normally and
persists nothing. It says `visit persistence = off` once at startup and never
again.

**`.gitignore` swallows test files.** The repo ignores `test_*.py` and
`*_test.py` globally and re-includes each suite by negation. A new suite is
invisible to git until its own `!tests/<x>/test_*.py` negation exists —
otherwise the commit ships a harness with no tests in it.

**Playwright's image tag and its pip version must move together.** The runner
image ships browser binaries for exactly its own Playwright version, and the
Python package looks them up by a version-stamped path. An unpinned
`playwright` quietly installs a newer release whose browsers are not in the
image, and every UI test dies with *"Executable doesn't exist at
/ms-playwright/…"*. Both are pinned; bump `FROM` and `playwright==` in the same
change.

**Playwright's bundled Chromium cannot play the product's video.** It is built
from open-source Chromium without proprietary codecs, so it neither offers
H.264 in a WebRTC SDP nor decodes it in a `<video>`. Every camera here is
H.264, so MediaMTX answers *"codecs not supported by client"* and the tile
reads **"WHEP connection failed: 400"** — against a pipeline that is fine.
`--headless=new` does not fix it; the codec is not in the binary. The runner
image installs Chrome stable and `conftest.py` sets `channel="chrome"`. If the
video tests ever start failing this way again, check that Chrome survived an
image rebuild before looking at MediaMTX.

**Playwright locators are live, not snapshots.** Holding
`rows.filter(has_text="unacked").first` and then acknowledging that row does
not leave you pointing at the acked row -- it stops matching, and the locator
silently re-resolves to the *next* unacked row, which still has its Ack
button. The assertion can then never come true, and the message ("expected
count 0, actual 1") gives no hint that it is looking at a different element
than the one you acted on. Assert on a count, or on an identity that survives
the change. This one passed for weeks because a fresh stack usually holds
exactly one unacknowledged alarm.

**A blind `.first` click is not a fallback.** It is a different action that
happens not to raise. `open_first_recording` used to click
`get_by_role("button").first` when no Play button was on screen; on the
Recordings page that is the sidebar toggle, so it opened navigation, left the
camera group collapsed, and the test timed out against a button that was never
going to render. Address the control you mean — page objects take the camera
name for exactly this reason.

**The Recordings list auto-expands only for a single camera.** With two or
more, every Play button stays out of the DOM until the right group header is
clicked. A test that passed alone will fail once another test leaves footage
behind, which makes it look like flake and is not.

**Playwright matches placeholders as substrings.** `get_by_placeholder("192.168.1.100")`
also matches the RTSP field's `rtsp://192.168.1.100:554/stream1` on the same
form, and every `fill()` then dies on a strict-mode violation that names
neither field helpfully. `selectors.py` passes `exact=True`, so a declared
placeholder must be the **whole** literal from the markup, not a readable
prefix.

**`RECORDINGS_PATH` is a bind mount**, so `docker compose down -v` does not
clear it. Old `cam-N` directories carry identity markers from cameras that no
longer exist, which the product then (correctly) refuses to serve.

**`--fresh` is required to test first-time setup.** The one-time token is armed
only while the admin account is unclaimed, so those assertions skip on a warm
stack rather than failing.

**A new user cannot reach a single page until MFA is enrolled.**
`ProtectedShell` renders the enrolment QR code for anyone whose `mfa_enabled`
is false, so a browser test that creates a user, mints a token and navigates
lands on the QR screen — where every assertion about *absence* passes
vacuously and every assertion about presence times out. The API layer has no
such gate, which is why the API RBAC tests never needed it. Use
`harness.bootstrap.enrol_mfa()`; `pages/` has nothing to click here on purpose.

**A GUI test that only asserts absence is not a test.** "The forbidden row is
missing" is equally true of the login page, the MFA screen, an error boundary
and a page that never finished loading. Pair it with something that must be
present — `test_a_viewer_does_not_see_an_ungranted_camera` grants a second
camera purely so the negative half has a control.

**A wrong TOTP counts as a failed login.** Five lock the account for three
minutes and wedge the run. Never retry a code inside the same 30-second
window, and aim failed-login tests at throwaway users.
