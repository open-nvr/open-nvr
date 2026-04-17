# ============================================================
# OpenNVR - Interactive Installation Wizard (Windows)
# ============================================================
# Called automatically by start.ps1 on first run.
# Can also be re-run manually: .\scripts\install.ps1
# ============================================================

param()

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

$ComposeFile = "docker-compose.yml"

# ── Collected settings ─────────────────────────────────────
$DeployMode    = "quick"
$RecordingsPath = ""
$AiEnabled     = $false
$AiRepoUrl     = "https://github.com/open-nvr/ai-adapter.git"
$AdminUsername  = "admin"
$AdminEmail     = "admin@opennvr.local"
$PostgresPassword          = ""
$SecretKey                 = ""
$CredentialEncryptionKey   = ""
$InternalApiKey            = ""
$MediamtxSecret            = ""

# ── Colour helpers ─────────────────────────────────────────
function WC($Text, $Color = "White") { Write-Host $Text -ForegroundColor $Color }
function WCN($Text, $Color = "White") { Write-Host $Text -ForegroundColor $Color -NoNewline }

function OK($msg)   { WC "  `u{2713}  $msg" Green  }
function WARN($msg) { WC "  `u{26A0}  $msg" Yellow }
function FAIL($msg) { WC "  `u{2717}  $msg" Red    }
function INFO($msg) { WC "  `u{00B7}  $msg" DarkGray }
function STEP($msg) { WC "  `u{2192}  $msg" Cyan   }

# ── Logo ───────────────────────────────────────────────────
function Show-Logo {
    Clear-Host
    WC ""
    WCN "  ██████╗ ██████╗ ███████╗███╗   ██╗" White
    WC " ███╗   ██╗██╗   ██╗██████╗ " DarkBlue
    WCN " ██╔═══██╗██╔══██╗██╔════╝████╗  ██║" White
    WC " ████╗  ██║██║   ██║██╔══██╗" DarkBlue
    WCN " ██║   ██║██████╔╝█████╗  ██╔██╗ ██║" White
    WC " ██╔██╗ ██║██║   ██║██████╔╝" DarkBlue
    WCN " ██║   ██║██╔═══╝ ██╔══╝  ██║╚██╗██║" White
    WC " ██║╚██╗██║╚██╗ ██╔╝██╔══██╗" DarkBlue
    WCN " ╚██████╔╝██║     ███████╗██║ ╚████║" White
    WC " ██║ ╚████║ ╚████╔╝ ██║  ██║" DarkBlue
    WCN "  ╚═════╝ ╚═╝     ╚══════╝╚═╝  ╚═══╝" White
    WC " ╚═╝  ╚═══╝  ╚═══╝  ╚═╝  ╚═╝" DarkBlue
    WC ""
    WC "  Open Source Network Video Recorder" White
    WC "  Self-Hosted  ·  AI-Ready  ·  Privacy-First" DarkGray
    WC ""
    WC "  $('━' * 52)" DarkGray
    WC "    Installation Wizard" DarkYellow
    WC "  $('━' * 52)" DarkGray
    WC ""
}

# ── Section header ─────────────────────────────────────────
function Section($title) {
    WC ""
    WC "  ◈  $title" Cyan
    WC "  $('─' * 50)" DarkGray
    WC ""
}

# ── Prompt helpers ─────────────────────────────────────────
function Ask($prompt, $default) {
    WCN "  ?  " Yellow
    WCN "$prompt " White
    WCN "[$default]" DarkGray
    WCN ": " White
    $val = Read-Host
    if ([string]::IsNullOrWhiteSpace($val)) { return $default }
    return $val
}

function AskYN($prompt, $default = "n") {
    $hint = if ($default -eq "y") { "Y/n" } else { "y/N" }
    WCN "  ?  " Yellow
    WCN "$prompt " White
    WCN "[$hint]" DarkGray
    WCN ": " White
    $val = Read-Host
    $val = if ([string]::IsNullOrWhiteSpace($val)) { $default } else { $val }
    return $val -match '^[Yy]'
}

function AskSecret($prompt) {
    WCN "  ?  " Yellow
    WCN "$prompt" White
    WCN ": " White
    $ss = Read-Host -AsSecureString
    return [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($ss))
}

function AskChoice($prompt, $default = "1") {
    WCN "  ?  " Yellow
    WCN "$prompt " White
    WCN "[$default]" DarkGray
    WCN ": " White
    $val = Read-Host
    if ([string]::IsNullOrWhiteSpace($val)) { return $default }
    return $val
}

