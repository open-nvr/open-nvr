# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The OpenNVR host agent on Windows: .\start.ps1 host-agent install|uninstall|status|logs
# Dot-sourced by start.ps1 (which provides Write-Color and Get-EnvVar).
#
# The agent (host-agent/, docs/HOST_AGENT.md) streams USB webcams into
# their OpenNVR cameras and reports the machine's own CPU, memory, disks.
# It opens no port: it calls core on 127.0.0.1 with its own key.
#
#   bin\opennvr-host.exe            the agent, copied out of the core image
#   bin\opennvr-host.build          the build it is (compared on every `up`)
#   data\host-agent-key\agent.key   its key (this user only; never mounted)
#   data\host-agent\agent.key.sha256  the key's hash, mounted read-only into core
#   Scheduled Task "OpenNVR host agent", at logon, in this user's session:
#   a SYSTEM or S4U task is denied the camera by Windows' camera privacy.
#
# Written for Windows PowerShell 5.1 (no ternary, no ??, no &&).

$HaTask    = "OpenNVR host agent"
$HaBinDir  = "bin"
$HaExe     = "bin\opennvr-host.exe"
$HaBuild   = "bin\opennvr-host.build"
$HaKeyDir  = "data\host-agent-key"
$HaKeyFile = "data\host-agent-key\agent.key"
$HaHashDir = "data\host-agent"
$HaHash    = "data\host-agent\agent.key.sha256"
$HaLog     = "data\host-agent-key\agent.log"
$HaAscii   = New-Object Text.UTF8Encoding($false)

function Write-HaSay($t)  { Write-Host "  $t" }
function Write-HaOk($t)   { Write-Host "  OK  $t" -ForegroundColor Green }
function Write-HaWarn($t) { Write-Host "  !   $t" -ForegroundColor Yellow }

function Get-HaPlatform {
    $arch = "amd64"
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { $arch = "arm64" }
    return "windows-$arch"
}

# Set KEY=value in .env, keeping every other line as it is.
function Set-HaEnvFlag([string]$Value) {
    if (-not (Test-Path ".env")) { return }
    # UTF-8 both ways: 5.1's Get-Content would mis-decode what the installer wrote.
    $lines = @([IO.File]::ReadAllLines((Resolve-Path ".env"), $HaAscii))
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^HOST_AGENT=') { $lines[$i] = "HOST_AGENT=$Value"; $found = $true }
    }
    if (-not $found) { $lines += "HOST_AGENT=$Value" }
    [IO.File]::WriteAllLines((Join-Path (Get-Location) ".env"), [string[]]$lines, $HaAscii)
}

