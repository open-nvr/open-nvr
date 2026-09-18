# End-to-end check of the Home Assistant integration against the running
# OpenNVR stack (HA-207). See scripts/ha-dev/e2e_ha.py for what it checks.
#
# Starts a FRESH dev Home Assistant (its config dir is recreated: onboarding
# needs a new instance), mints an OpenNVR admin JWT for the setup steps, runs
# the checks, and stops Home Assistant again unless -Keep.
#
# Usage:
#   scripts/ha-dev/e2e-ha.ps1
#   scripts/ha-dev/e2e-ha.ps1 -Keep            # leave HA running afterwards
param(
    [switch]$Keep,
    [string]$ConfigDir = 'D:\myData\ha-dev-e2e',
    [int]$Port = 8124
)

$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot

# A fresh instance every run. Only a directory this script made (it leaves a
# marker) is ever deleted: a mistyped -ConfigDir must not wipe a real HA.
$marker = Join-Path $ConfigDir '.opennvr-e2e'
if (Test-Path $ConfigDir) {
    if (-not (Test-Path $marker)) { throw "$ConfigDir was not created by this script; not deleting it" }
    Remove-Item -Recurse -Force $ConfigDir
}
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType File -Force -Path $marker | Out-Null
& (Join-Path $here 'run-ha.ps1') -ConfigDir $ConfigDir -Port $Port
if ($LASTEXITCODE -ne 0) { throw 'failed to start Home Assistant' }

$env:HA_URL = "http://localhost:$Port"
$env:NVR_URL = 'https://localhost'
$env:NVR_URL_FROM_HA = 'https://host.docker.internal'
$env:NVR_JWT = (& (Join-Path $here 'mint-jwt.ps1') -Minutes 60 | Select-Object -Last 1).Trim()

try {
    uv run --quiet --no-project --with aiohttp python (Join-Path $here 'e2e_ha.py')
    $code = $LASTEXITCODE
} finally {
    if (-not $Keep) { & (Join-Path $here 'run-ha.ps1') -Stop -ConfigDir $ConfigDir | Out-Null }
}
exit $code