# ── Random string generator ────────────────────────────────
function New-RandString([int]$Len = 32, [switch]$Hex) {
    $bytes = New-Object byte[] $(if ($Hex) { [int]($Len / 2) } else { $Len + 8 })
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    if ($Hex) { return ($bytes | ForEach-Object { $_.ToString('x2') }) -join '' }
    $encoded = [Convert]::ToBase64String($bytes)
    return $encoded.Replace('+','').Replace('/','').Replace('=','').Substring(0, $Len)
}

# ── STEP 1: Prerequisites ──────────────────────────────────
function Check-Prereqs {
    Section "Checking prerequisites"

    INFO "Platform: Windows (bridge network mode)"
    INFO "Compose file: $ComposeFile"
    WC ""

    $errors = 0

    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) {
        FAIL "Docker is not installed"
        STEP "Install from: https://docs.docker.com/desktop/install/windows-install/"
        $errors++
    } else {
        $ver = (docker --version 2>&1) -replace 'Docker version ',''
        OK "Docker $ver"
    }

    if ($dockerCmd) {
        $null = docker info 2>&1
        if ($LASTEXITCODE -ne 0) {
            FAIL "Docker Desktop is not running"
            STEP "Open Docker Desktop and wait for it to start"
            $errors++
        } else {
            OK "Docker daemon is running"
        }
    }

    $null = docker compose version 2>&1
    if ($LASTEXITCODE -ne 0) {
        FAIL "Docker Compose v2 not found"
        STEP "Update Docker Desktop: https://docs.docker.com/desktop/release-notes/"
        $errors++
    } else {
        $cv = (docker compose version 2>&1) -replace 'Docker Compose version ',''
        OK "Docker Compose $cv"
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        WARN "Git not found — AI adapter cloning will not be available"
    } else {
        $gv = (git --version 2>&1)
        OK "$gv"
    }

    if ($errors -gt 0) {
        WC ""
        FAIL "Fix the errors above then re-run: .\start.ps1"
        exit 1
    }
}

# ── STEP 2: Deployment mode ────────────────────────────────
function Ask-DeployMode {
    Section "Deployment mode"
    WC "  How do you want to set up OpenNVR?" White
    WC ""
    WC "   1  Quick start    · Dev defaults · Up in seconds" Cyan
    WC "   2  Production     · Strong auto-generated secrets · Recommended" Cyan
    WC ""
    $choice = AskChoice "Choose" "1"
    if ($choice -eq "2") {
        $script:DeployMode = "production"
        OK "Production mode — all secrets will be uniquely generated"
    } else {
        $script:DeployMode = "quick"
        OK "Quick start mode — harden later with: .\scripts\generate-secrets.ps1 -Write"
    }
}

# ── STEP 3: Recordings path ────────────────────────────────
function Ask-RecordingsPath {
    Section "Recording storage"
    WC "  Where should camera recordings be stored on this machine?" White
    WC "  This path is bind-mounted into the Docker containers." DarkGray
    WC ""

    $default = "C:\opennvr\recordings"
    $path = Ask "Recordings path" $default
    $script:RecordingsPath = $path -replace '\\','/'

    if (-not (Test-Path $path)) {
        try {
            New-Item -ItemType Directory -Force -Path $path | Out-Null
            OK "Created: $path"
        } catch {
            WARN "Could not create directory — Docker will attempt to create it"
        }
    } else {
        OK "Directory exists: $path"
    }
}

# ── STEP 4: AI detection ───────────────────────────────────
function Ask-AI {
    Section "AI-powered detection (optional)"
    WC "  OpenNVR supports AI object detection via a separate adapter service." White
    WC "  Requires cloning an extra repository (done automatically)." DarkGray
    WC ""

    if (AskYN "Enable AI detection?" "n") {
        $script:AiEnabled = $true
        WC ""
        $script:AiRepoUrl = Ask "AI adapter repository URL" "https://github.com/open-nvr/ai-adapter.git"
        OK "AI detection will be enabled"
    } else {
        $script:AiEnabled = $false
        OK "AI detection disabled — enable later by re-running: .\scripts\install.ps1"
    }
}

# ── STEP 5: Admin account ──────────────────────────────────
function Ask-Admin {
    Section "Administrator account"
    WC "  Set a username and email for the default admin account." White
    WC "  You will complete the full account setup (including password)" White
    WC "  at the first-time setup page after installation." DarkGray
    WC ""

    $script:AdminUsername = Ask "Username" "admin"
    $script:AdminEmail    = Ask "Email"    "admin@opennvr.local"
}

