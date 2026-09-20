"""The version is carried in three places (pyproject, the package, the HA
manifest's pin); they must agree, or HACS installs a pyopennvr the
integration was not written against."""

import json
from pathlib import Path
import tomllib

import pyopennvr

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT.parent / "hass-opennvr" / "custom_components" / "opennvr" / "manifest.json"


def test_version_is_set():
    assert pyopennvr.__version__ == "0.1.0"


def test_pyproject_carries_the_package_version():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == pyopennvr.__version__


def test_the_integration_pins_this_version():
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    pins = [r for r in manifest["requirements"] if r.startswith("pyopennvr")]
    assert pins == [f"pyopennvr=={pyopennvr.__version__}"]
