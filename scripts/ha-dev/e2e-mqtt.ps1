# End-to-end check of Home Assistant MQTT discovery (HA-402/403): a fresh
# Home Assistant with only its MQTT integration discovers OpenNVR through a
# Mosquitto broker. See scripts/ha-dev/e2e_mqtt.py for what it checks.
#
# Starts Mosquitto (container opennvr_mosquitto, anonymous, on OpenNVR's
# internal network and the host's port 1883) and a fresh dev Home Assistant,
# and removes both afterwards unless -Keep.
param(
    [switch]$Keep,
    [string]$ConfigDir = 'D:\myData\ha-dev-e2e-mqtt',
    [int]$Port = 8125
)

$ErrorActionPreference = 'Continue'
$here = $PSScriptRoot
$broker = 'opennvr_mosquitto'
$network = (docker inspect opennvr_core --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}').Split(' ') |
    Where-Object { $_ -like '*opennvr_internal' } | Select-Object -First 1

if (docker ps -aq -f "name=^${broker}$") { docker rm -f $broker | Out-Null }
docker run -d --name $broker --network $network -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to start Mosquitto' }

$marker = Join-Path $ConfigDir '.opennvr-e2e'
if (Test-Path $ConfigDir) {
    if (-not (Test-Path $marker)) { throw "$ConfigDir was not created by this script; not deleting it" }
    Remove-Item -Recurse -Force $ConfigDir
}
New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType File -Force -Path $marker | Out-Null
# A plain Home Assistant: no OpenNVR integration mounted.
docker rm -f opennvr_ha_mqtt 2>$null | Out-Null
docker run -d --name opennvr_ha_mqtt -p "${Port}:8123" --add-host host.docker.internal:host-gateway `
    -v "${ConfigDir}:/config" ghcr.io/home-assistant/home-assistant:2026.9.2 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to start Home Assistant' }

$env:HA_URL = "http://localhost:$Port"
$env:NVR_URL = 'https://localhost'
$env:NVR_JWT = (& (Join-Path $here 'mint-jwt.ps1') -Minutes 60 | Select-Object -Last 1).Trim()
$env:BROKER_FROM_NVR = "mqtt://${broker}:1883"
$env:BROKER_HOST_FROM_HA = 'host.docker.internal'

try {
    uv run --quiet --no-project --with aiohttp python (Join-Path $here 'e2e_mqtt.py')
    $code = $LASTEXITCODE
} finally {
    if (-not $Keep) {
        docker rm -f opennvr_ha_mqtt | Out-Null
        docker rm -f $broker | Out-Null
    }
}
exit $code