# ── STEP 6: Secrets ────────────────────────────────────────
function Generate-Secrets {
    Section "Generating secrets"

    $script:PostgresPassword        = New-RandString 32
    $script:SecretKey               = New-RandString 64 -Hex
    $script:InternalApiKey          = New-RandString 32
    $script:MediamtxSecret          = New-RandString 64 -Hex
    $script:CredentialEncryptionKey = $null

    $pyCmd = $null
    foreach ($cmd in @('python','python3')) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) { $pyCmd = $cmd; break }
    }
    if ($pyCmd) {
        try {
            $key = & $pyCmd -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" 2>$null
            if ($LASTEXITCODE -eq 0 -and $key) { $script:CredentialEncryptionKey = $key.Trim() }
        } catch {}
    }
    if (-not $script:CredentialEncryptionKey) {
        WARN "Python cryptography not found — using fallback key"
        INFO "Install for proper Fernet keys: pip install cryptography"
        $script:CredentialEncryptionKey = New-RandString 32
    }

    OK "Database password generated"
    OK "JWT secret key generated"
    OK "Credential encryption key generated"
    OK "Internal API key generated"
    OK "MediaMTX webhook secret generated"
}

# ── STEP 7: Clone AI adapter ───────────────────────────────
function Clone-AI {
    if (-not $script:AiEnabled) { return }

    Section "Cloning AI adapter"

    $parentDir  = Split-Path -Parent $ProjectRoot
    $targetDir  = Join-Path $parentDir "ai-adapter"

    if (Test-Path $targetDir) {
        WARN "Directory already exists: $targetDir"
        if (AskYN "Skip cloning and use existing directory?" "y") {
            OK "Using existing ai-adapter at: $targetDir"
            return
        }
        Remove-Item -Recurse -Force $targetDir
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        FAIL "Git is not installed — cannot clone AI adapter"
        WARN "Clone manually: git clone $($script:AiRepoUrl) $targetDir"
        $script:AiEnabled = $false
        return
    }

    WCN "  → Cloning $($script:AiRepoUrl) ..." Cyan
    try {
        $null = git clone $script:AiRepoUrl $targetDir 2>&1
        if ($LASTEXITCODE -eq 0) {
            WC " done" Green
            OK "AI adapter cloned to: $targetDir"

            $weightsDir = Join-Path $targetDir "model_weights"
            if (-not (Test-Path $weightsDir)) { New-Item -ItemType Directory -Path $weightsDir | Out-Null }
        } else {
            throw "git clone failed"
        }
    } catch {
        WC " failed" Red
        FAIL "Clone failed — check the URL and your internet connection"
        WARN "Continuing without AI detection"
        $script:AiEnabled = $false
    }
}

# ── STEP 8: Write .env ─────────────────────────────────────
function Write-EnvFile {
    Section "Writing configuration"

    $adapterLine = if ($script:AiEnabled) {
        "ADAPTER_URL=http://opennvr_ai:9100"
    } else {
        "# ADAPTER_URL=  # Uncomment when AI adapters are enabled"
    }

    $aiEnabledStr = if ($script:AiEnabled) { "true" } else { "false" }

    $content = @"
# ============================================================
# OpenNVR Configuration
# Generated by installer on $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')
# ============================================================

# -- DATABASE -------------------------------------------------
POSTGRES_USER=opennvr_user
POSTGRES_PASSWORD=$($script:PostgresPassword)
POSTGRES_DB=opennvr_db

# -- SECURITY -------------------------------------------------
SECRET_KEY=$($script:SecretKey)
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=120
# WARNING: Never change CREDENTIAL_ENCRYPTION_KEY after first run
CREDENTIAL_ENCRYPTION_KEY=$($script:CredentialEncryptionKey)
INTERNAL_API_KEY=$($script:InternalApiKey)
MEDIAMTX_SECRET=$($script:MediamtxSecret)

# -- APPLICATION ----------------------------------------------
DEBUG=False
HOST=0.0.0.0
PORT=8000
APPLICATION_URL=http://localhost:8000
API_PREFIX=/api/v1

# -- MEDIAMTX -------------------------------------------------
MEDIAMTX_BASE_URL=http://localhost:8889
MEDIAMTX_ADMIN_API=http://localhost:9997/v3
MEDIAMTX_API_URL=http://localhost:9997
MEDIAMTX_HLS_URL=http://localhost:8888
MEDIAMTX_RTSP_URL=rtsp://localhost:8554
MEDIAMTX_PLAYBACK_URL=http://localhost:9996
MEDIAMTX_STREAM_PREFIX=cam-
MEDIAMTX_PATH_MODE=id
MEDIAMTX_AUTO_PROVISION=True

# -- DOCKER NETWORKING ----------------------------------------
BACKEND_HOST=opennvr_core
BACKEND_PORT=8000

# -- RECORDING STORAGE ----------------------------------------
RECORDINGS_PATH=$($script:RecordingsPath)

# -- AI INFERENCE ---------------------------------------------
AI_ENABLED=$aiEnabledStr
KAI_C_URL=http://127.0.0.1:8100
KAI_C_IP=127.0.0.1
$adapterLine

# -- ADMIN USER -----------------------------------------------
DEFAULT_ADMIN_USERNAME=$($script:AdminUsername)
DEFAULT_ADMIN_EMAIL=$($script:AdminEmail)
DEFAULT_ADMIN_FIRST_NAME=System
DEFAULT_ADMIN_LAST_NAME=Administrator

# -- LOGGING --------------------------------------------------
LOG_LEVEL=INFO
LOG_FILE_ENABLED=True
LOG_FILE_PATH=logs/server.log
LOG_FILE_MAX_SIZE_MB=50
LOG_FILE_BACKUP_COUNT=10
LOG_CONSOLE_ENABLED=True
LOG_JSON_FORMAT=False
"@

    $content | Set-Content ".env" -Encoding UTF8
    OK ".env written successfully"
}

