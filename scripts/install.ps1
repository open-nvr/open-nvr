# OpenNVR interactive installer for Windows (also detects PowerShell on Linux/macOS).
# Mode: 'install' (fresh; fill missing, keep existing) or 'reconfigure'
# (re-prompt values with the current value as default).
param([string]$Mode = 'install')
$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$BaseCompose = 'docker-compose.yml'
Set-Location $ProjectRoot

function Info([string]$Message) { Write-Host "  $Message" }
function Ok([string]$Message) { Write-Host "  ✓ $Message" -ForegroundColor Green }
function Warn([string]$Message) { Write-Host "  ⚠ $Message" -ForegroundColor Yellow }
function Fail([string]$Message) { Write-Host "  X $Message" -ForegroundColor Red; exit 1 }
function Show-Logo {
    $c = 'Cyan'
    Write-Host ''
    Write-Host '   ___                   _   ___     ______ ' -ForegroundColor $c
    Write-Host '  / _ \ _ __   ___ _ __ | \ | \ \   / /  _ \' -ForegroundColor $c
    Write-Host " | | | | '_ \ / _ \ '_ \|  \| |\ \ / /| |_) |" -ForegroundColor $c
    Write-Host ' | |_| | |_) |  __/ | | | |\  | \ V / |  _ < ' -ForegroundColor $c
    Write-Host '  \___/| .__/ \___|_| |_|_| \_|  \_/  |_| \_\' -ForegroundColor $c
    Write-Host '       |_|                                   ' -ForegroundColor $c
    Write-Host '  Self-hosted NVR — the cameras are yours.' -ForegroundColor DarkGray
    Write-Host ''
}
function Ask-YesNo([string]$Prompt, [bool]$Default = $false) {
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    $answer = Read-Host "  $Prompt [$hint]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer -match '^[Yy]'
}
function Ask-Value([string]$Prompt, [string]$Default) {
    $answer = Read-Host "  $Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer
}
function Ask-Secret([string]$Prompt) {
    $secure = Read-Host "  $Prompt" -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}
function Explain([string]$What, [string]$Required, [string]$Default, [string]$Where = '') {
    Write-Host "  $What"
    Write-Host ("    required: {0,-4}  default: {1}" -f $Required, $Default)
    if ($Where) { Write-Host "    note: $Where" }
}
# Curated, ALWAYS-prompted value with an explanation. Enter keeps the current
# .env value (or the given default on a fresh install); typing overrides it.
function Configure-Value([string]$Key, [string]$Label, [string]$Default, [string]$What, [string]$Required, [string]$Where = '') {
    $current = Get-EnvValue $Key
    if (-not [string]::IsNullOrWhiteSpace($current)) { $Default = $current }
    Write-Host ''
    Explain $What $Required $Default $Where
    Set-EnvValue $Key (Ask-Value $Label $Default)
}

# Docker's platform vocabulary ('amd64'/'arm64'), not .NET's. Returns an
# empty string for anything unrecognised, which disables the preflight
# rather than guessing a platform string and rejecting a valid install.
function Get-HostArch {
    $a = ''
    # RuntimeInformation is the portable source; PROCESSOR_ARCHITECTURE is
    # the Windows PowerShell 5.1 fallback, where that type is unavailable.
    try { $a = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString() }
    catch { $a = $env:PROCESSOR_ARCHITECTURE }
    switch -Regex ($a) {
        '^(X64|AMD64)$'   { return 'amd64' }
        '^(Arm64|ARM64)$' { return 'arm64' }
        '^(Arm|ARM)$'     { return 'arm' }
        default           { return '' }
    }
}

function Detect-Platform {
    if ($IsLinux) { $script:Platform = 'Linux'; $script:DefaultRecordings = '/var/lib/opennvr/recordings' }
    elseif ($IsMacOS) { $script:Platform = 'macOS'; $script:DefaultRecordings = '/Users/Shared/opennvr-recordings' }
    else { $script:Platform = 'Windows'; $script:DefaultRecordings = 'C:/opennvr/recordings' }
    $script:HostArch = Get-HostArch
    $shown = if ($script:HostArch) { $script:HostArch } else { 'unknown-arch' }
    Ok "Detected $script:Platform/$shown (Docker bridge mode)"
}

# Does this image's manifest list carry an entry for the host architecture?
#   0 - yes
#   1 - the image resolves, but has no build for this architecture
#   2 - undeterminable (no buildx, private image, registry hiccup, or a
#       single-arch manifest with no platform metadata). Never a reason to
#       block an install: a false 'unsupported' is worse than the raw daemon
#       error this preflight exists to replace.
function Test-ImageArch([string]$Image) {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $raw = (docker buildx imagetools inspect --raw $Image 2>$null | Out-String)
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prevEAP
    if ($code -ne 0 -or -not $raw) { return 2 }
    # A bare image manifest (schema 2, single platform) has no 'manifests'
    # key and therefore no platform metadata to check.
    if ($raw -notmatch '"manifests"') { return 2 }
    # Buildx attestation entries sit alongside the real ones with
    # "architecture":"unknown", so matching the host arch specifically is
    # what distinguishes a usable build from provenance metadata.
    $flat = $raw -replace '\s', ''
    if ($flat -match ('"architecture":"' + [regex]::Escape($script:HostArch) + '"')) { return 0 }
    return 1
}

