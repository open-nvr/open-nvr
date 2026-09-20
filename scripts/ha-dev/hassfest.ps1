# Validate the integration with Home Assistant's hassfest, as HA's CI action does.
# Native tools (docker) write progress to stderr; with 'Stop' PowerShell 5.1
# turns that into a terminating error. Check $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$integration = Join-Path $repo 'integrations/home-assistant/hass-opennvr'
docker run --rm -v "${integration}:/github/workspace" ghcr.io/home-assistant/hassfest
exit $LASTEXITCODE
