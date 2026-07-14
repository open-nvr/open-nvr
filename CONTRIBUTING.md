# Contributing to OpenNVR

Thanks for thinking about contributing. The shortest possible summary: fork,
branch off `main`, write tests, open a PR with a small / focused changeset.
Everything else on this page is detail.

## What we want most

In rough order of impact:

1. **New AI adapters.** YOLOv11, BoT-SORT tracking, pose estimation, CLIP for
   semantic search, PaddleOCR, depth estimation, audio-event detection. See
   the [AI adapter authoring guide](https://github.com/open-nvr/ai-adapter#-write-your-own-adapter)
   in the sister repo — the SDK keeps a working adapter at around 30 lines.
2. **New example apps.** Everything under `examples/` is a copy-as-template
   starting point. New examples that pair an existing adapter with a
   different predicate (fall detection, package theft, dwell-time heatmaps)
   land cleanly because the shape is already proven.
3. **Bug reports with reproducers.** A focused issue with the camera type,
   adapter version, exact log line, and minimum repro saves hours.
4. **Documentation.** Docs that teach the same thing twice or that drifted
   away from the code are higher-impact than they look — if you spot one,
   PR the fix.
5. **Security-hardening improvements.** Reproducer + suggested mitigation
   gets prioritised. For vulnerabilities themselves, see [SECURITY.md](SECURITY.md)
   first — please don't open public issues for those.

## Setting up to contribute

You need both repos side-by-side:

```bash
git clone https://github.com/open-nvr/open-nvr.git
git clone https://github.com/open-nvr/ai-adapter.git
cd open-nvr
```

Then pick the path that matches what you're doing:

### Fast iteration (Docker)

Best for testing your changes end-to-end against the same stack the standard stack
install runs on. Slower per-iteration than the bare-metal path below because
every change rebuilds an image.

```bash
cp .env.example .env
./scripts/generate-secrets.sh --write
./start.sh build               # builds opennvr-core locally from your tree
```

### Bare-metal dev shell (no Docker)

Best for working on the Python code itself. Each subsystem runs in its own
terminal, restarts fast on change, hot-reloads via uvicorn / Vite.

```bash
# Backend (Python)
cd open-nvr/server
uv venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
uv sync
alembic upgrade head
python start.py

# KAI-C (in a second terminal)
cd open-nvr/kai-c
uv venv && source .venv/bin/activate
uv sync
python start.py

# Frontend (in a third terminal)
cd open-nvr/app
npm install
npm run dev                              # http://localhost:5173

# AI adapter monolith (in a fourth terminal, optional)
cd ai-adapter
uv venv && source .venv/bin/activate
uv sync --extra all --extra cpu
uv run python download_models.py
uv run uvicorn app.main:app --reload --port 9100

# MediaMTX (in a fifth terminal — binary, not a venv)
./mediamtx open-nvr/mediamtx.local.yml
```

Bare-metal walkthrough with prerequisites and gotchas:
[`docs/LOCAL_SETUP.md`](docs/LOCAL_SETUP.md).

## Branching and PR flow

```bash
# Start a branch off main
git checkout -b feature/<short-name>     # or fix/<short-name>

# Make your changes, write tests, run tests locally
pytest                                    # in server/, kai-c/, and any examples/* you touched

# Push and open the PR against main
git push -u origin feature/<short-name>
```

**Tests are required.** Every behaviour change needs a test. PRs without tests
are blocked — not as a paperwork ritual, but because the codebase only stays
maintainable if its invariants are encoded somewhere a future contributor will
see.

**One topic per PR.** Drive-by refactors that happen alongside the real change
go in their own PR. Reviewers can't push back on "the small refactor looks
weird" when the PR title is about something else.

**Reviewers respond fast and pull no punches.** Don't take it personally; we
review code the same way we expect to be reviewed.

## Coding standards

The standards below are enforced by CI. Local `pre-commit install` runs them
on every commit; CI runs them again on every PR.

### Python (server, kai-c, examples, ai-adapter)

- **Python 3.11+.** Type hints required on function signatures.
- **Black + isort + ruff.** Line length 100, isort uses the `black` profile.
- **mypy** is informational, not blocking — fix what you can.
- **Docstrings** on every public function. Google style. If a comment
  explains *why* (not *what*) it gets in; if it parrots the code it doesn't.

```python
from typing import Optional

def get_camera_by_id(camera_id: int, db: Session) -> Optional[Camera]:
    """Retrieve a camera by its database ID.

    Returns None if no camera with that ID exists. Caller is responsible
    for treating None as "not found" rather than "transient DB error" —
    those raise DatabaseError.
    """
    return db.query(Camera).filter(Camera.id == camera_id).first()
```

### TypeScript (`app/`)

- **ESLint** configured in `app/eslint.config.js`. `npm run lint` and
  `npm run type-check`.
- **No `any`** unless absolutely necessary (and noted in a comment).
- **Functional components with hooks.** PascalCase files for components,
  camelCase for utilities.

### Commit messages

[Conventional Commits](https://www.conventionalcommits.org/) — the bot in CI
parses these for the changelog generator, so the format matters:

```
<type>(<scope>): <subject>

<optional body>
<optional footer>
```

Common types: `feat`, `fix`, `docs`, `refactor`, `perf`, `test`, `chore`.

```text
feat(adapters): add YOLOv11 detector with ByteTrack tracking
fix(auth): keep session alive across active streaming reconnects
docs(quickstart): clarify Windows path syntax for RECORDINGS_PATH
```

## Adding a new AI adapter

The SDK pattern is the supported way to ship a new adapter — a contract-
compliant container, ~30 lines of Python, no changes to OpenNVR itself.

```bash
pip install opennvr-adapter-sdk
```

```python
from datetime import datetime, timezone
from opennvr_adapter_sdk import (
    AdapterApp, AdapterService, BodyShape, BODY_BYTES_KEY,
    HardwareEvaluationResponse, HardwareVerdict,
    InferResponse, ModelInfo,
)

class MyDetector(AdapterService):
    def load(self):
        # Eagerly load weights. Called once at startup before /health goes green.
        ...

    def is_ready(self) -> bool:
        return True

    def fingerprint(self) -> str | None:
        return "sha256:..."          # sha256 of the model weights on disk

    def model_info(self) -> ModelInfo:
        return ModelInfo(
            name="my-model", version="1.0.0",
            framework="onnx", fingerprint=self.fingerprint(),
        )

    def hardware_evaluation(self) -> HardwareEvaluationResponse:
        return HardwareEvaluationResponse(
            verdict=HardwareVerdict.OK, reasoning="ok",
            checked_at=datetime.now(timezone.utc), details={},
        )

    def infer(self, payload) -> InferResponse:
        frame_bytes = payload[BODY_BYTES_KEY]
        # ... run your model ...
        return InferResponse(
            model_name="my-model", model_version="1.0.0",
            inference_ms=42,
            result={"detections": [{"label": "person", "confidence": 0.93}]},
        )

app = AdapterApp(
    service=MyDetector(),
    name="my-detector", version="1.0.0", vendor="me", license="MIT",
    tasks_advertised=["object_detection"],
    body_shape=BodyShape.IMAGE,
).fastapi_app
```

Run with `uvicorn my_module:app --port 9001`, point KAI-C's
`ADAPTER_URL` at it, and you're online.

Full walkthrough in the [SDK README](https://github.com/open-nvr/ai-adapter/blob/main/opennvr_adapter_sdk/README.md)
and reference implementations in `ai-adapter/adapters/yolov8/`,
`adapters/whisper/`, `adapters/piper/`.

**Adding to the reference monolith instead?** The `ai-adapter` repo's
`app/adapters/` directory uses an older `BaseAdapter` plugin pattern that
predates the SDK. It's still supported for contributors extending the
bundled server, but it's not the path we recommend for new adapters — the
SDK pattern gives you a contract-compliant container with no OpenNVR code
changes. See [`ai-adapter/CONTRIBUTING.md`](https://github.com/open-nvr/ai-adapter/blob/main/CONTRIBUTING.md)
for the monolith path.

## Adding a new example app

Examples are first-class community contribution surface. The shape is
deliberate so reviewers can read one and know where everything lives in
the others.

1. Open a [discussion](https://github.com/open-nvr/open-nvr/discussions) with
   your idea, the camera setup you'll demo against, and the adapter(s) you'll
   chain.
2. Fork, branch, and `cp -r` one of the shipped examples whose shape is
   closest to yours as your starting template.
3. Replace the predicate (`zone.contains?`, the dwell-time state machine,
   etc.) with your domain logic. Keep `alerts.py`, the config-loading shape,
   and the test surface roughly as they are.
4. Open a PR. Reviewers look for: clarity, test coverage of the predicate,
   honest documentation of what the example does NOT yet do, and consistency
   with the rest of the gallery.

If yours lands as a first-party example, your name goes on it.

## Getting your app into the App Store

An example folder teaches the pattern; the **App Store index** is how an app
becomes browsable and one-click installable in every deployment's App
Catalog. Publishing an image and adding one reviewed entry to
`server/config/apps_index.yml` is a documented, validated, PR-based flow
(the Homebrew-tap / Home-Assistant-add-on model). Full walkthrough —
build on the SDK, publish + pin your image digest, add the entry, run
`make validate-apps-index`, open the PR — in
[`docs/CONTRIBUTING_APPS.md`](docs/CONTRIBUTING_APPS.md).

## Running tests

Every subsystem has its own `pytest` suite. Run them where they live:

```bash
# Backend
cd server && pytest

# KAI-C orchestrator
cd kai-c && pytest

# Example apps (each is independent)
cd examples/intrusion-detection && pytest
cd examples/camera-agent && uv sync --extra dev && pytest

# Frontend
cd app && npm test
```

Coverage reports: `pytest --cov=.` (Python) or `npm test -- --coverage`
(frontend).

CI runs the full matrix on every PR. Local green is a strong signal but not
a guarantee — CI also runs the smoke matrix that boots each adapter image
end-to-end, which won't run on your laptop without Docker.

## PR checklist

The PR template will ask you to confirm these. Going through the list before
opening the PR saves a review cycle.

- [ ] Code follows the style guide above (`pre-commit` is green).
- [ ] Tests added or updated for the behaviour change.
- [ ] Docs updated if user-visible behaviour changed.
- [ ] CHANGELOG entry added under `## Unreleased` if the change is
      user-visible.
- [ ] PR title follows Conventional Commits.
- [ ] PR description states *what* the change does and *why* — a sentence
      each is fine.

## Issue labels

| Label | Description |
|-------|-------------|
| `bug` | Something is broken |
| `enhancement` | New feature or feature request |
| `documentation` | Docs improvements |
| `good first issue` | Reasonable starting point for new contributors |
| `help wanted` | We'd accept a PR for this; nobody is actively working on it |
| `security` | Security-relevant; coordinate via [SECURITY.md](SECURITY.md) |
| `ai-adapter` | Adapter authoring / SDK changes |
| `performance` | Latency / throughput / memory regressions |

## Code of conduct

We want this to stay a project people enjoy contributing to. The expectation is welcoming, inclusive language and respectful disagreement; personal attacks, harassment, doxxing, and inflammatory off-topic posting are not part of that. Report violations to **contact@opennvr.org** — reports are confidential.

## Getting help while you contribute

For deeper reference reading, [`docs/AI_ADAPTER_CONTRACT.md`](docs/AI_ADAPTER_CONTRACT.md) covers the wire spec and [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md) covers the threat model. Open a [Discussion](https://github.com/open-nvr/open-nvr/discussions) for "is this the right approach?" questions before you start writing code, and an [Issue](https://github.com/open-nvr/open-nvr/issues) for "I think I found a bug" reports.


## Licensing of contributions (CLA / DCO)

OpenNVR is dual-licensed (AGPL-3.0-or-later + a commercial license —
see [docs/LICENSING.md](docs/LICENSING.md)), which requires the project
to hold dual-licensing rights over every contributed line of the AGPL
core:

- **AGPL components** (server, app, kai-c, camera-agent, examples):
  PRs require the one-time [CLA](docs/CLA.md). You keep your
  copyright; you grant the project the right to also offer your
  contribution under the commercial license.
- **Apache-2.0 SDKs** (`sdk/`, the adapter contract): no CLA — add a
  DCO `Signed-off-by:` line to your commits.
