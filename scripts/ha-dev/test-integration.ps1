# Run the pyopennvr and Home Assistant integration test suites in a Linux
# Python 3.14 container (Home Assistant 2026.9 needs Python >= 3.14.2 and
# does not support Windows).
#
# Usage (from anywhere):
#   scripts/ha-dev/test-integration.ps1              # both suites
#   scripts/ha-dev/test-integration.ps1 -Suite lib   # pyopennvr only
#   scripts/ha-dev/test-integration.ps1 -Suite ha -PytestArgs '-k camera'
#   scripts/ha-dev/test-integration.ps1 -Rebuild     # rebuild the test image
param(
    [ValidateSet('all', 'lib', 'ha')]
    [string]$Suite = 'all',
    [string]$PytestArgs = '',
    [switch]$Rebuild
)

# Native tools (docker) write progress to stderr; with 'Stop' PowerShell 5.1
# turns that into a terminating error. Check $LASTEXITCODE instead.
$ErrorActionPreference = 'Continue'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$image = 'opennvr/ha-integration-test:2026.9.2'

$haveImage = docker image ls -q $image
if ($Rebuild -or -not $haveImage) {
    docker build -t $image -f (Join-Path $repo 'integrations/home-assistant/Dockerfile.test') (Join-Path $repo 'integrations/home-assistant')
    if ($LASTEXITCODE -ne 0) { throw 'test image build failed' }
}

$steps = @('pip install -q -e /src/integrations/home-assistant/pyopennvr')
if ($Suite -in @('all', 'lib')) {
    $steps += "cd /src/integrations/home-assistant/pyopennvr && python -m pytest -q $PytestArgs"
}
if ($Suite -in @('all', 'ha')) {
    $steps += "cd /src/integrations/home-assistant/hass-opennvr && python -m pytest -q -p no:cacheprovider $PytestArgs"
}

docker run --rm -v "${repo}:/src" $image sh -c ($steps -join ' && ')
exit $LASTEXITCODE
