# ============================================================
# Behavioural tests for start.ps1's published-port resolver (#368).
#
# The PowerShell twin of test_port_resolution.sh. start.ps1 and start.sh
# are hand-mirrored implementations, so testing only the bash side is how
# a Windows-only regression ships unnoticed — and Windows is precisely
# where the WinNAT reserved ranges that motivated this live.
#
# Run on Windows:
#   powershell -NoProfile -ExecutionPolicy Bypass -File tests\host-hardening	est_port_resolution_ps1.ps1
#
# The resolver functions are extracted from start.ps1 via the PowerShell
# AST and re-defined with stubbed probes, so this exercises the real code
# without running the launcher.
# ============================================================
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$src  = Join-Path $repo 'start.ps1'

$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($src, [ref]$tokens, [ref]$errors)

$want = @('Import-PortTable','Show-PortHelp','Resolve-OnePort','Resolve-Ports')
$fns = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true)

$harness = New-Object System.Text.StringBuilder
foreach ($w in $want) {
    $f = $fns | Where-Object { $_.Name -eq $w } | Select-Object -First 1
    if (-not $f) { throw "could not extract $w from start.ps1" }
    [void]$harness.AppendLine($f.Extent.Text)
}

# Stubs: $BLOCKED is the set of ports the "host" refuses to bind.
$prelude = @'
function Write-Color($Text, $Color = "White") { }
function Get-EnvVar { param([string]$Name) return $script:EnvFile[$Name] }
function Test-PortUsable {
    param([int]$Port, [string]$Protocol = 'both', [string]$HostIp = '0.0.0.0', [int]$WaitSeconds = 6)
    return -not ($script:Blocked -contains $Port)
}
$PSScriptRoot_Override = $true
'@

$script:PortTableFile = Join-Path $repo 'scripts\ports.conf'
$code = $prelude + "`n" + $harness.ToString()
# The real file computes $script:PortTableFile from $PSScriptRoot; pin it here.
$code = $code -replace '\$script:PortTableFile = Join-Path \$PSScriptRoot "scripts\\ports\.conf"', ''
Invoke-Expression $code
$script:PortTableFile = Join-Path $repo 'scripts\ports.conf'

$allVars = @('HTTPS_PORT','HTTP_PORT','WEBRTC_ICE_PORT','RTSPS_PORT','CORE_HOST_PORT',
             'LOGS_PORT','AGENT_PORT','HLS_PORT','WEBRTC_HTTP_PORT','PLAYBACK_PORT','MEDIAMTX_API_PORT')

function Reset-Env {
    foreach ($v in $allVars + 'OPENNVR_HTTPS_SUFFIX' + 'OPENNVR_PORT_POLICY') {
        [Environment]::SetEnvironmentVariable($v, $null)
    }
    $script:EnvFile = @{}
    $script:PortPolicyMode = 'auto'
}

$fails = 0
function Check($name, $cond, $detail) {
    if ($cond) { "  PASS  $name" }
    else { "  FAIL  $name`n        $detail"; $script:fails++ }
}

"PowerShell resolver checks"

# 1. Table parses to the same 11 rows bash sees.
Reset-Env; $script:Blocked = @()
$rows = Import-PortTable
Check "Import-PortTable yields 11 rows" ($rows.Count -eq 11) "got $($rows.Count)"

# 2. Clean host -> defaults.
Reset-Env; $script:Blocked = @()
$ok = Resolve-Ports
Check "clean host resolves defaults" ($ok -and $env:AGENT_PORT -eq '9100' -and $env:HTTPS_PORT -eq '443') `
    "ok=$ok AGENT_PORT=$env:AGENT_PORT HTTPS_PORT=$env:HTTPS_PORT"

# 3. The reported bug: WinNAT block 9011-9110.
Reset-Env; $script:Blocked = 9011..9110
$ok = Resolve-Ports
Check "blocked 9100 shifts AGENT_PORT to 19100" ($ok -and $env:AGENT_PORT -eq '19100') `
    "ok=$ok AGENT_PORT=$env:AGENT_PORT"

# 4. Explicit value is never moved.
Reset-Env; $script:Blocked = @(9100); $script:EnvFile = @{ 'AGENT_PORT' = '9100' }
$ok = Resolve-Ports
Check "explicit blocked AGENT_PORT fails" (-not $ok) "ok=$ok AGENT_PORT=$env:AGENT_PORT"

# 5. pin row never relocates.
Reset-Env; $script:Blocked = @(443)
$ok = Resolve-Ports
Check "blocked 443 fails rather than moving" (-not $ok) "ok=$ok HTTPS_PORT=$env:HTTPS_PORT"

# 6. Privileged explicit port accepted (no 1024 floor).
Reset-Env; $script:Blocked = @(); $script:EnvFile = @{ 'HTTPS_PORT' = '443' }
$ok = Resolve-Ports
Check "explicit HTTPS_PORT=443 accepted" ($ok -and $env:HTTPS_PORT -eq '443') "ok=$ok"

# 7. Moved HTTPS_PORT yields the redirect suffix.
Reset-Env; $script:Blocked = @(); $script:EnvFile = @{ 'HTTPS_PORT' = '8443' }
$ok = Resolve-Ports
Check "HTTPS_PORT=8443 sets suffix" ($env:OPENNVR_HTTPS_SUFFIX -eq ':8443') `
    "suffix='$env:OPENNVR_HTTPS_SUFFIX'"

# 8. strict mode refuses to shift.
Reset-Env; $script:Blocked = 9011..9110; $script:EnvFile = @{ 'OPENNVR_PORT_POLICY' = 'strict' }
$ok = Resolve-Ports
Check "strict mode fails instead of shifting" (-not $ok) "ok=$ok AGENT_PORT=$env:AGENT_PORT"

# 9. auto mode still shifts (control for 8).
Reset-Env; $script:Blocked = 9011..9110; $script:EnvFile = @{ 'OPENNVR_PORT_POLICY' = 'auto' }
$ok = Resolve-Ports
Check "auto mode still shifts" ($ok -and $env:AGENT_PORT -eq '19100') "ok=$ok AGENT_PORT=$env:AGENT_PORT"

# 10. Invalid policy rejected.
Reset-Env; $script:Blocked = @(); $script:EnvFile = @{ 'OPENNVR_PORT_POLICY' = 'sometimes' }
$ok = Resolve-Ports
Check "invalid policy rejected" (-not $ok) "ok=$ok"

""
if ($fails -eq 0) { "All PowerShell resolver checks passed"; exit 0 }
else { "$fails PowerShell resolver checks FAILED"; exit 1 }