# ── Summary ────────────────────────────────────────────────
function Show-Summary {
    WC ""
    WC "  $('━' * 52)" DarkGray
    WC ""
    WC "    Installation Summary" White
    WC ""
    WC "    Platform        Windows ($ComposeFile)" DarkGray
    WC "    Mode            $($script:DeployMode)" DarkGray
    WC "    Recordings      $($script:RecordingsPath)" DarkGray
    $aiStatus = if ($script:AiEnabled) { "enabled" } else { "disabled" }
    WC "    AI Detection    $aiStatus" $(if ($script:AiEnabled) { "Green" } else { "DarkGray" })
    WC ""
    WC "    Admin user      $($script:AdminUsername)" DarkGray
    WC "    Admin email     $($script:AdminEmail)" DarkGray
    WC ""
    WC "  → Complete password setup at the first-time setup page." Cyan
    WC ""
    WC "  $('━' * 52)" DarkGray
}

# ── Launch services ────────────────────────────────────────
function Start-Services {
    WC ""
    if (-not (AskYN "Build and start OpenNVR now?" "y")) {
        WC ""
        INFO "Configuration saved to .env"
        WC ""
        STEP "To start later:"
        WC "    .\start.ps1 build   # first run — builds images" Cyan
        WC "    .\start.ps1         # subsequent starts" Cyan
        WC ""
        return
    }

    WC ""
    Section "Starting OpenNVR"

    $profileArg = @()
    if ($script:AiEnabled) { $profileArg = @("--profile", "ai") }

    WCN "  → Building Docker images (may take a few minutes on first run) ..." Cyan
    docker compose -f $ComposeFile @profileArg build 2>&1 | Where-Object { $_ -match '^(Step|STEP|#\d|Successfully|ERROR)' } | ForEach-Object { Write-Host "     $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -ne 0) { FAIL "Build failed — check the output above"; exit 1 }
    WC " done" Green
    OK "Docker images built"

    WCN "  → Starting all services ..." Cyan
    docker compose -f $ComposeFile @profileArg up -d
    if ($LASTEXITCODE -ne 0) { FAIL "Failed to start services — check the output above"; exit 1 }
    WC " done" Green
    OK "All services started"

    Start-Sleep 3

    WC ""
    WC "  $('━' * 52)" DarkGray
    WC ""
    WC "  ✓  OpenNVR is running!" Green
    WC ""
    WC "  Web Interface  →  http://localhost:8000" Cyan
    WC "  API Docs       →  http://localhost:8000/docs" Cyan
    WC "  First-time setup page opens automatically on first visit." DarkGray
    WC ""
    WC "  Useful commands:" DarkGray
    WC "    .\start.ps1 logs    # follow live logs" DarkGray
    WC "    .\start.ps1 status  # check container health" DarkGray
    WC "    .\start.ps1 down    # stop all services" DarkGray
    WC ""
    WC "  $('━' * 52)" DarkGray
    WC ""
}

# ── MAIN ───────────────────────────────────────────────────
Show-Logo

if (Test-Path ".env") {
    WARN "An existing .env was found."
    WC ""
    if (-not (AskYN "Reconfigure and overwrite existing settings?" "n")) {
        WC ""
        INFO "Installation cancelled. Your .env is unchanged."
        WC ""
        exit 0
    }
    WC ""
}

Check-Prereqs
Ask-DeployMode
Ask-RecordingsPath
Ask-AI
Ask-Admin
Generate-Secrets
Clone-AI
Write-EnvFile
Show-Summary
Start-Services
