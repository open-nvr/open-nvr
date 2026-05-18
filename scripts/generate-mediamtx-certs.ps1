# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
#
# OpenNVR — M1b-fixup H-3: generate a self-signed TLS cert pair for
# MediaMTX's RTSPS / HLS-over-TLS / WebRTC-signaling listeners.
#
# PowerShell counterpart to scripts/generate-mediamtx-certs.sh. Requires
# OpenSSL on PATH (ships with Git for Windows; or `winget install
# ShiningLight.OpenSSL` / `choco install openssl`).
#
# Usage:
#   .\scripts\generate-mediamtx-certs.ps1                # generate if missing
#   .\scripts\generate-mediamtx-certs.ps1 -Force         # overwrite
#   .\scripts\generate-mediamtx-certs.ps1 -OutDir DIR    # write to DIR
#   .\scripts\generate-mediamtx-certs.ps1 -ExtraSan "DNS:opennvr.lan,IP:10.0.0.5"

[CmdletBinding()]
param(
    [string]$OutDir = "./mediamtx-certs",
    [switch]$Force,
    [string]$ExtraSan = ""
)

$ErrorActionPreference = "Stop"

$OutDirResolved = (New-Item -ItemType Directory -Force -Path $OutDir).FullName
$CrtPath = Join-Path $OutDirResolved "server.crt"
$KeyPath = Join-Path $OutDirResolved "server.key"

if ((Test-Path $CrtPath) -and (Test-Path $KeyPath) -and -not $Force) {
    Write-Host "MediaMTX certs already exist at $OutDirResolved (server.crt + server.key)."
    Write-Host "Run with -Force to regenerate."
    exit 0
}

if (-not (Get-Command openssl -ErrorAction SilentlyContinue)) {
    Write-Error "openssl not found on PATH. Install OpenSSL (e.g. winget install ShiningLight.OpenSSL) and re-run."
    exit 69
}

$Subject = "/CN=opennvr-mediamtx"
$San = "DNS:localhost,DNS:mediamtx,IP:127.0.0.1,IP:::1"
if ($ExtraSan -ne "") {
    $San = "$San,$ExtraSan"
}

$ConfigFile = New-TemporaryFile
try {
    @"
[req]
default_bits       = 2048
prompt             = no
default_md         = sha256
req_extensions     = req_ext
distinguished_name = dn

[dn]
CN = opennvr-mediamtx

[req_ext]
subjectAltName = $San

[v3_ca]
subjectAltName     = $San
basicConstraints   = critical, CA:FALSE
keyUsage           = critical, digitalSignature, keyEncipherment
extendedKeyUsage   = serverAuth
"@ | Set-Content -Path $ConfigFile.FullName -Encoding ASCII

    & openssl req -x509 -newkey rsa:2048 -nodes `
        -days 3650 `
        -keyout $KeyPath `
        -out    $CrtPath `
        -config $ConfigFile.FullName `
        -extensions v3_ca 2>$null | Out-Null

    if ($LASTEXITCODE -ne 0) {
        throw "openssl req failed (exit $LASTEXITCODE)"
    }
} finally {
    Remove-Item $ConfigFile.FullName -ErrorAction SilentlyContinue
}

Write-Host "Generated MediaMTX self-signed cert pair:"
Write-Host "  $CrtPath"
Write-Host "  $KeyPath"
Write-Host ""
Write-Host "SAN: $San"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  - For docker-compose deployments, mount this directory into the"
Write-Host "    mediamtx container at the MediaMTX working directory."
Write-Host "  - For bare-metal deployments, copy server.crt and server.key next"
Write-Host "    to your mediamtx.yml file."
Write-Host "  - Browser clients will see an untrusted-cert warning the first"
Write-Host "    time they connect; replace with a CA-signed cert for production."
