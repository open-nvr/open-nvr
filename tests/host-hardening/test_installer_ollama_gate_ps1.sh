#!/usr/bin/env bash
# Regression tests for the WINDOWS installer's Ollama-availability gate —
# driving the real PowerShell logic, not grepping it.
#
# Same rules as the bash gate (test_installer_ollama_gate.sh):
#   * no exit without a working runtime: verified Ollama, or the container;
#   * a failed winget install cannot proceed broken;
#   * no winget -> no install offer, but the loop still guards;
#   * exhausted stdin -> the container, never an infinite loop. This one is
#     Windows-specific and sharp: on redirected-but-exhausted stdin,
#     Read-Host returns "" forever WITHOUT throwing, so the original
#     catch-only guard would have spun an unattended run to infinity.
set -u

. "$(dirname "$0")/_lib.sh"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT" || exit 1

# This suite needs PowerShell. Prefer a local pwsh; fall back to the
# Microsoft container image; skip (successfully, loudly) with neither —
# a suite that can't run must say so, not fail the build.
PWSH_RUN=""
if command -v pwsh >/dev/null 2>&1; then
    PWSH_RUN="pwsh"
elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    PWSH_RUN="docker run --rm -i -v ${REPO_ROOT}:/repo -w /repo mcr.microsoft.com/powershell:7.4-ubuntu-22.04 pwsh"
else
    echo "SKIP: neither pwsh nor docker is available to run the PowerShell gate tests."
    exit 0
fi

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

echo "Running installer ollama-gate tests (PowerShell)"
echo ""

HARNESS=$(mktemp -d)/gate_harness.ps1
mkdir -p "$(dirname "$HARNESS")"
cat > "$HARNESS" <<'PSEOF'
# Drives the REAL gate extracted from scripts/install.ps1 with everything
# stubbed. Args: extUrl haveOllama(yes/no) answersCsv appearsAfter(9999=never)
#               haveWinget(yes/no) wingetOk(yes/no)
param($extUrl, $haveOllama, $answersCsv, $appearsAfter, $haveWinget, $wingetOk)

$lines = Get-Content 'scripts/install.ps1'
$s = $lines.IndexOf(($lines | Where-Object { $_ -match '--- ollama-availability gate' } | Select-Object -First 1))
$e = $lines.IndexOf(($lines | Where-Object { $_ -match '--- end ollama-availability gate ---' } | Select-Object -First 1))
if ($s -lt 0 -or $e -lt 0) { Write-Output 'EXTRACT-FAILED'; exit 1 }
$gate = ($lines[$s..$e] -join "`n")

$script:Answers = @(); if ($answersCsv) { $script:Answers = $answersCsv -split ',' }
$script:AI = 0
$script:Asked = ''
$script:Warned = ''
$script:OllamaChecks = 0

function Get-EnvValue([string]$k) { if ($k -eq 'OLLAMA_EXTERNAL_URL') { $script:ExtUrl } }
function Set-EnvValue([string]$k, [string]$v) { if ($k -eq 'OLLAMA_EXTERNAL_URL') { $script:ExtUrl = $v } }
function Ask-YesNo([string]$q, $default) {
    $script:Asked += "|$q"
    $a = if ($script:AI -lt $script:Answers.Count) { $script:Answers[$script:AI] } else { 'n' }
    $script:AI++
    return ($a -eq 'y')
}
function Read-GateResponse([string]$Prompt) {
    $script:Asked += "|$Prompt"
    if ($script:AI -ge $script:Answers.Count) { return $null }     # stdin ran dry
    $a = $script:Answers[$script:AI]; $script:AI++
    if ($a -eq 'ENTER') { return '' }
    return $a
}
function Ok([string]$m) {}
function Warn([string]$m) { $script:Warned += "|$m" }
function Info([string]$m) {}
function Invoke-WebRequest { throw 'nothing answers anywhere in tests' }
function winget {
    $global:LASTEXITCODE = if ($script:WingetOk -eq 'yes') { 0 } else { 1 }
}
function Get-Command([string]$Name, $ErrorAction) {
    if ($Name -eq 'ollama') {
        $script:OllamaChecks++
        if ($script:HaveOllama -eq 'yes') { return @{Name='ollama'} }
        if ($script:OllamaChecks -gt $script:AppearsAfter) { return @{Name='ollama'} }
        return $null
    }
    if ($Name -eq 'winget') {
        if ($script:HaveWinget -eq 'yes') { return @{Name='winget'} } else { return $null }
    }
    return $null
}

