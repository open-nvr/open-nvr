# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""An app's adapter ships WITH it — the Package Delivery half.

The bug this guards (observed live 2026-09-21): the package-detection
adapter had a catalog entry in ``adapters_index.yml`` and a published
image on GHCR, and no compose service anywhere in the repo. So nothing
started it, nothing registered it, ``ai.capabilities()`` never listed
it, and the app's picker correctly fell back to a VQA model and
correctly graded its counts "fair" — on a deployment whose operator had
trained the detector, pushed the image, and had every reason to expect
"good".

A catalog entry is a thing you can read about. It is not a thing that
runs. This is the same failure ``test_lpr_adapter_wiring`` was written
for, one app over, so it is guarded the same way and in the same
string-level style.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_APPS = (REPO_ROOT / "docker-compose.apps.yml").read_text()
_INDEX = (REPO_ROOT / "server/config/adapters_index.yml").read_text()
_APP = (REPO_ROOT / "examples/package-delivery/package_delivery.py").read_text()


def _service_block(name: str, *, lead: int = 0) -> str:
    """The compose text for one service, plus any comment block above it.

    A helper rather than raw ``index`` arithmetic in each test: when the
    service is missing, these must fail with a sentence saying so, not
    with a ValueError from string slicing — a guard whose failure mode
    is a stack trace teaches nobody anything.
    """
    anchor = f"\n  {name}:\n"
    assert anchor in _APPS, f"no {name} service in docker-compose.apps.yml"
    start = _APPS.index(anchor)
    rest = _APPS[start + len(anchor):]
    # The next service at the same indent, or the volumes section.
    ends = [i for i in (rest.find("\n  # "), rest.find("\nvolumes:"))
            if i != -1]
    end = min(ends) if ends else len(rest)
    return _APPS[max(0, start - lead): start + len(anchor) + end]


def test_package_delivery_ships_the_detector_it_grades_itself_on():
    # Service-level anchor (two-space indent, own line): a depends_on
    # entry or a URL mention must not satisfy this.
    assert "\n  package-detection-adapter:\n" in _APPS, (
        "the package detector has a catalog entry and no way to run — "
        "every install silently counts parcels with a VQA model and "
        "reports 'fair'")
    assert ("ghcr.io/open-nvr/package-detection-adapter:"
            "${ADAPTER_TAG:-latest}") in _APPS, (
        "the adapter must ride the same ADAPTER_TAG pin as every other "
        "adapter (RFC-0002 decision 7: one pinned set per release)")
    assert "OPENNVR_ADAPTER_TOKEN=${INTERNAL_API_KEY}" in _APPS


def test_it_registers_under_the_task_string_the_app_matches_on():
    """The register name is not cosmetic: it is the task the picker
    tests for, and the route KAI-C serves."""
    assert 'name="package_detection"' in _APPS
    assert "http://package-detection-adapter:9010" in _APPS
    assert "/api/v1/adapters/register" in _APPS
    # The app grades its counts "good" only on this exact string.
    assert '"package_detection" in tasks' in _APP, (
        "the app no longer picks on the task this overlay registers — "
        "update both sides together")
    assert 'tasks_advertised: [package_detection]' in _INDEX, (
        "the catalog entry no longer advertises the task the app matches")


def test_the_weights_variable_is_the_one_the_adapter_actually_reads():
    """The yolov8 block carries a comment earned the hard way: naming
    the variable after the image rather than after what the code reads
    gives a model that never loads and a /infer that 503s for ever.
    The adapter reads a DIRECTORY, not a file path."""
    assert "PACKAGE_DETECTION_WEIGHTS_DIR=/app/model_weights" in _APPS
    assert "PACKAGE_DETECTION_MODEL_PATH" not in _APPS, (
        "that variable does not exist in the adapter — it reads "
        "PACKAGE_DETECTION_WEIGHTS_DIR and looks for "
        "yolov8n-package.onnx inside it")
    assert "opennvr_package_detection_weights:/app/model_weights" in _APPS, (
        "the adapter reads its model from the volume the weights-init "
        "fills; without the mount it starts with nothing")


