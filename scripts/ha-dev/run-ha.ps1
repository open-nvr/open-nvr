# Start (or restart) a development Home Assistant with the OpenNVR integration mounted.
#
# It runs on Docker's default bridge network, NOT opennvr_internal. It reaches
# OpenNVR through the host's published nginx (https://host.docker.internal), as a
# real LAN install would. On opennvr_internal it would sit inside
# INTERNAL_SERVICE_CIDRS and silently bypass the device firewall.
#
# pyopennvr is pip-installed into the container before HA starts, so HA sees the
# requirement as satisfied and never tries to fetch the unpublished package from PyPI.
#
# Usage:
#   scripts/ha-dev/run-ha.ps1            # start / restart
#   scripts/ha-dev/run-ha.ps1 -Stop      # stop and remove the container (config is kept)
param(
    [switch]$Stop,
    [string]$ConfigDir = 'D:\myData\ha-dev-config',
    [int]$Port = 8123
)

# Native tools (docker) write progress to stderr; with 'Stop' PowerShell 5.1
# turns that into a terminating error. Check $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$name = 'opennvr_ha_dev'
$image = 'ghcr.io/home-assistant/home-assistant:2026.9.2'

if (docker ps -aq -f "name=^${name}$") { docker rm -f $name | Out-Null }
if ($Stop) { Write-Host "Stopped $name (config kept in $ConfigDir)"; exit 0 }

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null

$component = Join-Path $repo 'integrations/home-assistant/hass-opennvr/custom_components/opennvr'
$lib = Join-Path $repo 'integrations/home-assistant/pyopennvr'

docker run -d --name $name `
    -p "${Port}:8123" `
    --add-host host.docker.internal:host-gateway `
    -v "${ConfigDir}:/config" `
    -v "${component}:/config/custom_components/opennvr:ro" `
    -v "${lib}:/opt/pyopennvr:ro" `
    --entrypoint sh $image `
    -c 'pip install -q /opt/pyopennvr && exec /init' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'failed to start Home Assistant' }

Write-Host "Home Assistant starting on http://localhost:$Port (container $name, config $ConfigDir)"
Write-Host "OpenNVR URL to use inside HA: https://host.docker.internal (turn off SSL verification for the self-signed certificate)"