$script:ExtUrl = $extUrl
$script:HaveOllama = $haveOllama
$script:AppearsAfter = [int]$appearsAfter
$script:HaveWinget = $haveWinget
$script:WingetOk = $wingetOk

$hostOllama = $false
$llmWhere = 'host'
$ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue

Invoke-Expression $gate

Write-Output ("where=$llmWhere url=$($script:ExtUrl) asked=$($script:Asked) warned=$($script:Warned)")
PSEOF

run_gate() {  # <ext_url> <have_ollama> <answers> <appears_after> <have_winget> <winget_ok>
    if [[ "$PWSH_RUN" == "pwsh" ]]; then
        pwsh -NoProfile -File "$HARNESS" "$1" "$2" "$3" "$4" "$5" "$6" 2>/dev/null
    else
        docker run --rm -v "${REPO_ROOT}:/repo" -v "$(dirname "$HARNESS"):/h" -w /repo \
            mcr.microsoft.com/powershell:7.4-ubuntu-22.04 \
            pwsh -NoProfile -File /h/gate_harness.ps1 "$1" "$2" "$3" "$4" "$5" "$6" 2>/dev/null
    fi
}

LOCAL_URL="http://host.docker.internal:11434"
LAN_URL="http://192.168.0.50:11434"

start_test "winget install accepted and succeeding keeps the host runtime"
out=$(run_gate "$LOCAL_URL" no "y" 9999 yes yes)
if [[ "$out" == *"where=host"* && "$out" == *"winget?"* \
      && "$out" != *"press Enter to re-check"* ]]; then
    pass
else
    fail "expected a clean winget install, got: $out"
fi

start_test "a FAILED winget install cannot proceed broken"
out=$(run_gate "$LOCAL_URL" no "y,y" 9999 yes no)
[[ "$out" == *"where=container"* ]] && pass \
    || fail "failed winget then container should switch, got: $out"

start_test "no winget: no install offer, the loop still guards"
out=$(run_gate "$LOCAL_URL" no "n,ENTER,container" 9999 no no)
if [[ "$out" == *"where=container"* && "$out" != *"winget?"* \
      && "$out" == *"press Enter to re-check"* ]]; then
    pass
else
    fail "wingetless box should skip the offer but keep the loop, got: $out"
fi

start_test "exhausted stdin takes the container — the silent-empty-string hang"
out=$(run_gate "$LOCAL_URL" no "n,n" 9999 no no)
if [[ "$out" == *"where=container"* && "$out" == *"No interactive input"* ]]; then
    pass
else
    fail "expected the container on EOF, got: $out"
fi

start_test "the loop notices an Ollama installed mid-loop"
out=$(run_gate "$LOCAL_URL" no "n,n,ENTER" 1 no no)
if [[ "$out" == *"where=host"* && "$out" == *"launch the Ollama app"* ]]; then
    pass
else
    fail "expected the re-check to find the new install, got: $out"
fi

start_test "remote endpoint: no install nag, reachability warning only"
out=$(run_gate "$LAN_URL" no "" 9999 yes yes)
if [[ "$out" == *"where=host"* && "$out" != *"asked=|"* \
      && "$out" == *"not answering right now"* ]]; then
    pass
else
    fail "remote endpoint should warn without questions, got: $out"
fi

echo ""
if (( TESTS_FAILED > 0 )); then
    echo "✗ ${TESTS_FAILED} of ${TESTS_RUN} tests failed"
    exit 1
fi
echo "✓ all ${TESTS_RUN} tests passed"
