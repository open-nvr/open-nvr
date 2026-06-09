# ============================================================
# OpenNVR - Smart Launcher (Windows PowerShell)
# ============================================================
# First run → launches the interactive installer automatically.
# Subsequent runs → validates and starts services.
#
# Usage:
#   .\start.ps1              # start (or install on first run)
#   .\start.ps1 build        # rebuild images and start
#   .\start.ps1 install      # re-run the interactive installer
#   .\start.ps1 down         # stop all services
#   .\start.ps1 logs         # tail logs
#   .\start.ps1 status       # show container status
#   .\start.ps1 validate     # run pre-flight checks only
# ============================================================

param(
    [string]$Command = "up"
)

$ComposeFile = "docker-compose.tier0.yml"
$OsLabel     = "Windows (tier0 demo / bridge network mode)"

function Write-Color($Text, $Color = "White") {
    Write-Host $Text -ForegroundColor $Color
}

# ── Read a value from .env ─────────────────────────────────
function Get-EnvVar {
    param([string]$Key)
    if (-not (Test-Path ".env")) { return $null }
    $line = Get-Content ".env" | Where-Object { $_ -match "^${Key}=" } | Select-Object -First 1
    if ($line) { return ($line -split '=', 2)[1].Trim('"').Trim("'") }
    return $null
}

# ── Build Compose profile args ─────────────────────────────
function Get-ComposeArgs {
    $args = @("-f", $ComposeFile)
    $ai = Get-EnvVar "AI_ENABLED"
    if ($ai -eq "true") { $args += @("--profile", "ai") }
    return $args
}

# ── Port conflict check ────────────────────────────────────
function Test-PortInUse {
    param([int]$Port)
    $listeners = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners()
    return ($listeners | Where-Object { $_.Port -eq $Port }).Count -gt 0
}

# ── Pre-flight validation ──────────────────────────────────
function Invoke-Validate {
    $errors = 0; $warnings = 0

    Write-Color "  Running pre-flight checks..." Cyan
    Write-Color ""

    # 1. Docker
    $null = docker info 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Color "  ✗ Docker is not running" Red
        Write-Color "      → Start Docker Desktop and retry."
        $errors++
    } else {
        Write-Color "  ✓ Docker is running" Green
    }

    # 2. Compose file
    if (-not (Test-Path $ComposeFile)) {
        Write-Color "  ✗ Compose file not found: $ComposeFile" Red
        $errors++
    } else {
        Write-Color "  ✓ Compose file: $ComposeFile" Green
    }

    # 3. .env
    if (-not (Test-Path ".env")) {
        Write-Color "  ✗ No .env file — run installer first: .\start.ps1 install" Red
        $errors++
    } else {
        Write-Color "  ✓ .env file found" Green

        # 4. Default secrets
        $insecureKeys = @()
        foreach ($key in @('SECRET_KEY','CREDENTIAL_ENCRYPTION_KEY','INTERNAL_API_KEY','MEDIAMTX_SECRET','POSTGRES_PASSWORD')) {
            $val = Get-EnvVar $key
            if ($val -match '^(dev_|insecure_|change_me|your_|changeme|placeholder|dummy)') {
                $insecureKeys += $key
            }
        }
        if ($insecureKeys.Count -gt 0) {
            Write-Color "  ⚠ Default dev secrets detected (not safe for production):" Yellow
            foreach ($k in $insecureKeys) { Write-Color "      - $k" Gray }
            Write-Color "      → Run: .\scripts\generate-secrets.ps1 -Write" Cyan
            $warnings++
        } else {
            Write-Color "  ✓ Secrets look non-default" Green
        }

        # 5. (password managed via first-time setup page — no check needed)

        # 6. Recordings path
        $recPath = Get-EnvVar "RECORDINGS_PATH"
        if ($recPath -and $recPath -ne "./recordings" -and $recPath -ne ".\recordings" -and (-not (Test-Path $recPath))) {
            Write-Color "  ⚠ RECORDINGS_PATH does not exist: $recPath" Yellow
            Write-Color "      → Docker will attempt to create it."
            $warnings++
        } elseif ($recPath) {
            Write-Color "  ✓ RECORDINGS_PATH: $recPath" Green
        }
    }

    # 7. Port conflicts
    $busyPorts = @(8000, 8554, 8888, 8889, 9997) | Where-Object { Test-PortInUse $_ }
    if ($busyPorts) {
        Write-Color "  ⚠ Ports already in use: $($busyPorts -join ', ')" Yellow
        Write-Color "      → Check: netstat -ano | findstr LISTENING"
        $warnings++
    } else {
        Write-Color "  ✓ Required ports appear free" Green
    }

    Write-Color ""
    if ($errors -gt 0) {
        Write-Color "  ✗ $errors error(s) — cannot start." Red
        return $false
    } elseif ($warnings -gt 0) {
        Write-Color "  ⚠ $warnings warning(s) — review above before production." Yellow
    } else {
        Write-Color "  ✓ All checks passed." Green
    }
    Write-Color ""
    return $true
}

