# ============================================================================
# run-examples.ps1 — generic demo runner for everything under examples/
#
# Windows companion to scripts/run-examples.sh. Discovers examples by scanning
# examples/*/ (no hardcoded list) and can list / smoke-test / run them against
# the demo stack from docker-compose.tier0.yml.
#
# Usage:
#   .\scripts\run-examples.ps1 list
#   .\scripts\run-examples.ps1 smoke [name ...]
#   .\scripts\run-examples.ps1 run <name>
#
# Env knobs (set before calling, e.g.  $env:SMOKE_SECONDS=10):
#   SMOKE_SECONDS=8   survival time required for a smoke PASS
#   WITH_DOCKER=0     1 to also build Dockerfile-based examples
#   NO_STACK_CHECK=0  1 to skip the tier0 reachability check
# ============================================================================
param(
    [string]$Command = "smoke",
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Names
)
$ErrorActionPreference = "Stop"

$RepoRoot     = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$ExamplesDir  = Join-Path $RepoRoot "examples"
$SmokeSeconds = [int]($env:SMOKE_SECONDS  ?? 8)
$WithDocker   = ($env:WITH_DOCKER   -eq "1")
$NoStackCheck = ($env:NO_STACK_CHECK -eq "1")

$PyRun = (Get-Command uv -ErrorAction SilentlyContinue) ? "uv" : "python"

function Ok($m)   { Write-Host "✓ $m" -ForegroundColor Green }
function Err($m)  { Write-Host "✗ $m" -ForegroundColor Red }
function Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }

function Discover {
    Get-ChildItem -Directory $ExamplesDir | Where-Object {
        (Test-Path (Join-Path $_.FullName "pyproject.toml")) -or (Test-Path (Join-Path $_.FullName "Dockerfile"))
    } | Select-Object -ExpandProperty Name
}

# Resolve an example's main module file. Convention (uniform across every
# example): entrypoint is <dir-name-with-underscores>.py, run as
# `python <main>.py --config config.yml`. Returns the basename or $null.
function Main-Module($dir) {
    $cand = ((Split-Path $dir -Leaf) -replace '-', '_') + ".py"
    if (Test-Path (Join-Path $dir $cand)) { return $cand }
    return $null
}

# Start the python invocation for an example, returning the Process object.
function Py-Start($dir, $main, $stdout, $stderr) {
    if ($PyRun -eq "uv") { $argList = @("run", "python", $main, "--config", "config.yml") }
    else                 { $argList = @($main, "--config", "config.yml") }
    Start-Process -FilePath $PyRun -ArgumentList $argList -WorkingDirectory $dir `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -NoNewWindow
}

function Ensure-Config($dir) {
    $cfg = Join-Path $dir "config.yml"; $ex = Join-Path $dir "config.example.yml"
    if ((-not (Test-Path $cfg)) -and (Test-Path $ex)) {
        Copy-Item $ex $cfg; Warn "  created config.yml from config.example.yml (review for real creds)"
    }
}

function Stack-Up {
    if ($NoStackCheck) { return $true }
    try { Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:9997/v3/paths/list" -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}

function Cmd-List {
    Write-Host "Examples under ${ExamplesDir}:`n"
    foreach ($name in Discover) {
        $dir = Join-Path $ExamplesDir $name
        $entry = Main-Module $dir
        $docker = (Test-Path (Join-Path $dir "Dockerfile")) ? "docker" : "-"
        "{0,-28} {1,-10} entry={2}" -f $name, $docker, ($entry ?? "<none>")
    }
}

function Smoke-One($name) {
    $dir = Join-Path $ExamplesDir $name
    Ensure-Config $dir
    $main = Main-Module $dir
    if ($main) {
        $log = New-TemporaryFile
        $p = Py-Start $dir $main $log "$log.err"
        Start-Sleep -Seconds $SmokeSeconds
        if (-not $p.HasExited) {
            # Still running → healthy long-lived daemon.
            $p.Kill(); Ok "${name}: stayed up ${SmokeSeconds}s (connected to stack)"
            Remove-Item $log,"$log.err" -ErrorAction SilentlyContinue; return 0
        }
        if ($p.ExitCode -eq 0) {
            # Clean exit → one-shot example (e.g. --once). PASS.
            Ok "${name}: completed cleanly (exit 0)"
            Remove-Item $log,"$log.err" -ErrorAction SilentlyContinue; return 0
        }
        Err "${name}: exited with code $($p.ExitCode) — last lines:"; Get-Content "$log.err","$log" -Tail 6 -ErrorAction SilentlyContinue
        Remove-Item $log,"$log.err" -ErrorAction SilentlyContinue; return 1
    } elseif ((Test-Path (Join-Path $dir "Dockerfile")) -and $WithDocker) {
        $tag = "opennvr-example-$name"; Write-Host "  building $tag ..."
        docker build -q -t $tag $dir | Out-Null
        if ($LASTEXITCODE -ne 0) { Err "${name}: docker build failed"; return 1 }
        Ok "${name}: image built ($tag)"; return 0
    } elseif (Test-Path (Join-Path $dir "Dockerfile")) {
        Warn "${name}: Dockerfile-only (set WITH_DOCKER=1 to build) — skipped"; return 2
    } else {
        Warn "${name}: no <name>.py entry and no Dockerfile — skipped"; return 2
    }
}

function Cmd-Smoke($targets) {
    if (-not (Stack-Up)) {
        Err "Demo stack not reachable on 127.0.0.1:9997."
        Write-Host "  Start it first:  docker compose -f docker-compose.tier0.yml up -d"
        Write-Host "  (or set `$env:NO_STACK_CHECK=1 to smoke-test offline)"
        exit 1
    }
    if (-not $targets -or $targets.Count -eq 0) { $targets = Discover }
    $pass = 0; $fail = 0; $skip = 0
    foreach ($name in $targets) {
        Write-Host "`n── $name ─────────────────────────────"
        switch (Smoke-One $name) { 0 { $pass++ } 1 { $fail++ } default { $skip++ } }
    }
    Write-Host "`n════════════════════════════════════════"
    Write-Host ("  {0} passed  {1} failed  {2} skipped" -f $pass, $fail, $skip)
    if ($fail -ne 0) { exit 1 }
}

function Cmd-Run($name) {
    if (-not $name) { Err "usage: run-examples.ps1 run <name>"; exit 2 }
    $dir = Join-Path $ExamplesDir $name
    if (-not (Test-Path $dir)) { Err "no such example: $name"; exit 2 }
    Ensure-Config $dir
    $main = Main-Module $dir
    if (-not $main) { Err "${name}: no <name>.py entry module"; exit 2 }
    Write-Host "Running $name ($main --config config.yml) in foreground — Ctrl-C to stop."
    Push-Location $dir
    try {
        if ($PyRun -eq "uv") { & uv run python $main --config config.yml }
        else                 { & python $main --config config.yml }
    } finally { Pop-Location }
}

switch ($Command) {
    "list"  { Cmd-List }
    "smoke" { Cmd-Smoke $Names }
    "run"   { Cmd-Run ($Names | Select-Object -First 1) }
    default { Err "unknown command: $Command"; Write-Host "commands: list | smoke [name...] | run <name>"; exit 2 }
}
