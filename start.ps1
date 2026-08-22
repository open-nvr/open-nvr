# ============================================================
# OpenNVR - Smart Launcher (Windows PowerShell)
# ============================================================
# One command does it all: run .\start.ps1 with no arguments. On a fresh
# checkout it launches the interactive installer (creates and configures .env,
# builds, and starts). On later runs it asks whether to start as-is or
# reconfigure. The sub-commands below are for scripted / power use.
#
# Usage:
#   .\start.ps1              # smart start: install on first run, else start/reconfigure
#   .\start.ps1 up           # start now using the existing .env (no prompt)
#   .\start.ps1 build        # rebuild images and start
#   .\start.ps1 install      # re-run the interactive installer (reconfigure)
#   .\start.ps1 reconfigure  # alias for install
#   .\start.ps1 down         # stop all services
#   .\start.ps1 logs         # tail logs
#   .\start.ps1 status       # show container status
#   .\start.ps1 validate     # run pre-flight checks only
#   .\start.ps1 token        # re-print the first-time setup token
# ============================================================

param(
    [string]$Command = "start"
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
    # Read as UTF-8 explicitly — Windows PowerShell 5.1's Get-Content defaults to
    # ANSI, which mis-decodes any non-ASCII the installer wrote as UTF-8.
    $lines = [IO.File]::ReadAllLines((Resolve-Path ".env"), (New-Object Text.UTF8Encoding($false)))
    $line = $lines | Where-Object { $_ -match "^${Key}=" } | Select-Object -First 1
    if ($line) { return ($line -split '=', 2)[1].Trim('"').Trim("'") }
    return $null
}

