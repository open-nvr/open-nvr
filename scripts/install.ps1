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

function Detect-Platform {
    if ($IsLinux) { $script:Platform = 'Linux'; $script:DefaultRecordings = '/var/lib/opennvr/recordings' }
    elseif ($IsMacOS) { $script:Platform = 'macOS'; $script:DefaultRecordings = '/Users/Shared/opennvr-recordings' }
    else { $script:Platform = 'Windows'; $script:DefaultRecordings = 'C:/opennvr/recordings' }
    Ok "Detected $script:Platform (Docker bridge mode)"
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
        $prof = if ($mode -eq '2') { 'camera-agent-chat' } else { 'camera-agent' }

        Write-Host ''
        Write-Host '  -- Camera Agent models (all local, no API keys) -------'
        Configure-Value OLLAMA_MODEL 'Local LLM model (Ollama)' 'qwen2.5:1.5b' `
            'The local chat model that answers your questions; must support tool calling.' 'yes' `
            'Pulled automatically. qwen2.5:0.5b (low RAM) | 1.5b (default) | 3b (better, slower).'
        if ($prof -eq 'camera-agent') {
            Configure-Value WHISPER_MODEL_SIZE 'Whisper speech-to-text model' 'base.en' `
                'Transcribes your spoken questions (voice mode only).' 'yes' `
                'tiny.en (fastest) | base.en (default) | small.en (most accurate).'
        }
        Configure-Value CAPTION_ADAPTER 'Scene-description model' 'moondream' `
            'Describes what a camera sees. moondream answers questions (VQA); blip writes plain captions.' 'yes' `
            'moondream | blip - both run locally.'
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
function Pull-AndBuild {
    Write-Host ''
    Info 'First-time setup downloads several container images (and, for the'
    Info 'Camera Agent, a local LLM model of ~1 GB). Depending on your network'
    Info 'this can take 8-15 minutes. Later starts are much faster - everything'
    Info 'is cached, so you only pay this cost once.'
    Write-Host ''
    Info 'Pulling the OpenNVR core stack...'
    docker compose -f $BaseCompose pull --ignore-buildable
    if ($LASTEXITCODE -ne 0) { Fail 'Failed to pull the core stack' }
    Choose-Example
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