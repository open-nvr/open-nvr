# HA-601 spike: Home Assistant's onvif integration against the ONVIF spike
# server (scripts/spikes/onvif_server.py). Starts a fresh, plain Home
# Assistant (container opennvr_ha_onvif, port 8126, config on D:) and removes
# it afterwards unless -Keep. See scripts/spikes/onvif_ha_check.py.
param(
    [switch]$Keep,
    [string]$ConfigDir = 'D:\myData\ha-dev-onvif',
    [int]$Port = 8126
)

$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$marker = Join-Path $ConfigDir '.opennvr-e2e'
if (Test-Path $ConfigDir) {
    if (-not (Test-Path $marker)) { throw "$ConfigDir was not created by this script; not deleting it" }
    Remove-Item -Recurse -Force $ConfigDir
}
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType File -Force -Path $marker | Out-Null
docker rm -f opennvr_ha_onvif 2>$null | Out-Null
docker run -d --name opennvr_ha_onvif -p "${Port}:8123" --add-host host.docker.internal:host-gateway `
    -v "${ConfigDir}:/config" ghcr.io/home-assistant/home-assistant:2026.9.2 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to start Home Assistant' }

$env:HA_URL = "http://localhost:$Port"
$env:NVR_URL = 'https://localhost'
$env:NVR_JWT = (& (Join-Path $here '..\ha-dev\mint-jwt.ps1') -Minutes 60 | Select-Object -Last 1).Trim()
$env:ONVIF_HOST_FROM_HA = 'host.docker.internal'

try {
    uv run --quiet --no-project --with aiohttp python (Join-Path $here 'onvif_ha_check.py')
    $code = $LASTEXITCODE
} finally {
    if (-not $Keep) { docker rm -f opennvr_ha_onvif | Out-Null }
}
exit $code
