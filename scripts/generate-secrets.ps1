# ============================================
# OpenNVR NVR - Secrets Generator (PowerShell)
# ============================================
# This script generates all required secrets for .env file
# Run this script and copy the output to your .env file
# Usage: .\generate-secrets.ps1
# ============================================

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "OPENNVR NVR - SECURITY SECRETS GENERATOR" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Generating secure random secrets..." -ForegroundColor Yellow
Write-Host ""

# Function to generate random string
function Get-RandomString {
    param (
        [int]$Length = 32,
        [switch]$Hex
    )
    
    if ($Hex) {
        $bytes = New-Object byte[] ($Length / 2)
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        return ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
    } else {
        $bytes = New-Object byte[] $Length
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
        return [Convert]::ToBase64String($bytes).Substring(0, $Length)
    }
}

# Check if Python is available
$pythonAvailable = $false
$pythonCmd = $null

if (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonCmd = "python"
    $pythonAvailable = $true
} elseif (Get-Command python3 -ErrorAction SilentlyContinue) {
    $pythonCmd = "python3"
    $pythonAvailable = $true
}

if (-not $pythonAvailable) {
    Write-Host "WARNING: Python not found! Cannot generate CREDENTIAL_ENCRYPTION_KEY" -ForegroundColor Yellow
    Write-Host "Please install Python 3 and cryptography package" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host "============================================" -ForegroundColor Green
Write-Host "COPY THESE VALUES TO YOUR .env FILE" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""

# Generate database password
$dbPassword = Get-RandomString -Length 32
Write-Host "# Database Configuration" -ForegroundColor Gray
Write-Host "POSTGRES_PASSWORD=$dbPassword"
Write-Host "DATABASE_URL=postgresql://opennvr_user:$dbPassword@db:5432/opennvr_db"
Write-Host ""

# Generate JWT secret key
$secretKey = Get-RandomString -Length 64 -Hex
Write-Host "# JWT Secret Key" -ForegroundColor Gray
Write-Host "SECRET_KEY=$secretKey"
Write-Host ""

# Generate credential encryption key
if ($pythonAvailable) {
    try {
        $credentialKey = & $pythonCmd -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>$null
        if ($LASTEXITCODE -eq 0 -and $credentialKey) {
            Write-Host "# Credential Encryption Key" -ForegroundColor Gray
            Write-Host "CREDENTIAL_ENCRYPTION_KEY=$credentialKey"
        } else {
            Write-Host "# Credential Encryption Key (FAILED - install cryptography: uv add cryptography)" -ForegroundColor Yellow
            Write-Host "CREDENTIAL_ENCRYPTION_KEY=GENERATE_MANUALLY"
        }
    } catch {
        Write-Host "# Credential Encryption Key (Error generating - install: uv add cryptography)" -ForegroundColor Yellow
        Write-Host "CREDENTIAL_ENCRYPTION_KEY=GENERATE_MANUALLY"
    }
} else {
    Write-Host "# Credential Encryption Key (Python not found)" -ForegroundColor Yellow
    Write-Host "CREDENTIAL_ENCRYPTION_KEY=GENERATE_MANUALLY"
}
Write-Host ""

# Generate internal API key
$internalApiKey = Get-RandomString -Length 32
Write-Host "# Internal API Key" -ForegroundColor Gray
Write-Host "INTERNAL_API_KEY=$internalApiKey"
Write-Host ""

# Generate MediaMTX secret
$mediamtxSecret = Get-RandomString -Length 32 -Hex
Write-Host "# MediaMTX Secret" -ForegroundColor Gray
Write-Host "MEDIAMTX_SECRET=$mediamtxSecret"
Write-Host ""

Write-Host "============================================" -ForegroundColor Green
Write-Host "GENERATION COMPLETE" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Green
Write-Host ""
Write-Host "⚠️  IMPORTANT:" -ForegroundColor Yellow
Write-Host "  1. Copy ALL the values above to your .env file"
Write-Host "  2. Keep these secrets SECURE and PRIVATE"
Write-Host "  3. NEVER commit .env file to version control"
Write-Host "  4. Store a backup of these secrets in a secure location"
Write-Host ""
Write-Host "✅ After copying to .env, you can run:" -ForegroundColor Green
Write-Host "   docker compose up -d"
Write-Host ""

# Optional: Save to file
$saveToFile = Read-Host "Save secrets to secrets.txt? (y/N)"
if ($saveToFile -eq 'y' -or $saveToFile -eq 'Y') {
    $outputFile = "secrets-$(Get-Date -Format 'yyyyMMdd-HHmmss').txt"
    @"
# OpenNVR NVR Secrets - Generated $(Get-Date)
# KEEP THIS FILE SECURE - DO NOT COMMIT TO GIT

# Database Configuration
POSTGRES_PASSWORD=$dbPassword
DATABASE_URL=postgresql://opennvr_user:$dbPassword@db:5432/opennvr_db

# JWT Secret Key
SECRET_KEY=$secretKey

# Credential Encryption Key
CREDENTIAL_ENCRYPTION_KEY=$credentialKey

# Internal API Key
INTERNAL_API_KEY=$internalApiKey

# MediaMTX Secret
MEDIAMTX_SECRET=$mediamtxSecret
"@ | Out-File -FilePath $outputFile -Encoding UTF8
    
    Write-Host "✅ Secrets saved to: $outputFile" -ForegroundColor Green
    Write-Host "⚠️  Remember to delete this file after copying to .env!" -ForegroundColor Yellow
}