# ── Build Compose profile args ─────────────────────────────
function Get-ComposeArgs {
    $args = @("-f", $ComposeFile)
    $exampleCompose = Get-EnvVar "OPENNVR_EXAMPLE_COMPOSE"
    $exampleProfile = Get-EnvVar "OPENNVR_EXAMPLE_PROFILE"
    if ($exampleCompose) {
        if (-not (Test-Path $exampleCompose)) {
            if ($exampleCompose -like "*camera-agent-lite*") {
                Write-Color "camera-agent-lite was removed - the camera-agent example (with OLLAMA_EXTERNAL_URL for host Ollama) replaces it." Yellow
                Write-Color "Fix: re-run scripts\install.ps1 reconfigure, or clear the OPENNVR_EXAMPLE* lines in .env." Yellow
            }
            throw "Configured example Compose file not found: $exampleCompose"
        }
        $args += @("-f", $exampleCompose)
        # External LLM runtime (.env OLLAMA_EXTERNAL_URL): overlay that skips
        # the bundled ollama container and points the agent at the operator's
        # endpoint — the GPU path on macOS/Windows, where the in-VM container
        # is CPU-only. Mirrors compose_args in start.sh.
        $externalLlm = Get-EnvVar "OLLAMA_EXTERNAL_URL"
        if ($externalLlm -and $exampleCompose -like "*camera-agent.yml") {
            $args += @("-f", "docker-compose.camera-agent.external-llm.yml")
        }
    }
    if ($exampleProfile) { $args += @("--profile", $exampleProfile) }
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

    # 1. Docker — probe with the error-action policy relaxed so the daemon's
    # stderr (when it's down) doesn't surface as a NativeCommandError stack trace.
    $prevEAP = $ErrorActionPreference; $ErrorActionPreference = 'SilentlyContinue'
    docker info 2>$null | Out-Null; $dockerUp = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if (-not $dockerUp) {
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
    # after `compose up -d --remove-orphans`. But `up -d` returns when containers are
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
            $lines = $raw -split "
"
            for ($i = $lines.Length - 1; $i -ge 0; $i--) {
                if ($lines[$i] -match "first-time setup token") {
                    $end = [Math]::Min($i + 6, $lines.Length - 1)
                    $banner = ($lines[$i..$end] -join "
")
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
        foreach ($line in ($banner -split "
")) { Write-Color ("  " + $line) White }
        Write-Color ""
    } else {
        # Container healthy AND no banner = admin already activated on
        # a previous boot. Unambiguous now.
        $adminUser = "admin"
        try { $got = Get-EnvVar "DEFAULT_ADMIN_USERNAME"; if (-not [string]::IsNullOrWhiteSpace($got)) { $adminUser = $got } } catch { }
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

function Show-RunningInfo {
    $u = Get-EnvVar "DEFAULT_ADMIN_USERNAME"
    if ([string]::IsNullOrWhiteSpace($u)) { $u = 'admin' }
    Write-Color ""
    Write-Color "  ✓ OpenNVR is running!" Green
    Write-Color "  Web UI (local) → http://localhost:8000  (login: $u)" Cyan
    Write-Color "  Web UI (HTTPS) → https://localhost/" Cyan
    Write-Color "  Web UI (LAN)   → https://<this-host-ip>/" Cyan
    Write-Color "  API Docs       → http://localhost:8000/docs" Cyan
    # If an agent example is active, surface its demo URL(s) too. The agents
    # serve their own https on the LAN (sign in with your OpenNVR account).
    $exProfile = Get-EnvVar "OPENNVR_EXAMPLE_PROFILE"
    $example = Get-EnvVar "OPENNVR_EXAMPLE"
    if ($exProfile -in @('camera-agent', 'camera-agent-chat') -or $example -eq 'camera-agent') {
        Write-Color "  Camera Agent   → https://localhost:9100/demo  (ask your cameras - voice or chat)" Cyan
        Write-Color "  Camera Agent (LAN) → https://<this-host-ip>:9100/demo  (OpenNVR login)" Cyan
    }
    Write-Color "  First-time setup page opens automatically on first visit." DarkGray
}

# ── Host LAN IP detection (OPENNVR_HOST_IP) ────────────────
# The core container runs on a Docker bridge and can't see the host's LAN, so
# ONVIF discovery (and the TLS cert SAN) needs the host's LAN IP passed in.
# Mirrors start.sh's route-aware detect_lan_ip: default-route NIC first, VPN
# tunnels skipped, and an operator-set OPENNVR_HOST_IP (.env or process env)
# always wins.
function Test-VpnAddress {
    param([string]$InterfaceAlias, [string]$Ip)
    if ($InterfaceAlias -match 'Tailscale|WireGuard|OpenVPN|ZeroTier|Nebula|TAP') { return $true }
    # CGNAT 100.64.0.0/10 — Tailscale-style overlay addresses.
    $parts = $Ip -split '\.'
    if ($parts.Count -eq 4 -and [int]$parts[0] -eq 100 -and [int]$parts[1] -ge 64 -and [int]$parts[1] -le 127) { return $true }
    return $false
}

function Test-PrivateIPv4 {
    param([string]$Ip)
    $parts = $Ip -split '\.'
    if ($parts.Count -ne 4) { return $false }
    $a = [int]$parts[0]; $b = [int]$parts[1]
    if ($a -eq 10) { return $true }
    if ($a -eq 172 -and $b -ge 16 -and $b -le 31) { return $true }
    if ($a -eq 192 -and $b -eq 168) { return $true }
    return $false
}

# Every private IPv4 on a physical, connected, non-VPN adapter — default-route
# NIC's address first. Physical-only skips Hyper-V/WSL vEthernet switches and
# WiFi-Direct pseudo-adapters, whose host-side IPs are never a camera LAN.
function Get-LanIPs {
    $found = New-Object System.Collections.Generic.List[string]
    try {
        $physUp = @(Get-NetAdapter -Physical -ErrorAction Stop | Where-Object { $_.Status -eq 'Up' })
        $defaultIfIndexes = @(
            try {
                Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction Stop |
                    Sort-Object -Property RouteMetric, InterfaceMetric |
                    Select-Object -ExpandProperty InterfaceIndex
            } catch { @() }
        )
        # Default-route adapter first, then the rest (e.g. a second NIC on a
        # dedicated camera network).
        $ordered = @($physUp | Where-Object { $defaultIfIndexes -contains $_.ifIndex }) +
                   @($physUp | Where-Object { $defaultIfIndexes -notcontains $_.ifIndex })
        foreach ($ad in $ordered) {
            $addrs = try { Get-NetIPAddress -InterfaceIndex $ad.ifIndex -AddressFamily IPv4 -ErrorAction Stop } catch { @() }
            foreach ($a in $addrs) {
                $ip = $a.IPAddress
                if ($ip -like '127.*' -or $ip -like '169.254.*') { continue }
                if (Test-VpnAddress $ad.InterfaceAlias $ip) { continue }
                if ((Test-PrivateIPv4 $ip) -and -not $found.Contains($ip)) { $found.Add($ip) }
            }
        }
    } catch {}
    if ($found.Count -eq 0) {
        try {
            # Fallback: any private IPv4 on a non-VPN interface.
            foreach ($a in (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction Stop)) {
                $ip = $a.IPAddress
                if ($ip -like '127.*' -or $ip -like '169.254.*') { continue }
                if (Test-VpnAddress $a.InterfaceAlias $ip) { continue }
                if ((Test-PrivateIPv4 $ip) -and -not $found.Contains($ip)) { $found.Add($ip) }
            }
        } catch {}
    }
    return $found
}

function Set-HostIpEnv {
    $hostIpSet = -not [string]::IsNullOrWhiteSpace($env:OPENNVR_HOST_IP) -or
                 -not [string]::IsNullOrWhiteSpace((Get-EnvVar "OPENNVR_HOST_IP"))
    $lanIpsSet = -not [string]::IsNullOrWhiteSpace($env:OPENNVR_LAN_IPS) -or
                 -not [string]::IsNullOrWhiteSpace((Get-EnvVar "OPENNVR_LAN_IPS"))
    if ($hostIpSet -and $lanIpsSet) { return }
    $lanIps = Get-LanIPs
    if ($lanIps.Count -eq 0) { return }
    # Process env feeds compose ${VAR:-} interpolation; operator .env wins.
    if (-not $hostIpSet) {
        # Single IP only — this one also lands in TLS cert SANs.
        $env:OPENNVR_HOST_IP = $lanIps[0]
        Write-Color "  Detected host LAN IP: $($lanIps[0]) (TLS cert SAN + camera discovery)" DarkGray
    }
    if (-not $lanIpsSet -and $lanIps.Count -gt 1) {
        $env:OPENNVR_LAN_IPS = ($lanIps -join ',')
        Write-Color "  Additional LAN NIC IP(s): $(($lanIps | Select-Object -Skip 1) -join ', ') (camera discovery scans these subnets too)" DarkGray
    }
}

# ── Host LAN IP hint file (./data/net-hints/host-ips) ──────
# The OPENNVR_HOST_IP/OPENNVR_LAN_IPS env vars are frozen into the core
# container at creation, so they go stale when the host moves to another
# subnet. This file is bind-mounted read-only into the container (see
# docker-compose.yml) and rewritten on every launcher run; the server's
# detect_local_subnets() reads it first, so ONVIF discovery follows the
# host's *current* networks without a container recreate.
function Write-NetHints {
    $hints = New-Object System.Collections.Generic.List[string]
    foreach ($candidate in @(
        $env:OPENNVR_HOST_IP, (Get-EnvVar "OPENNVR_HOST_IP"),
        $env:OPENNVR_LAN_IPS, (Get-EnvVar "OPENNVR_LAN_IPS"))) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        foreach ($token in ($candidate -split '[,\s]+')) {
            if ($token -and -not $hints.Contains($token)) { $hints.Add($token) }
        }
    }
    $file = Join-Path "data\net-hints" "host-ips"
    try {
        if ($hints.Count -gt 0) {
            New-Item -ItemType Directory -Force "data\net-hints" | Out-Null
            # ASCII avoids a BOM the container-side parser would choke on.
            Set-Content -Path $file -Value ($hints -join ' ') -Encoding Ascii
        } elseif (Test-Path $file) {
            Remove-Item -Force $file -Confirm:$false
        }
    } catch {}
}

# ── Raw start / build (no front-door prompt) ───────────────
# These assume .env exists — the smart Invoke-Start and the installer
# guarantee that before calling them. Kept separate so the installer can call
# `start.ps1 up` without re-triggering the front door (which would loop).
function Invoke-Up {
    if (-not (Test-Path ".env")) {
        Write-Color "  No .env found. Run .\start.ps1 (no arguments) to set up." Red
        exit 1
    }
    Show-Banner
    if (-not (Invoke-Validate)) { exit 1 }
    Set-HostIpEnv
    Write-NetHints
    $ca = Get-ComposeArgs
    Write-Color "  Starting all services ..." Green
    docker compose @ca up -d --remove-orphans
    Show-RunningInfo
    Show-FirstTimeSetupToken -ComposeArgs $ca
}

function Invoke-Build {
    if (-not (Test-Path ".env")) {
        Write-Color "  No .env found. Run .\start.ps1 (no arguments) to set up." Red
        exit 1
    }
    Show-Banner
    if (-not (Invoke-Validate)) { exit 1 }
    Set-HostIpEnv
    Write-NetHints
    $ca = Get-ComposeArgs
    Write-Color "  Building images and starting all services ..." Green
    docker compose @ca build
    docker compose @ca up -d --remove-orphans
    Show-RunningInfo
    Show-FirstTimeSetupToken -ComposeArgs $ca
}

# ── Smart front door (bare .\start.ps1) ────────────────────
# No .env yet          → run the installer (creates/configures .env, builds, starts).
# .env exists + console → ask start-as-is vs reconfigure.
# .env exists, no TTY   → just start (CI / piped input: never block).
function Invoke-Start {
    $installer = Join-Path $PSScriptRoot "scripts\install.ps1"
    if (-not (Test-Path ".env")) {
        Write-Color "  First run — launching the OpenNVR installer ..." Green
        & $installer
        exit $LASTEXITCODE
    }
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
    if ($interactive) {
        Write-Color ""
        Write-Color "  An existing OpenNVR configuration (.env) was found." White
        Write-Color "    1) Start with the current configuration" Gray
        Write-Color "    2) Reconfigure (change settings / example), then start" Gray
        Write-Color "    3) Quit" Gray
        Write-Color ""
        $choice = Read-Host "  Your choice [1]"
        if ([string]::IsNullOrWhiteSpace($choice)) { $choice = "1" }
        switch ($choice) {
            "1" { Invoke-Up }
            "2" { & $installer reconfigure; exit $LASTEXITCODE }
            "3" { Write-Color "  Nothing started." Gray; exit 0 }
            default { Write-Color "  Invalid choice: $choice" Red; exit 1 }
        }
    } else {
        Invoke-Up
    }
}

# ── Run command ────────────────────────────────────────────
switch ($Command) {

    "start" { Invoke-Start }

    "install"     { & (Join-Path $PSScriptRoot "scripts\install.ps1") reconfigure; exit $LASTEXITCODE }
    "reconfigure" { & (Join-Path $PSScriptRoot "scripts\install.ps1") reconfigure; exit $LASTEXITCODE }

    "up"    { Invoke-Up }
    "build" { Invoke-Build }

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

    "token" {
        # Re-surface the first-time setup token on demand. Mints nothing — just
        # reads what opennvr-core already printed. Says so if setup is complete.
        $ca = if (Test-Path ".env") { Get-ComposeArgs } else { @("-f", $ComposeFile) }
        Show-FirstTimeSetupToken -ComposeArgs $ca
    }

    "refresh-net" {
        # Rewrite data\net-hints\host-ips from the host's CURRENT networks —
        # and nothing else: no validation, no compose, no container churn.
        # The file is bind-mounted read-only into opennvr-core and re-read on
        # every discovery request, so camera discovery follows a host that
        # moved to a new network the moment this finishes. The camera dialog's
        # network dropdown points operators here.
        Set-HostIpEnv
        Write-NetHints
        $hintsFile = Join-Path "data\net-hints" "host-ips"
        if (Test-Path $hintsFile) {
            Write-Color "  ✓ Network hints refreshed: $(Get-Content $hintsFile)" Green
            Write-Color "  Camera discovery picks this up immediately - no restart needed." DarkGray
        } else {
            Write-Color "  ⚠ No LAN address detected; hint file cleared." Yellow
        }
    }

    default {
        Write-Color "Unknown command: $Command" Red
        Write-Color "Usage: .\start.ps1 [start|up|build|down|logs|status|validate|token|refresh-net|install|reconfigure]"
        exit 1
    }
}