# The key, created once. Only this user may read it (the inherited ACL of a
# repo folder is often Everyone/Users).
function Initialize-HaKey {
    New-Item -ItemType Directory -Force $HaKeyDir, $HaHashDir | Out-Null
    if (-not (Test-Path $HaKeyFile)) {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        $rng.GetBytes($bytes)
        $rng.Dispose()
        $key = "ohk_" + ([Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_'))
        [IO.File]::WriteAllText((Join-Path (Get-Location) $HaKeyFile), $key + "`n", $HaAscii)
        & icacls $HaKeyDir /inheritance:r /grant:r "${env:USERNAME}:(OI)(CI)F" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
        Write-HaOk "Created the agent key."
    }
    $key = (Get-Content $HaKeyFile -Raw).Trim()
    $sha = [Security.Cryptography.SHA256]::Create()
    $hex = -join ($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($key)) | ForEach-Object { $_.ToString("x2") })
    $sha.Dispose()
    $current = ""
    if (Test-Path $HaHash) { $current = (Get-Content $HaHash -Raw).Trim() }
    if ($current -ne $hex) {
        [IO.File]::WriteAllText((Join-Path (Get-Location) $HaHash), $hex + "`n", $HaAscii)
    }
}

# Copy the agent for this machine out of the running core container. Returns
# $true when a different build was installed. The running exe cannot be
# overwritten on Windows, but it can be renamed: old -> .old.exe, new in place.
function Install-HaBinary {
    $plat = Get-HaPlatform
    $state = docker inspect -f "{{.State.Running}}" opennvr_core 2>$null
    if ($state -ne "true") {
        Write-HaWarn "OpenNVR is not running, so the agent cannot be copied out of it. Run .\start.ps1 up first."
        return $false
    }
    New-Item -ItemType Directory -Force $HaBinDir | Out-Null
    $tmp = Join-Path $HaBinDir ".incoming"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    New-Item -ItemType Directory -Force $tmp | Out-Null
    docker cp "opennvr_core:/opt/opennvr-host/$plat/opennvr-host.exe" "$tmp\opennvr-host.exe" 2>$null | Out-Null
    $okExe = ($LASTEXITCODE -eq 0)
    docker cp "opennvr_core:/opt/opennvr-host/BUILD" "$tmp\BUILD" 2>$null | Out-Null
    if (-not $okExe -or -not (Test-Path "$tmp\opennvr-host.exe")) {
        Remove-Item -Recurse -Force $tmp
        Write-HaWarn "This OpenNVR image has no host agent for $plat (an image from before the agent?)."
        return $false
    }
    $newBuild = ""
    if (Test-Path "$tmp\BUILD") { $newBuild = (Get-Content "$tmp\BUILD" -Raw).Trim() }
    $oldBuild = ""
    if (Test-Path $HaBuild) { $oldBuild = (Get-Content $HaBuild -Raw).Trim() }
    if ((Test-Path $HaExe) -and $newBuild -ne "" -and $newBuild -eq $oldBuild) {
        Remove-Item -Recurse -Force $tmp
        return $false
    }
    $old = "bin\opennvr-host.old.exe"
    if (Test-Path $old) { Remove-Item -Force $old -ErrorAction SilentlyContinue }
    if (Test-Path $HaExe) { Move-Item -Force $HaExe $old }
    Move-Item -Force "$tmp\opennvr-host.exe" $HaExe
    [IO.File]::WriteAllText((Join-Path (Get-Location) $HaBuild), $newBuild + "`n", $HaAscii)
    Remove-Item -Recurse -Force $tmp
    Write-HaOk "Host agent $newBuild installed in bin\."
    return $true
}

# Stop the per-camera tasks the webcam helper script used to install (before
# the agent existed). By task name, never by process name.
function Remove-HaScriptEraTasks {
    foreach ($t in @(Get-ScheduledTask -TaskName "OpenNVR webcam cam-*" -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskName $t.TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $t.TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Write-HaOk "Removed the old webcam task '$($t.TaskName)' (the agent replaces it)."
    }
    $dir = Join-Path $env:LOCALAPPDATA "OpenNVR\webcam"
    if (Test-Path $dir) { Remove-Item -Recurse -Force $dir -ErrorAction SilentlyContinue }
}

function Register-HaTask {
    $here = (Get-Location).Path
    $act = New-ScheduledTaskAction -Execute (Join-Path $here $HaExe) -WorkingDirectory $here
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
    $who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
    Register-ScheduledTask -TaskName $HaTask -Action $act -Trigger $trigger -Settings $settings -Principal $who `
        -Description "OpenNVR host agent ($here): USB webcams and host monitoring. Logs: $here\$HaLog" -Force | Out-Null
}

# Our agent processes: matched by the full path of OUR binary (and its .old
# copy), never by process name, so another OpenNVR folder's agent or anything
# else called opennvr-host is never touched.
function Get-HaProcesses {
    $here = (Get-Location).Path
    $paths = @((Join-Path $here $HaExe), (Join-Path $here "bin\opennvr-host.old.exe"))
    return @(Get-CimInstance Win32_Process -Filter "Name='opennvr-host.exe' OR Name='opennvr-host.old.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and ($paths -contains $_.ExecutablePath) })
}

function Restart-HaTask {
    if (-not (Get-ScheduledTask -TaskName $HaTask -ErrorAction SilentlyContinue)) { return }
    Stop-ScheduledTask -TaskName $HaTask -ErrorAction SilentlyContinue
    # Stop-ScheduledTask returns before the agent is gone (and an agent from an
    # earlier task run is not stopped by it at all). Start the new one only
    # after the old one exited, or the two overlap on the agent lock.
    for ($i = 0; $i -lt 20 -and (Get-HaProcesses).Count -gt 0; $i++) { Start-Sleep -Milliseconds 500 }
    foreach ($p in Get-HaProcesses) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-ScheduledTask -TaskName $HaTask
}

function Test-HaFfmpeg {
    if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { return $true }
    $pk = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if ((Test-Path $pk) -and (Get-ChildItem -Path $pk -Recurse -Filter ffmpeg.exe -ErrorAction SilentlyContinue | Select-Object -First 1)) { return $true }
    return $false
}

function Install-HostAgent {
    Write-Color "  OpenNVR host agent" Cyan
    Initialize-HaKey
    Install-HaBinary | Out-Null
    if (-not (Test-Path $HaExe)) { Write-HaWarn "Not installed: no agent binary."; return }
    Remove-HaScriptEraTasks
    Register-HaTask
    Restart-HaTask
    Set-HaEnvFlag "on"
    Write-HaOk "Running in the background, and at every logon of $env:USERNAME (task '$HaTask')."
    if (-not (Test-HaFfmpeg)) {
        Write-HaWarn "ffmpeg is not installed: USB webcams cannot stream until it is."
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget -and [Environment]::UserInteractive) {
            $ans = Read-Host "  Install ffmpeg now with winget (Gyan.FFmpeg)? [Y/n]"
            if (-not ($ans -and $ans.Trim().ToLower().StartsWith("n"))) {
                & winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
                if ($LASTEXITCODE -eq 0) { Write-HaOk "ffmpeg installed; the agent picks it up within a minute." }
            }
        } else {
            Write-HaSay "Install it with: winget install --id Gyan.FFmpeg -e"
        }
    }
    Write-HaSay "Windows runs it only while you are logged in (camera privacy). On an NVR PC, turn on automatic sign-in."
    Write-HaSay "Logs: $HaLog"
}

# Called by `up`: keep an installed agent current with the core it talks to.
#
# A fresh install (the installer set HOST_AGENT=on, nothing registered yet)
# gets the whole install here; no admin rights are needed for a task that
# runs as this user, so this works from the installer's `up` and from the
# unattended updater alike.
function Update-HostAgent {
    if ((Get-EnvVar "HOST_AGENT") -ne "on") { return }
    if (-not (Get-ScheduledTask -TaskName $HaTask -ErrorAction SilentlyContinue)) {
        Install-HostAgent
        return
    }
    Initialize-HaKey
    if (Install-HaBinary) {
        Restart-HaTask
        Write-HaOk "Host agent restarted on the new build."
    }
}

function Uninstall-HostAgent {
    if (Get-ScheduledTask -TaskName $HaTask -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $HaTask -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $HaTask -Confirm:$false
        Write-HaOk "Removed the task '$HaTask'."
    }
    foreach ($p in @($HaExe, "bin\opennvr-host.old.exe", $HaBuild)) {
        if (Test-Path $p) { Remove-Item -Force $p -ErrorAction SilentlyContinue }
    }
    foreach ($d in @($HaKeyDir, $HaHashDir)) {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue }
    }
    Set-HaEnvFlag "off"
    Write-HaOk "Host agent removed. USB webcam cameras stop streaming; browser webcams are not affected."
}

function Show-HostAgentStatus {
    $t = Get-ScheduledTask -TaskName $HaTask -ErrorAction SilentlyContinue
    if (-not $t) { Write-HaSay "Not installed. Install it with: .\start.ps1 host-agent install"; return }
    $build = "?"
    if (Test-Path $HaBuild) { $build = (Get-Content $HaBuild -Raw).Trim() }
    Write-HaSay "Task '$HaTask': $($t.State); build $build"
    if (Test-HaFfmpeg) { Write-HaSay "ffmpeg: found" } else { Write-HaWarn "ffmpeg: not installed" }
    if (Test-Path $HaLog) {
        Write-HaSay "Last log lines ($HaLog):"
        Get-Content $HaLog -Tail 8 | ForEach-Object { Write-HaSay "  $_" }
    }
}

function Invoke-HostAgent([string]$Action) {
    switch ($Action) {
        "install"   { Install-HostAgent }
        "uninstall" { Uninstall-HostAgent }
        "status"    { Show-HostAgentStatus }
        "logs"      { if (Test-Path $HaLog) { Get-Content $HaLog -Tail 50 -Wait } else { Write-HaSay "No log yet." } }
        default     { Write-Color "Usage: .\start.ps1 host-agent install|uninstall|status|logs" Red; exit 1 }
    }
}
