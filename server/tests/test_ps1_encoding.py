# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Every PowerShell script with a non-ASCII character starts with a UTF-8 BOM.

Windows PowerShell 5.1 (the one every Windows ships) reads a .ps1 without a
BOM as ANSI. A UTF-8 "✓" or "—" then decodes to bytes that include a
typographic quote, which PowerShell treats as a real one, and the script no
longer parses: `.\\scripts\\generate-secrets.ps1 -Write` failed this way.
Pure-ASCII scripts need no BOM.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def unparseable_on_51(files: list[Path]) -> list[Path]:
    """The scripts Windows PowerShell 5.1 would misread: non-ASCII, no BOM."""
    out = []
    for f in files:
        data = f.read_bytes()
        if any(b > 127 for b in data) and not data.startswith(b"\xef\xbb\xbf"):
            out.append(f)
    return out


def test_non_ascii_powershell_scripts_carry_a_bom():
    try:
        listed = subprocess.run(["git", "ls-files", "*.ps1"], cwd=REPO, capture_output=True,
                                text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    missing = unparseable_on_51([REPO / line for line in listed.splitlines() if line.strip()])
    assert not missing, (
        f"Windows PowerShell 5.1 cannot parse these (UTF-8 without a BOM): "
        f"{[str(f.relative_to(REPO)) for f in missing]}. Save them as 'UTF-8 with BOM', or keep them ASCII.")


def test_the_check_tells_the_cases_apart(tmp_path):
    text = 'Write-Host "✓ done"\n'.encode()
    no_bom, with_bom, ascii_only = tmp_path / "a.ps1", tmp_path / "b.ps1", tmp_path / "c.ps1"
    no_bom.write_bytes(text)
    with_bom.write_bytes(b"\xef\xbb\xbf" + text)
    ascii_only.write_bytes(b'Write-Host "done"\n')
    assert unparseable_on_51([no_bom, with_bom, ascii_only]) == [no_bom]
