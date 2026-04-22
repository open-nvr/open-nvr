# ============================================================
# OpenNVR - Secrets Generator (Windows PowerShell)
# ============================================================
# Generates cryptographically secure secrets and writes them
# directly into the .env file (in the project root).
#
# Usage:
#   .\scripts\generate-secrets.ps1         # dry-run: print only
#   .\scripts\generate-secrets.ps1 -Write  # generate and write to .env
# ============================================================

param(
    [switch]$Write
)

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
$EnvFile     = Join-Path $ProjectRoot ".env"
$EnvExample  = Join-Path $ProjectRoot ".env.example"

function Write-Color($Text, $Color = "White") {
    Write-Host $Text -ForegroundColor $Color
}

Write-Color ""
Write-Color "╔══════════════════════════════════════════════╗" Cyan
Write-Color "║       OpenNVR - Secrets Generator            ║" Cyan
Write-Color "╚══════════════════════════════════════════════╝" Cyan
Write-Color ""

# ── Helper: generate random string ────────────────────────
function New-RandomString {
    param(
        [int]$Length = 32,
        [switch]$Hex,
        [switch]$Base64
    )
    $bytes = New-Object byte[] $(if ($Hex) { [int]($Length / 2) } else { $Length })
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    if ($Hex)    { return ($bytes | ForEach-Object { $_.ToString('x2') }) -join '' }
    if ($Base64) { return [Convert]::ToBase64String($bytes) }
    return [Convert]::ToBase64String($bytes).Substring(0, $Length)
}

# ── Dependency check: Python for Fernet key ───────────────
$PythonCmd = $null
foreach ($cmd in @('python', 'python3')) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) {
        $PythonCmd = $cmd
        break
    }
}
if (-not $PythonCmd) {
    Write-Color "WARNING: Python not found — CREDENTIAL_ENCRYPTION_KEY will not be updated." Yellow
    Write-Color "         Install Python 3 and run: pip install cryptography" Yellow
    Write-Color ""
}

# ── Generate secrets ───────────────────────────────────────
Write-Color "Generating secrets..." Green
Write-Color ""

$PostgresPassword = New-RandomString -Length 32
$SecretKey        = New-RandomString -Length 64 -Hex
$InternalApiKey   = New-RandomString -Length 32 -Base64
$MediamtxSecret   = New-RandomString -Length 64 -Hex

$CredentialKey = $null
if ($PythonCmd) {
    try {
        $CredentialKey = & $PythonCmd -c `
            "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" `
            2>$null
        if ($LASTEXITCODE -ne 0 -or -not $CredentialKey) { $CredentialKey = $null }
    } catch { $CredentialKey = $null }
}

if (-not $CredentialKey) {
    Write-Color "WARNING: Could not generate Fernet key. Install: pip install cryptography" Yellow
    Write-Color "         CREDENTIAL_ENCRYPTION_KEY will not be updated." Yellow
    Write-Color ""
}

# ── Display generated values ───────────────────────────────
Write-Color "  POSTGRES_PASSWORD        = $PostgresPassword"        Gray
Write-Color "  SECRET_KEY                = $SecretKey"               Gray
Write-Color "  CREDENTIAL_ENCRYPTION_KEY = $(if ($CredentialKey) { $CredentialKey } else { '<skipped>' })" Gray
Write-Color "  INTERNAL_API_KEY          = $InternalApiKey"          Gray
Write-Color "  MEDIAMTX_SECRET           = $MediamtxSecret"          Gray
Write-Color ""

# ── Write to .env ──────────────────────────────────────────
if (-not $Write) {
    Write-Color "Dry-run mode — nothing written." Yellow
    Write-Color "Run with -Write to apply:  .\scripts\generate-secrets.ps1 -Write" Cyan
    Write-Color ""
    exit 0
}

# Ensure .env exists
if (-not (Test-Path $EnvFile)) {
    Write-Color ".env not found — copying from .env.example ..." Yellow
    Copy-Item $EnvExample $EnvFile
    Write-Color "✓ .env created from template." Green
}

# Helper: replace key=value in .env file
function Set-EnvVar {
    param([string]$Key, [string]$Value, [string]$File)
    $lines   = Get-Content $File
    $pattern = "^${Key}="
    $newLine = "${Key}=${Value}"
    $found   = $false
    $updated = $lines | ForEach-Object {
        if ($_ -match $pattern) { $found = $true; $newLine }
        else                    { $_ }
    }
    if (-not $found) { $updated += $newLine }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllLines($File, [string[]]$updated, $utf8NoBom)
}

Write-Color "Writing secrets to .env ..." Green

Set-EnvVar "POSTGRES_PASSWORD" $PostgresPassword $EnvFile
Set-EnvVar "SECRET_KEY"        $SecretKey        $EnvFile
Set-EnvVar "INTERNAL_API_KEY"  $InternalApiKey   $EnvFile
Set-EnvVar "MEDIAMTX_SECRET"   $MediamtxSecret   $EnvFile

if ($CredentialKey) {
    Set-EnvVar "CREDENTIAL_ENCRYPTION_KEY" $CredentialKey $EnvFile
}

Write-Color ""
Write-Color "✓ Secrets written to .env" Green
Write-Color ""
Write-Color "IMPORTANT:" Yellow
Write-Color "  1. Never commit .env to version control"
Write-Color "  2. Store a secure backup of these secrets"
Write-Color "  3. CREDENTIAL_ENCRYPTION_KEY cannot be changed after first run"
Write-Color ""
Write-Color "Ready to start:  .\start.ps1 build" Green
Write-Color ""