# ── Banner ─────────────────────────────────────────────────
function Show-FirstTimeSetupToken {
    param([array]$ComposeArgs)
    # V-001 / M0 C-1 UX: surface the setup-token banner so the operator
    # can copy it from the wizard's terminal instead of grepping logs.
    #
    # ISSUE-5 fix: the previous version polled docker logs for 30s
    # after `compose up -d`. But `up -d` returns when containers are
    # *scheduled*, not when they're *healthy*. Post-ISSUE-3 the
    # yolov8-weights-init container takes ~3 min on x86 / ~10-15 min
    # on a Pi 5 to export the ONNX model before opennvr-core even
    # starts. A 30-second poll always lost that race on slow hardware
    # and fell through to a misleading "either the admin is already
    # activated or the server is still starting" message.
    #
    # New strategy: wait for opennvr-core's Docker healthcheck to pass
    # first (with progress feedback so the operator isn't staring at a
    # silent terminal for 15 min), THEN extract the banner from the
    # logs. Once healthy, the banner is unambiguously present — its
    # absence then means the admin is already activated, which we
    # report as such.
    $container = "opennvr_core"          # container_name from compose
    # $env:OPENNVR_SETUP_TOKEN_MAX_WAIT_S exists so a future smoke-test
    # harness can short-circuit the 20-minute production timeout with
    # something testable, e.g. $env:OPENNVR_SETUP_TOKEN_MAX_WAIT_S=10.
    $maxWaitSeconds = 1200               # 20 min — covers Pi 5 + YOLO export
    if ($env:OPENNVR_SETUP_TOKEN_MAX_WAIT_S) {
        $maxWaitSeconds = [int]$env:OPENNVR_SETUP_TOKEN_MAX_WAIT_S
    }
    $pollIntervalSeconds = 2
    $elapsed = 0
    $lastHealth = ""
    $lastMessageAt = 0
    $banner = ""

    Write-Color ""
    Write-Color "  Waiting for opennvr-core to be healthy before showing the" DarkGray
    Write-Color "  first-time setup token. Init containers can take 10-15 min" DarkGray
    Write-Color "  on a Pi 5 the first time (YOLOv8 .pt -> ONNX export)." DarkGray

    while ($elapsed -lt $maxWaitSeconds) {
        # docker inspect returns empty if the container hasn't been
        # created yet (yolov8-weights-init still running). Treat that
        # as "waiting".
        $health = ""
        try {
            $health = (& docker inspect `
                --format='{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' `
                $container 2>$null).Trim()
        } catch {
            $health = "absent"
        }
        if (-not $health) { $health = "absent" }

        switch ($health) {
            "healthy" {
                Write-Color "  [OK] opennvr-core is healthy" Green
                break
            }
            "unhealthy" {
                Write-Color ""
                Write-Color "  opennvr-core reported unhealthy. Inspect:" Yellow
                Write-Color ("      docker compose " + ($ComposeArgs -join ' ') + " logs --tail 100 opennvr-core") DarkGray
                Write-Color ""
                return
            }
            "none" {
                # Container running, no healthcheck defined (custom
                # image stripped it). No signal to wait on — fall
                # through to banner extraction immediately. The token
                # banner is printed early in lifespan, so if the
                # container exists it's almost certainly in the logs.
                Write-Color "  opennvr-core has no healthcheck; checking logs now" DarkGray
                break
            }
            default {
                # absent / starting — periodic progress message
                if (($health -ne $lastHealth) -or `
                    (($elapsed - $lastMessageAt) -ge 15)) {
                    if ($health -eq "absent") {
                        Write-Color ("  [${elapsed}s] opennvr-core not yet created (init containers running)...") DarkGray
                    } else {
                        Write-Color ("  [${elapsed}s] opennvr-core booting...") DarkGray
                    }
                    $lastMessageAt = $elapsed
                }
            }
        }
        if ($health -eq "healthy" -or $health -eq "none") { break }
        $lastHealth = $health
        Start-Sleep -Seconds $pollIntervalSeconds
        $elapsed += $pollIntervalSeconds
    }

    if ($elapsed -ge $maxWaitSeconds) {
        Write-Color ""
        Write-Color ("  Timed out after " + $maxWaitSeconds + "s waiting for opennvr-core") Yellow
        Write-Color "  to become healthy. Check init container progress:" Yellow
        Write-Color ("      docker compose " + ($ComposeArgs -join ' ') + " ps") DarkGray
        Write-Color ("      docker compose " + ($ComposeArgs -join ' ') + " logs --tail 100 opennvr-core") DarkGray
        Write-Color "  Once healthy, retrieve the token manually:" DarkGray
        Write-Color ("      docker compose " + ($ComposeArgs -join ' ') + " logs opennvr-core | Select-String 'first-time setup token' -Context 0,6") DarkGray
        Write-Color ""
        return
    }

    # Healthy — the lifespan hook prints the banner very early in
    # boot, so it's definitely in the logs by now. --tail 5000 scoops
    # the early-boot region without a brittle --since time window.
    #
    # Iterating in reverse and taking the FIRST hit gives us the most
    # recent banner. If opennvr-core crash-looped during boot,
    # ``maybe_arm`` runs once per restart and prints a fresh banner
    # with a new token each time; earlier banners are stale (their
    # in-memory tokens died with the container) and would mislead the
    # operator into copy-pasting an invalidated value.
    try {
        $raw = & docker compose @ComposeArgs logs `
            --no-color --no-log-prefix --tail 5000 opennvr-core 2>$null
        if ($raw) {
            $lines = $raw -split "`n"
            for ($i = $lines.Length - 1; $i -ge 0; $i--) {
                if ($lines[$i] -match "first-time setup token") {
                    $end = [Math]::Min($i + 6, $lines.Length - 1)
                    $banner = ($lines[$i..$end] -join "`n")
                    break
                }
            }
        }
    } catch {
        # ignore — fall through to the "already activated" path
    }

    Write-Color ""
    if ($banner) {
        Write-Color "  🔑 First-time setup token (one-time use — copy into the UI):" Yellow
        Write-Color ""
        foreach ($line in ($banner -split "`n")) { Write-Color ("  " + $line) White }
        Write-Color ""
    } else {
        # Container healthy AND no banner = admin already activated on
        # a previous boot. Unambiguous now.
        $adminUser = "admin"
        try { $adminUser = (Get-EnvVar "DEFAULT_ADMIN_USERNAME") } catch { }
        Write-Color "  First-time setup is already complete." Green
        Write-Color ("  Log in at http://localhost:8000 as " + $adminUser + ".") DarkGray
        Write-Color "  (To re-arm the setup token, wipe the database volume and restart.)" DarkGray
        Write-Color ""
    }
}

function Show-Banner {
    Write-Color ""
    Write-Color "  ╔══════════════════════════════════════════════╗" Cyan
    Write-Color "  ║           OpenNVR - Smart Launcher           ║" Cyan
    Write-Color "  ╚══════════════════════════════════════════════╝" Cyan
    Write-Color ""
    Write-Color "  OS detected   : $OsLabel"     Green
    Write-Color "  Compose file  : $ComposeFile" Green
    Write-Color "  Command       : $Command"      Green
    Write-Color ""
}

# ── Run command ────────────────────────────────────────────
switch ($Command) {

    "install" {
        $installScript = Join-Path $PSScriptRoot "scripts\install.ps1"
        & $installScript
    }

    "up" {
        if (-not (Test-Path ".env")) {
            Write-Color "  No .env found — launching installer..." Yellow
            Write-Color ""
            $installScript = Join-Path $PSScriptRoot "scripts\install.ps1"
            & $installScript
            exit $LASTEXITCODE
        }
        Show-Banner
        $ok = Invoke-Validate
        if (-not $ok) { exit 1 }
        $ca = Get-ComposeArgs
        Write-Color "  Starting all services ..." Green
        docker compose @ca up -d
        Write-Color ""
        $u = Get-EnvVar "DEFAULT_ADMIN_USERNAME"
        Write-Color "  ✓ OpenNVR is running!" Green
        Write-Color "  Web UI   → http://localhost:8000  (login: $u)" Cyan
        Write-Color "  API Docs → http://localhost:8000/docs" Cyan
        Write-Color "  First-time setup page opens automatically on first visit." DarkGray
        Show-FirstTimeSetupToken -ComposeArgs $ca
    }

    "build" {
        if (-not (Test-Path ".env")) {
            Write-Color "  No .env found — launching installer..." Yellow
            Write-Color ""
            $installScript = Join-Path $PSScriptRoot "scripts\install.ps1"
            & $installScript
            exit $LASTEXITCODE
        }
        Show-Banner
        $ok = Invoke-Validate
        if (-not $ok) { exit 1 }
        $ca = Get-ComposeArgs
        Write-Color "  Building images and starting all services ..." Green
        docker compose @ca build
        docker compose @ca up -d
        Write-Color ""
        $u = Get-EnvVar "DEFAULT_ADMIN_USERNAME"
        Write-Color "  ✓ OpenNVR is running!" Green
        Write-Color "  Web UI   → http://localhost:8000  (login: $u)" Cyan
        Write-Color "  API Docs → http://localhost:8000/docs" Cyan
        Write-Color "  First-time setup page opens automatically on first visit." DarkGray
        Show-FirstTimeSetupToken -ComposeArgs $ca
    }

    "down" {
        Show-Banner
        $ca = if (Test-Path ".env") { Get-ComposeArgs } else { @("-f", $ComposeFile) }
        Write-Color "  Stopping all services ..." Yellow
        docker compose @ca down
        Write-Color "  ✓ All services stopped." Green
    }

    "logs" {
        Show-Banner
        $ca = if (Test-Path ".env") { Get-ComposeArgs } else { @("-f", $ComposeFile) }
        Write-Color "  Tailing logs (Ctrl+C to exit) ..." Green
        docker compose @ca logs -f
    }

    "status" {
        $ca = if (Test-Path ".env") { Get-ComposeArgs } else { @("-f", $ComposeFile) }
        docker compose @ca ps
    }

    "validate" {
        Show-Banner
        Invoke-Validate | Out-Null
    }

    default {
        Write-Color "Unknown command: $Command" Red
        Write-Color "Usage: .\start.ps1 [up|build|down|logs|status|validate|install]"
        exit 1
    }
}