# Preflight: name the images that cannot run here, before the pull starts.
# `docker compose pull` resolves every image's manifest list against
# linux/$HostArch, and when one has no matching entry the daemon aborts the
# whole pull with a bare 'no matching manifest for linux/arm64/v8' - no image
# name, no explanation, several services in. Only meaningful off amd64; every
# image in the stack publishes amd64.
function Check-ImageArchitectures {
    if (-not $script:HostArch -or $script:HostArch -eq 'amd64') { return }

    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    docker buildx version 2>$null | Out-Null
    $buildxOk = ($LASTEXITCODE -eq 0)
    $images = @()
    if ($buildxOk) { $images = @(docker compose -f $BaseCompose config --images 2>$null) }
    $ErrorActionPreference = $prevEAP
    if (-not $buildxOk -or $images.Count -eq 0) { return }

    Info "Checking that the pinned images publish a linux/$script:HostArch build..."
    $unsupported = @()
    foreach ($image in $images) {
        if (-not $image) { continue }
        if ((Test-ImageArch $image) -eq 1) { $unsupported += $image }
    }
    if ($unsupported.Count -eq 0) {
        Ok "Every core image has a linux/$script:HostArch build"
        return
    }

    Write-Host ''
    Warn "This machine is $script:Platform/$script:HostArch. These images publish no linux/$script:HostArch build:"
    foreach ($image in $unsupported) { Write-Host "      $image" }
    Write-Host ''

    if ($env:OPENNVR_ALLOW_EMULATION -eq '1') {
        # Set on the process environment, not a local: the installer hands off
        # to start.ps1 / start.sh, and every later `docker compose` call has to
        # resolve the same way or the stack comes up half-emulated.
        $env:DOCKER_DEFAULT_PLATFORM = 'linux/amd64'
        Warn 'OPENNVR_ALLOW_EMULATION=1 - pulling the linux/amd64 builds and running'
        Warn 'them under emulation. Detection latency will be several times worse.'
        Warn 'In Docker Desktop, enable Settings -> General -> "Use Rosetta for'
        Warn 'x86_64/amd64 emulation" first, or containers may fail to start at all.'
        Write-Host ''
        return
    }

    Write-Host '  Nothing has been downloaded. Left alone, the pull fails partway through'
    Write-Host "  with `"no matching manifest for linux/$script:HostArch/v8`" and no indication"
    Write-Host '  of which image caused it.'
    Write-Host ''
    Write-Host '  Options:'
    Write-Host '    1. Install on an amd64 host.'
    Write-Host '    2. Run the amd64 builds under emulation - slower, but functional.'
    Write-Host '       In Docker Desktop enable Settings -> General -> "Use Rosetta for'
    Write-Host '       x86_64/amd64 emulation", then re-run:'
    Write-Host '         $env:OPENNVR_ALLOW_EMULATION = "1"; .\scripts\install.ps1'
    Write-Host '    3. Build the images above from source for this architecture.'
    Write-Host ''
    Fail "Aborting: $($unsupported.Count) image(s) have no linux/$script:HostArch build."
}

# CLDR windowsZones primary (territory 001) mapping. Windows PowerShell 5.1
# has no TryConvertWindowsIdToIanaId, so Detect-Timezone falls back to this.
$script:WindowsToIana = @{
    'Dateline Standard Time' = 'Etc/GMT+12'; 'UTC-11' = 'Etc/GMT+11'
    'Aleutian Standard Time' = 'America/Adak'; 'Hawaiian Standard Time' = 'Pacific/Honolulu'
    'Marquesas Standard Time' = 'Pacific/Marquesas'; 'Alaskan Standard Time' = 'America/Anchorage'
    'UTC-09' = 'Etc/GMT+9'; 'Pacific Standard Time (Mexico)' = 'America/Tijuana'
    'UTC-08' = 'Etc/GMT+8'; 'Pacific Standard Time' = 'America/Los_Angeles'
    'US Mountain Standard Time' = 'America/Phoenix'; 'Mountain Standard Time (Mexico)' = 'America/Mazatlan'
    'Mountain Standard Time' = 'America/Denver'; 'Yukon Standard Time' = 'America/Whitehorse'
    'Central America Standard Time' = 'America/Guatemala'; 'Central Standard Time' = 'America/Chicago'
    'Easter Island Standard Time' = 'Pacific/Easter'; 'Central Standard Time (Mexico)' = 'America/Mexico_City'
    'Canada Central Standard Time' = 'America/Regina'; 'SA Pacific Standard Time' = 'America/Bogota'
    'Eastern Standard Time (Mexico)' = 'America/Cancun'; 'Eastern Standard Time' = 'America/New_York'
    'Haiti Standard Time' = 'America/Port-au-Prince'; 'Cuba Standard Time' = 'America/Havana'
    'US Eastern Standard Time' = 'America/Indiana/Indianapolis'; 'Turks And Caicos Standard Time' = 'America/Grand_Turk'
    'Paraguay Standard Time' = 'America/Asuncion'; 'Atlantic Standard Time' = 'America/Halifax'
    'Venezuela Standard Time' = 'America/Caracas'; 'Central Brazilian Standard Time' = 'America/Cuiaba'
    'SA Western Standard Time' = 'America/La_Paz'; 'Pacific SA Standard Time' = 'America/Santiago'
    'Newfoundland Standard Time' = 'America/St_Johns'; 'Tocantins Standard Time' = 'America/Araguaina'
    'E. South America Standard Time' = 'America/Sao_Paulo'; 'SA Eastern Standard Time' = 'America/Cayenne'
    'Argentina Standard Time' = 'America/Argentina/Buenos_Aires'; 'Montevideo Standard Time' = 'America/Montevideo'
    'Magallanes Standard Time' = 'America/Punta_Arenas'; 'Saint Pierre Standard Time' = 'America/Miquelon'
    'Bahia Standard Time' = 'America/Bahia'; 'UTC-02' = 'Etc/GMT+2'
    'Greenland Standard Time' = 'America/Nuuk'; 'Azores Standard Time' = 'Atlantic/Azores'
    'Cape Verde Standard Time' = 'Atlantic/Cape_Verde'; 'UTC' = 'Etc/UTC'
    'GMT Standard Time' = 'Europe/London'; 'Greenwich Standard Time' = 'Atlantic/Reykjavik'
    'Sao Tome Standard Time' = 'Africa/Sao_Tome'; 'Morocco Standard Time' = 'Africa/Casablanca'
    'W. Europe Standard Time' = 'Europe/Berlin'; 'Central Europe Standard Time' = 'Europe/Budapest'
    'Romance Standard Time' = 'Europe/Paris'; 'Central European Standard Time' = 'Europe/Warsaw'
    'W. Central Africa Standard Time' = 'Africa/Lagos'; 'Jordan Standard Time' = 'Asia/Amman'
    'GTB Standard Time' = 'Europe/Bucharest'; 'Middle East Standard Time' = 'Asia/Beirut'
    'Egypt Standard Time' = 'Africa/Cairo'; 'E. Europe Standard Time' = 'Europe/Chisinau'
    'Syria Standard Time' = 'Asia/Damascus'; 'West Bank Standard Time' = 'Asia/Hebron'
    'South Africa Standard Time' = 'Africa/Johannesburg'; 'FLE Standard Time' = 'Europe/Kyiv'
    'Israel Standard Time' = 'Asia/Jerusalem'; 'South Sudan Standard Time' = 'Africa/Juba'
    'Kaliningrad Standard Time' = 'Europe/Kaliningrad'; 'Sudan Standard Time' = 'Africa/Khartoum'
    'Libya Standard Time' = 'Africa/Tripoli'; 'Namibia Standard Time' = 'Africa/Windhoek'
    'Arabic Standard Time' = 'Asia/Baghdad'; 'Turkey Standard Time' = 'Europe/Istanbul'
    'Arab Standard Time' = 'Asia/Riyadh'; 'Belarus Standard Time' = 'Europe/Minsk'
    'Russian Standard Time' = 'Europe/Moscow'; 'E. Africa Standard Time' = 'Africa/Nairobi'
    'Volgograd Standard Time' = 'Europe/Volgograd'; 'Iran Standard Time' = 'Asia/Tehran'
    'Arabian Standard Time' = 'Asia/Dubai'; 'Astrakhan Standard Time' = 'Europe/Astrakhan'
    'Azerbaijan Standard Time' = 'Asia/Baku'; 'Russia Time Zone 3' = 'Europe/Samara'
    'Mauritius Standard Time' = 'Indian/Mauritius'; 'Saratov Standard Time' = 'Europe/Saratov'
    'Georgian Standard Time' = 'Asia/Tbilisi'; 'Caucasus Standard Time' = 'Asia/Yerevan'
    'Afghanistan Standard Time' = 'Asia/Kabul'; 'West Asia Standard Time' = 'Asia/Tashkent'
    'Ekaterinburg Standard Time' = 'Asia/Yekaterinburg'; 'Pakistan Standard Time' = 'Asia/Karachi'
    'Qyzylorda Standard Time' = 'Asia/Qyzylorda'; 'India Standard Time' = 'Asia/Kolkata'
    'Sri Lanka Standard Time' = 'Asia/Colombo'; 'Nepal Standard Time' = 'Asia/Kathmandu'
    'Central Asia Standard Time' = 'Asia/Bishkek'; 'Bangladesh Standard Time' = 'Asia/Dhaka'
    'Omsk Standard Time' = 'Asia/Omsk'; 'Myanmar Standard Time' = 'Asia/Yangon'
    'SE Asia Standard Time' = 'Asia/Bangkok'; 'Altai Standard Time' = 'Asia/Barnaul'
    'W. Mongolia Standard Time' = 'Asia/Hovd'; 'North Asia Standard Time' = 'Asia/Krasnoyarsk'
    'N. Central Asia Standard Time' = 'Asia/Novosibirsk'; 'Tomsk Standard Time' = 'Asia/Tomsk'
    'China Standard Time' = 'Asia/Shanghai'; 'North Asia East Standard Time' = 'Asia/Irkutsk'
    'Singapore Standard Time' = 'Asia/Singapore'; 'W. Australia Standard Time' = 'Australia/Perth'
    'Taipei Standard Time' = 'Asia/Taipei'; 'Ulaanbaatar Standard Time' = 'Asia/Ulaanbaatar'
    'Aus Central W. Standard Time' = 'Australia/Eucla'; 'Transbaikal Standard Time' = 'Asia/Chita'
    'Tokyo Standard Time' = 'Asia/Tokyo'; 'North Korea Standard Time' = 'Asia/Pyongyang'
    'Korea Standard Time' = 'Asia/Seoul'; 'Yakutsk Standard Time' = 'Asia/Yakutsk'
    'Cen. Australia Standard Time' = 'Australia/Adelaide'; 'AUS Central Standard Time' = 'Australia/Darwin'
    'E. Australia Standard Time' = 'Australia/Brisbane'; 'AUS Eastern Standard Time' = 'Australia/Sydney'
    'West Pacific Standard Time' = 'Pacific/Port_Moresby'; 'Tasmania Standard Time' = 'Australia/Hobart'
    'Vladivostok Standard Time' = 'Asia/Vladivostok'; 'Lord Howe Standard Time' = 'Australia/Lord_Howe'
    'Bougainville Standard Time' = 'Pacific/Bougainville'; 'Russia Time Zone 10' = 'Asia/Srednekolymsk'
    'Magadan Standard Time' = 'Asia/Magadan'; 'Norfolk Standard Time' = 'Pacific/Norfolk'
    'Sakhalin Standard Time' = 'Asia/Sakhalin'; 'Central Pacific Standard Time' = 'Pacific/Guadalcanal'
    'Russia Time Zone 11' = 'Asia/Kamchatka'; 'New Zealand Standard Time' = 'Pacific/Auckland'
    'UTC+12' = 'Etc/GMT-12'; 'Fiji Standard Time' = 'Pacific/Fiji'
    'Chatham Islands Standard Time' = 'Pacific/Chatham'; 'UTC+13' = 'Etc/GMT-13'
    'Tonga Standard Time' = 'Pacific/Tongatapu'; 'Samoa Standard Time' = 'Pacific/Apia'
    'Line Islands Standard Time' = 'Pacific/Kiritimati'
}

# Best-effort IANA timezone of this host, used as the default for the TZ
# prompt. Containers never inherit the host's zone on their own, so whatever
# the operator confirms here must be written to .env explicitly.
function Detect-Timezone {
    if ($IsLinux -or $IsMacOS) {
        try {
            if (Get-Command timedatectl -ErrorAction SilentlyContinue) {
                $tz = timedatectl show -p Timezone --value 2>$null
                if ($tz) { return "$tz".Trim() }
            }
            if (Test-Path '/etc/timezone') {
                $tz = (Get-Content '/etc/timezone' -TotalCount 1).Trim()
                if ($tz) { return $tz }
            }
            $link = Get-Item '/etc/localtime' -ErrorAction SilentlyContinue
            if ($link -and "$($link.Target)" -match 'zoneinfo/(.+)$') { return $Matches[1] }
        } catch {}
        return 'UTC'
    }
    $winId = (Get-TimeZone).Id
    # PowerShell 7+/.NET 6 converts directly; 5.1 lands in the catch and uses
    # the CLDR table above.
    try {
        $iana = $null
        if ([TimeZoneInfo]::TryConvertWindowsIdToIanaId($winId, [ref]$iana) -and $iana) { return $iana }
    } catch {}
    if ($script:WindowsToIana.ContainsKey($winId)) { return $script:WindowsToIana[$winId] }
    return 'UTC'
}
function Check-Prerequisites {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Fail 'Docker is not installed. Install Docker Desktop, then re-run.'
    }
    # Native commands (docker) write to stderr when the daemon is down. Under
    # $ErrorActionPreference='Stop' that stderr becomes a NativeCommandError
    # that prints an ugly stack trace and aborts before our friendly message.
    # Silence the policy + stderr around the probes and judge by exit code only.
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    docker compose version 2>$null | Out-Null; $composeOk = ($LASTEXITCODE -eq 0)
    docker info 2>$null | Out-Null;            $dockerOk  = ($LASTEXITCODE -eq 0)
    $ErrorActionPreference = $prevEAP
    if (-not $composeOk) { Fail 'Docker Compose v2 is required. Update Docker Desktop and re-run.' }
    if (-not $dockerOk)  { Fail 'Docker is not running. Start Docker Desktop, wait until it is ready, then re-run.' }
    if (-not (Test-Path $BaseCompose)) { Fail "$BaseCompose was not found in $ProjectRoot" }
}

# CRITICAL: read AND write .env with the SAME explicit encoding (UTF-8, no BOM).
# Windows PowerShell 5.1's Get-Content defaults to ANSI (Windows-1252) while we
# write UTF-8 — so any non-ASCII byte in the file (an em-dash in a comment, an
# accented value) is re-decoded wrong and re-encoded larger on every rewrite.
# Across the ~13 Set-EnvValue calls per install that compounds into a multi-MB
# .env of mojibake. Pinning both sides to UTF-8 makes the round trip byte-stable.
$script:Utf8NoBom = New-Object Text.UTF8Encoding($false)
function Read-EnvLines {
    $p = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path $p)) { return @() }
    return [IO.File]::ReadAllLines($p, $script:Utf8NoBom)
}
function Get-EnvValue([string]$Key) {
    $line = Read-EnvLines | Where-Object { $_ -match ('^' + [regex]::Escape($Key) + '=') } | Select-Object -Last 1
    if (-not $line) { return '' }
    $value = ($line -split '=', 2)[1] -replace '\s+#.*$', ''
    return $value.Trim().Trim('"').Trim("'")
}
function Set-EnvValue([string]$Key, [string]$Value) {
    $lines = [Collections.Generic.List[string]](Read-EnvLines)
    $pattern = '^' + [regex]::Escape($Key) + '='
    $output = [Collections.Generic.List[string]]::new(); $written = $false
    foreach ($line in $lines) {
        if ($line -match $pattern) {
            if (-not $written) { $output.Add("$Key=$Value"); $written = $true }
        } else { $output.Add($line) }
    }
    if (-not $written) { $output.Add(''); $output.Add("$Key=$Value") }
    [IO.File]::WriteAllLines((Join-Path $ProjectRoot '.env'), $output, $script:Utf8NoBom)
}
function Test-MissingOrPlaceholder([string]$Value) {
    return [string]::IsNullOrWhiteSpace($Value) -or $Value -match '^(dev_|insecure_|change_me|your_|changeme|placeholder|dummy|CKLghtP4rWz8J9vN2xQ5mT7yU8kF6bD3eH1aG4cS0wE=)'
}
function New-RandomBytes([int]$Count) { $b = New-Object byte[] $Count; $rng = [Security.Cryptography.RandomNumberGenerator]::Create(); try { $rng.GetBytes($b) } finally { $rng.Dispose() }; return $b }
function New-Hex([int]$Bytes) { return ((New-RandomBytes $Bytes) | ForEach-Object { $_.ToString('x2') }) -join '' }
function New-Password { return [Convert]::ToBase64String((New-RandomBytes 36)).Replace('+','').Replace('/','').Replace('=','').Substring(0,32) }
function New-FernetKey { return [Convert]::ToBase64String((New-RandomBytes 32)).Replace('+','-').Replace('/','_') }
function Ensure-PlainValue([string]$Key, [string]$Label, [string]$Default) {
    $current = Get-EnvValue $Key
    if (-not [string]::IsNullOrWhiteSpace($current)) {
        # Fresh install: keep existing, don't nag. Reconfigure: offer current as default.
        if ($script:Mode -ne 'reconfigure') { return }
        $Default = $current
    }
    Set-EnvValue $Key (Ask-Value $Label $Default)
}
function Ensure-SecretValue([string]$Key, [string]$Label, [string]$Generated) {
    $current = Get-EnvValue $Key
    if (-not (Test-MissingOrPlaceholder $current)) { Ok "$Label already configured"; return }
    if (Ask-YesNo "$Label is missing or insecure. Use a newly generated value?" $true) { $value = $Generated }
    else { $value = Ask-Secret "Enter $Label"; if ([string]::IsNullOrWhiteSpace($value)) { Fail "$Label cannot be empty" } }
    Set-EnvValue $Key $value; Ok "$Label configured"
}
function Prepare-Environment {
    if (-not (Test-Path '.env')) {
        if (-not (Test-Path '.env.example')) { Fail '.env.example is missing' }
        Copy-Item '.env.example' '.env'; Ok 'Created .env from .env.example'
    } else { Ok 'Using existing .env; secrets are preserved, and you can update values below' }

    # Secrets — generated automatically; prompted only if still a placeholder.
    Ensure-SecretValue POSTGRES_PASSWORD 'PostgreSQL password' (New-Password)
    Ensure-SecretValue SECRET_KEY 'JWT signing key' (New-Hex 32)
    Ensure-SecretValue CREDENTIAL_ENCRYPTION_KEY 'credential encryption key' (New-FernetKey)
    Ensure-SecretValue INTERNAL_API_KEY 'internal API key' (New-Password)
    Ensure-SecretValue MEDIAMTX_SECRET 'MediaMTX webhook secret' (New-Hex 32)

    # Rarely-changed identifiers — filled only if missing.
    Ensure-PlainValue POSTGRES_USER 'PostgreSQL user' 'opennvr_user'
    Ensure-PlainValue POSTGRES_DB 'PostgreSQL database' 'opennvr_db'

    # Curated, explained settings. Enter keeps the [default]; all local.
    Write-Host ''
    Write-Host '  -- Basic settings -------------------------------------'
    Configure-Value DEFAULT_ADMIN_USERNAME 'Administrator username' 'admin' `
        'Login name for the first OpenNVR admin account.' 'yes' `
        'You pick this yourself - no external account involved.'
    Configure-Value DEFAULT_ADMIN_EMAIL 'Administrator email' 'admin@opennvr.local' `
        'Contact email tied to the admin account.' 'yes' `
        'Any address works; the placeholder is fine for an offline setup.'
    Configure-Value RECORDINGS_PATH 'Recordings folder on this machine' $script:DefaultRecordings `
        'Host directory where recorded video segments are written.' 'yes' `
        'Created automatically if it does not exist yet.'
    Configure-Value TZ 'Timezone (IANA name)' (Detect-Timezone) `
        'Timezone used to name recording folders and align the playback timeline.' 'yes' `
        'Auto-detected from this machine; the recorder and backend containers both use it.'
    $tzValue = Get-EnvValue TZ
    if ($tzValue -ne 'UTC' -and $tzValue -notmatch '/') {
        Warn "'$tzValue' does not look like an IANA timezone (e.g. Asia/Kolkata); the containers will fall back to UTC if it is invalid"
    }

    $recordings = Get-EnvValue RECORDINGS_PATH
    if (-not (Test-Path $recordings)) { try { New-Item -ItemType Directory -Force -Path $recordings | Out-Null } catch { Warn 'Could not create recordings directory; Docker will try' } }
}

function Find-ExampleCompose([string]$Name) {
    $candidates = @("docker-compose.$Name.yml", "docker-compose.$Name.yaml", "examples/$Name/docker-compose.yml", "examples/$Name/docker-compose.yaml", "examples/$Name/compose.yml", "examples/$Name/compose.yaml")
    foreach ($candidate in $candidates) { if (Test-Path $candidate) { return $candidate } }
    return $null
}
function Prompt-OverlayDefaults([string]$File) {
    $text = [IO.File]::ReadAllText((Resolve-Path $File))
    $seen = @{}
    foreach ($match in [regex]::Matches($text, '\$\{([A-Z][A-Z0-9_]*):-([^}]+)\}')) {
        $key = $match.Groups[1].Value; $default = $match.Groups[2].Value
        if ($seen[$key]) { continue }; $seen[$key] = $true
        if ([string]::IsNullOrWhiteSpace((Get-EnvValue $key))) { Set-EnvValue $key (Ask-Value $key $default) }
    }
}
# Catalog-driven model menu — renders examples/camera-agent/model_catalog.txt
# (kind|model|min_ram_gb|tested|speed|summary) annotated for the detected
# hardware, suggestion preselected; returns the chosen model name. Falls
# back to a plain prompt when the catalog is missing.
function Pick-ModelFromCatalog([string]$Kind, [string]$Suggest, [string]$Label, [int]$RamGb) {
    $catalog = 'examples/camera-agent/model_catalog.txt'
    if (-not (Test-Path $catalog)) { return (Ask-Value $Label $Suggest) }
    $rows = @(Get-Content $catalog | Where-Object { $_ -and -not $_.StartsWith('#') } |
        ForEach-Object { $f = $_ -split '\|'; if ($f[0] -eq $Kind) { $f } })
    if ($rows.Count -eq 0) { return (Ask-Value $Label $Suggest) }
    Write-Host ''
    Write-Host "  $Label - pick a number, or type any Ollama model name:"
    $defaultIdx = ''
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $f = $rows[$i]
        $mark = ''
        if ($f[1] -eq $Suggest) { $mark = '  <- suggested'; $defaultIdx = [string]($i + 1) }
        $fit = ''
        if ($RamGb -gt 0 -and [int]$f[2] -gt $RamGb) { $fit = "  [needs ~$($f[2]) GB - detected $RamGb GB]" }
        $tested = if ($f[3] -eq 'yes') { 'tested' } else { 'untested' }
        Write-Host ('   {0}. {1,-16} ~{2}GB  {3,-8} {4,-8} {5}{6}{7}' -f ($i + 1), $f[1], $f[2], $f[4], $tested, $f[5], $fit, $mark)
    }
    $answer = Read-Host "  $Label [$(if ($defaultIdx) { $defaultIdx } else { $Suggest })]"
    if ([string]::IsNullOrWhiteSpace($answer)) { $answer = if ($defaultIdx) { $defaultIdx } else { $Suggest } }
    $n = 0
    if ([int]::TryParse($answer, [ref]$n) -and $n -ge 1 -and $n -le $rows.Count) { return $rows[$n - 1][1] }
    return $answer
}

function Choose-Example {
    $script:ExampleName = ''; $script:ExampleCompose = ''; $script:ExampleProfile = ''
    Set-EnvValue OPENNVR_EXAMPLE ''; Set-EnvValue OPENNVR_EXAMPLE_COMPOSE ''; Set-EnvValue OPENNVR_EXAMPLE_PROFILE ''
    Write-Host ''
    Write-Host '  -- Example app ----------------------------------------'
    Info 'Examples add an AI app on top of the core NVR. The Camera Agent lets you'
    Info 'ask your cameras questions out loud or by chat. Everything runs locally.'
    if (-not (Ask-YesNo 'Set up an example app now?' $false)) { return }
    $examples = @(Get-ChildItem 'examples' -Directory | Sort-Object Name)
    if ($examples.Count -eq 0) { Warn 'No examples were found'; return }
    Write-Host ''; Info 'Available examples:'
    for ($i=0; $i -lt $examples.Count; $i++) {
        $manifest = Find-ExampleCompose $examples[$i].Name
        $status = if ($manifest) { "installable: $manifest" } else { 'no Compose manifest' }
        Write-Host ('  {0,2}. {1,-30} [{2}]' -f ($i+1), $examples[$i].Name, $status)
    }
    Write-Host '   0. Core stack only'; Write-Host ''
    $choiceRaw = Read-Host '  Select an example [0]'; if ([string]::IsNullOrWhiteSpace($choiceRaw)) { $choiceRaw = '0' }
    $choice = 0; if (-not [int]::TryParse($choiceRaw, [ref]$choice)) { Fail 'Invalid selection' }
    if ($choice -eq 0) { return }
    if ($choice -lt 1 -or $choice -gt $examples.Count) { Fail 'Selection out of range' }
    $name = $examples[$choice-1].Name; $manifest = Find-ExampleCompose $name
    if (-not $manifest) { Fail "The '$name' example has no Docker Compose manifest" }
    # $prof, not $profile — $PROFILE is an automatic PowerShell variable.
    $prof = $name
    if ($name -eq 'camera-agent') {
        Write-Host ''
        Explain 'Camera Agent runs in VOICE mode (speak, hear spoken answers) or CHAT mode (type, read answers). Voice adds Whisper speech-to-text and Piper text-to-speech; chat is lighter.' 'pick one' '1 (voice)'
        $mode = Ask-Value 'Camera Agent mode: 1=voice, 2=chat' '1'
        # $prof, not $profile - $PROFILE is an automatic PowerShell variable.
        $prof = if ($mode -eq '2') { 'camera-agent-chat' } else { 'camera-agent' }

        Write-Host ''
        # LLM runtime FIRST: where the LLM runs decides which hardware the
        # model suggestion below is sized for (host GPU/RAM vs the Docker
        # VM's CPU-only allowance). Windows/macOS default to the host
        # (container VMs have no GPU access); Linux keeps the bundled
        # container. A host Ollama already on :11434 flips the default.
        $hostOllama = $false
        try {
            $null = Invoke-WebRequest -Uri 'http://localhost:11434/api/version' -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            $hostOllama = $true
        } catch {}
        $llmDefault = if ($script:Platform -eq 'Linux' -and -not $hostOllama) { '1' } else { '2' }
        Explain 'Where should the LLM run? In Docker on Windows/macOS the container CANNOT use the GPU - answers take minutes of pure CPU. Ollama running ON this machine uses the real GPU and skips a 3.2 GB image. On a Linux server the bundled container is fine.' 'pick one' $llmDefault
        if ($hostOllama) { Ok 'Found Ollama already running on this machine (:11434)' }
        $llmMode = Ask-Value 'LLM runtime: 1=bundled container, 2=Ollama on this machine / external URL' $llmDefault
        $llmWhere = 'container'
        $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
        if ($llmMode -eq '2') {
            $llmWhere = 'host'
            Configure-Value OLLAMA_EXTERNAL_URL 'External LLM endpoint' 'http://host.docker.internal:11434' `
                'Ollama-compatible endpoint the agent calls for the LLM. host.docker.internal reaches this machine from inside Docker.' 'yes' `
                'Native Ollama: http://host.docker.internal:11434 | LAN box: http://<ip>:11434'
            if (-not $hostOllama) {
                if ($ollamaCmd) {
                    Warn 'Ollama is installed but not answering on :11434 - start it (launch the Ollama app).'
                } elseif (Get-Command winget -ErrorAction SilentlyContinue) {
                    if (Ask-YesNo 'Ollama is not installed. Install it now with winget?' $true) {
                        winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
                        if ($LASTEXITCODE -eq 0) { Ok 'Ollama installed - launch it once so it starts serving.' }
                        else { Warn 'Install failed - get it from https://ollama.com/download' }
                        $ollamaCmd = Get-Command ollama -ErrorAction SilentlyContinue
                    } else {
                        Warn 'Install Ollama before first use: https://ollama.com/download'
                    }
                } else {
                    Warn 'Install Ollama on this machine first: https://ollama.com/download'
                }
            }
            Info 'The bundled ollama container will be skipped entirely.'
        } else {
            Set-EnvValue OLLAMA_EXTERNAL_URL ''
        }

        # Hardware-aware suggestion, sized for where the LLM will run.
        # Tiers mirror examples/camera-agent/MODELS_AND_LATENCY.md (and
        # suggest_llm_model in install.sh) - keep the three in sync.
        $ramGb = 0; $cores = [int]($env:NUMBER_OF_PROCESSORS); $accel = 'cpu'
        try { $ramGb = [int]([math]::Floor((Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory / 1GB)) } catch {}
        if ($llmWhere -eq 'host') {
            if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
                try { & nvidia-smi -L *> $null; if ($LASTEXITCODE -eq 0) { $accel = 'cuda' } } catch {}
            }
        }
        # Ceiling = the tested envelope: never suggest beyond qwen2.5:3b
        # (the largest model this agent is exercised with); bigger-is-
        # runnable is detectable, bigger-is-better is not. VLM default is
        # ALWAYS moondream — an untested model through the new ollamavlm
        # adapter would stack unknowns; headroom is advertised in the
        # prompt note instead.
        $llmSuggest = if ($accel -ne 'cpu') {
            if ($ramGb -ge 16) { 'qwen2.5:3b' } else { 'qwen2.5:1.5b' }
        } else {
            if ($ramGb -ge 16 -and $cores -ge 8) { 'qwen2.5:1.5b' } else { 'qwen2.5:0.5b' }
        }
        $vlmSuggest = 'moondream'
        $hwDesc = "$(if ($accel -eq 'cuda') { 'NVIDIA GPU (CUDA), ' } else { 'CPU only, ' })$ramGb GB RAM, $cores cores"
        Ok "Detected: $hwDesc -> suggesting $llmSuggest"

        Write-Host ''
        Write-Host '  -- Camera Agent models (all local, no API keys) -------'
        Explain "The local chat model that answers your questions; must support tool calling. The suggestion is sized for this machine ($hwDesc), capped at the largest model this agent is tested with - 'untested' entries are known-good models nobody has validated with THIS agent yet." 'yes' $llmSuggest
        Set-EnvValue OLLAMA_MODEL (Pick-ModelFromCatalog 'llm' $llmSuggest 'Local LLM model (Ollama)' $ramGb)
        # Pull offer AFTER the model choice, so it pulls what was chosen.
        if ($llmWhere -eq 'host') {
            $extModel = Get-EnvValue OLLAMA_MODEL; if (-not $extModel) { $extModel = $llmSuggest }
            if ($ollamaCmd) {
                $have = (& ollama list 2>$null | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] }) -contains $extModel
                if ($have) {
                    Ok "Model $extModel is already available on this machine."
                } elseif (Ask-YesNo "Pull the model now (ollama pull $extModel)?" $true) {
                    & ollama pull $extModel
                    if ($LASTEXITCODE -ne 0) { Warn "Pull failed - run 'ollama pull $extModel' manually before first use." }
                } else {
                    Warn "Before first use:  ollama pull $extModel"
                }
            } else {
                Warn "Before first use, pull the model ON THE HOST:  ollama pull $extModel"
            }
        }
        if ($prof -eq 'camera-agent') {
            Configure-Value WHISPER_MODEL_SIZE 'Whisper speech-to-text model' 'base.en' `
                'Transcribes your spoken questions (voice mode only).' 'yes' `
                'tiny.en (fastest) | base.en (default) | small.en (most accurate).'
        }
        Configure-Value CAPTION_ADAPTER 'Scene-description model' 'moondream' `
            'Describes what a camera sees. moondream answers questions (VQA); blip writes plain captions; ollamavlm proxies to your Ollama (GPU-fast when the LLM runs on this machine - needs an adapter tag newer than 0.1.3).' 'yes' `
            'moondream | blip | ollamavlm - all local.'
        if ((Get-EnvValue CAPTION_ADAPTER) -eq 'ollamavlm') {
            Explain 'Multimodal Ollama model the ollamavlm adapter uses for scene questions; the adapter auto-pulls it. moondream is the tested default.' 'yes' $vlmSuggest
            Set-EnvValue OLLAMA_VLM_MODEL (Pick-ModelFromCatalog 'vlm' $vlmSuggest 'Vision model (Ollama)' $ramGb)
        }
    } else {
        Prompt-OverlayDefaults $manifest
    }
    $script:ExampleName=$name; $script:ExampleCompose=$manifest; $script:ExampleProfile=$prof
    Set-EnvValue OPENNVR_EXAMPLE $name; Set-EnvValue OPENNVR_EXAMPLE_COMPOSE $manifest; Set-EnvValue OPENNVR_EXAMPLE_PROFILE $prof
    Ok "Selected $name ($prof)"
    if ($name -eq 'camera-agent') {
        Info 'The local LLM model downloads on first start - usually the slowest step.'
    }
}
# Docker VM allowance check — Windows twin of check_docker_vm_allowance in
# install.sh. On Docker Desktop the WSL2/Hyper-V VM's CPU/RAM allowance is a
# Docker Desktop / .wslconfig setting we cannot change from here; detect an
# undersized allowance for what was selected and say exactly what to change.
function Check-DockerVmAllowance {
    $vmMemGb = 0; $vmCpus = 0
    try {
        $vmMemGb = [int]([math]::Floor([long](docker info --format '{{.MemTotal}}' 2>$null) / 1GB))
        $vmCpus  = [int](docker info --format '{{.NCPU}}' 2>$null)
    } catch {}
    if ($vmMemGb -le 0 -or $vmCpus -le 0) { return }
    $hostMemGb = 0
    try { $hostMemGb = [int]([math]::Floor((Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).TotalPhysicalMemory / 1GB)) } catch {}
    $needMem = 6
    if ($script:ExampleName -and -not (Get-EnvValue OLLAMA_EXTERNAL_URL)) { $needMem = 8 }
    Info "Docker VM allowance: $vmCpus CPUs / $vmMemGb GB (this machine has $hostMemGb GB)."
    if ($vmMemGb -lt $needMem -or $vmCpus -lt 4) {
        Warn "That is on the small side for what you selected (recommended: >=4 CPUs, >=$needMem GB)."
        Warn 'OpenNVR cannot change this itself - raise it in Docker Desktop:'
        Warn '  Settings -> Resources (WSL2 backend: edit %UserProfile%\.wslconfig,'
        Warn '  e.g. [wsl2] / memory=8GB / processors=4, then wsl --shutdown),'
        Warn 'then re-run .\start.ps1 up. detect-pipeline and the vision model'
        Warn 'are the main consumers of this shared allowance.'
    }
}

function Pull-AndBuild {
    Write-Host ''
    Info 'First-time setup downloads several container images (and, for the'
    Info 'Camera Agent, a local LLM model of ~1 GB). Depending on your network'
    Info 'this can take 8-15 minutes. Later starts are much faster - everything'
    Info 'is cached, so you only pay this cost once.'
    Check-ImageArchitectures
    Write-Host ''
    Info 'Pulling the OpenNVR core stack...'
    docker compose -f $BaseCompose pull --ignore-buildable
    if ($LASTEXITCODE -ne 0) { Fail 'Failed to pull the core stack' }
    Choose-Example
    Check-DockerVmAllowance
    $script:ComposeArgs = @('-f', $BaseCompose)
    if ($script:ExampleCompose) {
        $script:ComposeArgs += @('-f', $script:ExampleCompose, '--profile', $script:ExampleProfile)
        Info "Pulling images for $script:ExampleName..."
        docker compose @script:ComposeArgs pull --ignore-buildable
        if ($LASTEXITCODE -ne 0) { Fail "Failed to pull $script:ExampleName" }
    }
    Info 'Building services that do not publish a pre-built image...'
    docker compose @script:ComposeArgs build
    if ($LASTEXITCODE -ne 0) { Fail 'Docker build failed' }
}

Show-Logo
Write-Host '  OpenNVR interactive installer'; Write-Host ''
Detect-Platform
Check-Prerequisites
Prepare-Environment
Pull-AndBuild
Write-Host ''; Info 'Configuration and images are ready. Starting OpenNVR...'; Write-Host ''
if ($script:Platform -eq 'Windows') { & (Join-Path $ProjectRoot 'start.ps1') up; exit $LASTEXITCODE }
& bash (Join-Path $ProjectRoot 'start.sh') up
exit $LASTEXITCODE