def test_the_weights_volume_is_declared():
    volumes = _APPS[_APPS.rindex("\nvolumes:\n"):]
    assert "opennvr_package_detection_weights:" in volumes


def test_the_app_does_NOT_hard_depend_on_the_detector():
    """Deliberately unlike LPR, and the distinction matters.

    LPR cannot read a plate without its OCR adapter, so it waits for it.
    Package Delivery is designed to degrade — a package detector is
    "good", a VQA model "fair", the COCO bag classes a stand-in — and
    the page says which it got. Waiting on `service_healthy` here would
    convert that graceful degradation into an app that never starts at
    all on a box where the weights have not been supplied, which is
    strictly worse than counting parcels with a VLM.
    """
    app = _APPS[_APPS.index("\n  package-delivery:\n"):]
    block = app[:app.index("networks:")]
    assert "package-detection-adapter:" not in block, (
        "the app must not wait on a detector it is designed to do "
        "without — see this test's docstring")


def test_registration_failure_degrades_it_does_not_wedge():
    register = _APPS[_APPS.index("\n  package-detection-register:\n"):]
    block = register[:register.index("networks:")]
    assert "exit 0" in block, (
        "a failed registration must be a WARN plus the app's own "
        "fallback, never a compose up that never finishes")
    assert "restart: \"no\"" in block


def test_the_operator_is_told_how_to_get_good_rather_than_left_guessing():
    """The whole reason this went unnoticed: "fair" is a correct answer
    that looks like a broken one. The compose block has to say where
    the weights come from."""
    block = _service_block("package-detection-adapter", lead=1400)
    assert "PACKAGE_DETECTION_MODEL_URL" in block
    assert "yolov8n-package.onnx" in block, (
        "the filename the adapter looks for must be written down — an "
        "operator putting their fine-tune in under another name gets a "
        "silent fallback to 'fair'")


def test_the_weights_ship_with_the_app_rather_than_being_fetched():
    """An app whose detector needs a hand-supplied model counts "fair"
    on every stock install, which reads as the app rating its own model
    poorly. yolov8-weights and yolo-pose-weights already solved this —
    and their headers record WHY a boot-time fetch is not good enough:
    operators on filtered networks (IN/CN/IR) cannot reach the URL."""
    assert "\n  package-detection-weights-init:\n" in _APPS, (
        "no weights-init — a stock install has no model and silently "
        "counts at the VQA grade")
    assert ("${PACKAGE_DETECTION_WEIGHTS_IMAGE:-"
            "ghcr.io/open-nvr/package-detection-weights:") in _APPS, (
        "the weights image must be pinned and overridable, so a site "
        "fine-tune is one variable rather than a compose edit")
    init = _service_block("package-detection-weights-init")
    assert "build:" in init, (
        "keep the local build fallback: an unpublished image, a private "
        "package or a blocked ghcr.io otherwise leaves no weights at all")
    assert 'entrypoint: ["sh", "-ec"]' in init, (
        "list-form entrypoint — compose word-splits a string command, "
        "which copies nothing and leaves an empty volume")


def test_the_adapter_waits_for_its_weights():
    """Unlike the app→adapter edge, THIS one is a hard dependency: an
    adapter that starts before the copy finishes loads nothing, and
    then reports itself unhealthy for ever."""
    block = _service_block("package-detection-adapter")
    assert "package-detection-weights-init:" in block
    assert "service_completed_successfully" in block


def test_the_weights_image_is_built_and_published():
    workflow = REPO_ROOT / ".github/workflows/build-package-detection-weights.yml"
    assert workflow.exists(), (
        "a compose file referencing an image nobody builds is a compose "
        "file that pulls a 404")
    text = workflow.read_text()
    assert "package-detection-weights" in text
    assert "linux/amd64,linux/arm64" in text, (
        "the ONNX is architecture-independent, so there is no reason to "
        "leave arm64 operators building it themselves")
    assert "Sanity-check the published image" in text, (
        "pull back what was pushed: a download that 404s bakes an HTML "
        "error page into the image and fails silently on the operator's "
        "machine weeks later")
