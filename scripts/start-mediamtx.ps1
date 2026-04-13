#!/usr/bin/env pwsh
# Start MediaMTX with environment variables from server/.env
# This script automatically loads MEDIAMTX_SECRET from server/.env
# and sets the necessary environment variables for local development.

param(
    [switch]$Help
)

if ($Help) {
    Write-Host @"
Start MediaMTX with environment variables

USAGE:
    .\scripts\start-mediamtx.ps1

DESCRIPTION:
    This script loads MEDIAMTX_SECRET from server/.env and starts MediaMTX
    with the correct environment variables for local development.

ENVIRONMENT VARIABLES SET:
    - MEDIAMTX_SECRET: Loaded from server/.env
    - BACKEND_HOST: 127.0.0.1 (localhost)
    - BACKEND_PORT: 8000

REQUIREMENTS:
    - server/.env must exist with MEDIAMTX_SECRET defined
    - MediaMTX binary must be in mediamtx/ directory

"@
    exit 0
}

# Change to project root
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host "  OpenNVR - MediaMTX Startup Script" -ForegroundColor Cyan
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

# Check if server/.env exists
$EnvFile = Join-Path $ProjectRoot "server\.env"
if (-not (Test-Path $EnvFile)) {
    Write-Host "❌ ERROR: server/.env file not found!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please create server/.env from server/env.example and configure MEDIAMTX_SECRET" -ForegroundColor Yellow
    Write-Host "You can generate a secret with: openssl rand -hex 32" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Load MEDIAMTX_SECRET from server/.env
Write-Host "📄 Loading configuration from server/.env..." -ForegroundColor Green
$MediaMtxSecret = $null

Get-Content $EnvFile | ForEach-Object {
    $line = $_.Trim()
    
    # Skip comments and empty lines
    if ($line -match '^\s*#' -or $line -eq '') {
        return
    }
    
    # Parse MEDIAMTX_SECRET
    if ($line -match '^MEDIAMTX_SECRET\s*=\s*(.+)$') {
        $MediaMtxSecret = $matches[1].Trim()
        # Remove surrounding quotes if present
        $MediaMtxSecret = $MediaMtxSecret -replace '^[''"]|[''"]$', ''
    }
}

# Validate MEDIAMTX_SECRET was found
if ([string]::IsNullOrWhiteSpace($MediaMtxSecret)) {
    Write-Host "❌ ERROR: MEDIAMTX_SECRET not found in server/.env!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please add MEDIAMTX_SECRET to server/.env" -ForegroundColor Yellow
    Write-Host "Generate with: openssl rand -hex 32" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Set environment variables for MediaMTX
Write-Host "🔐 Setting environment variables..." -ForegroundColor Green
$env:MEDIAMTX_SECRET = $MediaMtxSecret
$env:BACKEND_HOST = "127.0.0.1"
$env:BACKEND_PORT = "8000"

Write-Host "   ✓ MEDIAMTX_SECRET: ****" -ForegroundColor Gray
Write-Host "   ✓ BACKEND_HOST: $env:BACKEND_HOST" -ForegroundColor Gray
Write-Host "   ✓ BACKEND_PORT: $env:BACKEND_PORT" -ForegroundColor Gray
Write-Host ""

# Check if MediaMTX binary exists
$MediaMtxDir = Join-Path $ProjectRoot "mediamtx"
$MediaMtxExe = Join-Path $MediaMtxDir "mediamtx.exe"

if (-not (Test-Path $MediaMtxExe)) {
    # Try without .exe extension (for WSL/Linux binary)
    $MediaMtxExe = Join-Path $MediaMtxDir "mediamtx"
    if (-not (Test-Path $MediaMtxExe)) {
        Write-Host "❌ ERROR: MediaMTX binary not found!" -ForegroundColor Red
        Write-Host ""
        Write-Host "Expected location: mediamtx/mediamtx.exe" -ForegroundColor Yellow
        Write-Host "Please download from: https://github.com/bluenviron/mediamtx/releases" -ForegroundColor Yellow
        Write-Host ""
        exit 1
    }
}

# Check if mediamtx.yml exists
$MediaMtxConfig = Join-Path $MediaMtxDir "mediamtx.yml"
if (-not (Test-Path $MediaMtxConfig)) {
    Write-Host "❌ ERROR: mediamtx.yml not found in mediamtx/ directory!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please ensure mediamtx.yml is copied to mediamtx/ directory" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Display startup info
Write-Host "🚀 Starting MediaMTX..." -ForegroundColor Green
Write-Host "   Binary: $MediaMtxExe" -ForegroundColor Gray
Write-Host "   Config: $MediaMtxConfig" -ForegroundColor Gray
Write-Host ""
Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor Cyan
Write-Host ""

# Start MediaMTX
Set-Location $MediaMtxDir
& $MediaMtxExe $MediaMtxConfig
