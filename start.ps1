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

$ComposeFile = "docker-compose.yml"
$OsLabel     = "Windows (bridge network mode)"

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
