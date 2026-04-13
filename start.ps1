# ============================================================
# OpenNVR - Smart Start Script (Windows PowerShell)
# ============================================================
# Automatically detects your environment and picks the correct
# Docker Compose file:
#   - Windows → docker-compose.yml (bridge network mode)
#
# Usage:
#   .\start.ps1              # start
#   .\start.ps1 build        # rebuild images and start
#   .\start.ps1 down         # stop all services
#   .\start.ps1 logs         # tail logs
#   .\start.ps1 status       # show container status
# ============================================================

param(
    [string]$Command = "up"
)

# ── Colours ────────────────────────────────────────────────
function Write-Color($Text, $Color = "White") {
    Write-Host $Text -ForegroundColor $Color
}

# ── Always bridge mode on Windows ──────────────────────────
$ComposeFile = "docker-compose.yml"
$OsLabel     = "Windows (bridge network mode)"

Write-Color ""
Write-Color "╔══════════════════════════════════════════════╗" Cyan
Write-Color "║           OpenNVR - Smart Launcher           ║" Cyan
Write-Color "╚══════════════════════════════════════════════╝" Cyan
Write-Color ""
Write-Color "  OS detected   : $OsLabel"        Green
Write-Color "  Compose file  : $ComposeFile"    Green
Write-Color "  Command       : $Command"         Green
Write-Color ""

# ── Pre-flight checks ──────────────────────────────────────
# 1. Docker running?
$dockerInfo = docker info 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Color "Docker is not running. Please start Docker Desktop and try again." Red
    exit 1
}

# 2. Compose file exists?
if (-not (Test-Path $ComposeFile)) {
    Write-Color "Compose file not found: $ComposeFile" Red
    exit 1
}

# 3. .env file exists?
if (-not (Test-Path ".env")) {
    Write-Color "No .env file found. Copying from .env.docker ..." Yellow
    Copy-Item ".env.docker" ".env"
    Write-Color ".env created. Edit it to customise settings." Green
}

# ── Run command ────────────────────────────────────────────
switch ($Command) {
    "up" {
        Write-Color "Starting all services ..." Green
        docker compose -f $ComposeFile up -d
        Write-Color ""
        Write-Color "OpenNVR is running!" Green
        Write-Color "  Web UI   → http://localhost:8000  (admin / SecurePass123!)" Cyan
        Write-Color "  API Docs → http://localhost:8000/docs" Cyan
    }

    "build" {
        Write-Color "Building images and starting all services ..." Green
        docker compose -f $ComposeFile build
        docker compose -f $ComposeFile up -d
        Write-Color ""
        Write-Color "OpenNVR is running!" Green
        Write-Color "  Web UI   → http://localhost:8000  (admin / SecurePass123!)" Cyan
        Write-Color "  API Docs → http://localhost:8000/docs" Cyan
    }

    "down" {
        Write-Color "Stopping all services ..." Yellow
        docker compose -f $ComposeFile down
        Write-Color "All services stopped." Green
    }

    "logs" {
        Write-Color "Tailing logs (Ctrl+C to exit) ..." Green
        docker compose -f $ComposeFile logs -f
    }

    "status" {
        docker compose -f $ComposeFile ps
    }

    default {
        Write-Color "Unknown command: $Command" Red
        Write-Color "Usage: .\start.ps1 [up|build|down|logs|status]"
        exit 1
    }
}

