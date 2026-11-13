# Recreate ONLY the opennvr-core container on a given image tag, safely.
#
#  - Uses the compose file set core was created with (docker-compose.yml).
#  - --no-deps: CORE_TAG also selects the detect-pipeline image; we must not touch it.
#  - Never --remove-orphans (it deletes the fakecams / apps-profile containers).
#  - Carries over OPENNVR_HOST_IP / OPENNVR_LAN_IPS from the running container:
#    a plain `compose up` leaves them empty, which silently breaks ONVIF discovery.
#  - Never edits .env: CORE_TAG is set for this process only.
#
# Usage:
#   scripts/ha-dev/swap-core.ps1 -Tag ha-dev     # run the locally built dev image
#   scripts/ha-dev/swap-core.ps1 -Tag main       # restore the normal image
param([Parameter(Mandatory)][string]$Tag)

$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path

$envLines = docker inspect opennvr_core --format '{{range .Config.Env}}{{println .}}{{end}}' 2>$null
foreach ($name in 'OPENNVR_HOST_IP', 'OPENNVR_LAN_IPS') {
    $line = $envLines | Where-Object { $_ -like "$name=*" } | Select-Object -First 1
    if ($line) { Set-Item -Path "env:$name" -Value ($line.Substring($name.Length + 1)) }
}
if (-not $env:OPENNVR_HOST_IP) { Write-Warning 'OPENNVR_HOST_IP not found on the running core; ONVIF discovery may be degraded' }

$env:CORE_TAG = $Tag
Push-Location $repo
try {
    docker compose -f docker-compose.yml up -d --no-deps opennvr-core
    if ($LASTEXITCODE -ne 0) { throw "compose up failed for core:$Tag" }
} finally { Pop-Location }

# Wait for health (up to ~3 min: startup runs migrations).
for ($i = 0; $i -lt 36; $i++) {
    $state = docker inspect opennvr_core --format '{{.State.Health.Status}}' 2>$null
    if ($state -eq 'healthy') { break }
    Start-Sleep -Seconds 5
}
$image = docker inspect opennvr_core --format '{{.Config.Image}}'
Write-Host "opennvr_core: $image -> $state"
if ($state -ne 'healthy') { exit 1 }
