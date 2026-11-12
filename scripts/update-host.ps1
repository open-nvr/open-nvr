# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The host side of the OpenNVR updater on Windows: `.\start.ps1 update`.
# Same steps, files and rules as scripts/update-host.sh (read that header
# for the whole story); the decisions live in server/updater and run inside
# a core image. Dot-sourced by start.ps1, which provides Get-ComposeArgs,
# Get-EnvVar and Write-Color.
#
#   .\start.ps1 update [--to 0.1.9] [--bundle F.tar] [--dry-run] [--yes]
#                      [--adopt] [--accept-manual] [--channel edge] [--restore DIR]
#   .\start.ps1 update --list-images   this site's images (for make-offline-bundle.py)
#
# Written for Windows PowerShell 5.1: no &&, no ternary, and every native
# command is checked through $LASTEXITCODE. `up` runs in a child PowerShell
# so that start.ps1's own exit-on-failure cannot skip the rollback.

$UpdDir       = "data\updates"
$UpdBackups   = "backups"
$UpdKeep      = if ($env:OPENNVR_UPDATE_KEEP_BACKUPS) { [int]$env:OPENNVR_UPDATE_KEEP_BACKUPS } else { 3 }
$UpdVolumes   = @("opennvr_jwt_keys", "opennvr_kai_c_state")
$Utf8NoBom    = New-Object Text.UTF8Encoding($false)

function Write-UpdSay($t)  { Write-Host "  $t" }
function Write-UpdOk($t)   { Write-Host "  ✓ $t" -ForegroundColor Green }
function Write-UpdWarn($t) { Write-Host "  ! $t" -ForegroundColor Yellow }
function Write-UpdFail($t) { Write-Host "  ✗ $t" -ForegroundColor Red }

function Get-UpdKV([string]$File, [string]$Key, [switch]$Last) {
    if (-not (Test-Path $File)) { return $null }
    $hits = @([IO.File]::ReadAllLines((Resolve-Path $File), $Utf8NoBom) | Where-Object { $_ -match "^$Key=" })
    if ($hits.Count -eq 0) { return $null }
    $line = if ($Last) { $hits[-1] } else { $hits[0] }
    return ($line -split '=', 2)[1]
}

# A run is over: its last step, and one line in history.log for the app.
function Complete-UpdRun([string]$Result) {
    $finished = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Add-UpdState @("STEP=$Result", "FINISHED=$finished")
    $line = "$finished $(Get-UpdKV "$UpdDir\state.env" FROM) $(Get-UpdKV "$UpdDir\state.env" TO) $Result`n"
    [IO.File]::AppendAllText((Join-Path (Get-Location).Path "$UpdDir\history.log"), $line, $Utf8NoBom)
}
function Add-UpdState([string[]]$Pairs) {
    $f = Join-Path $UpdDir "state.env"
    [IO.File]::AppendAllText((Join-Path (Get-Location).Path $f), (($Pairs -join "`n") + "`n"), $Utf8NoBom)
}

# start.sh's write_env_var: update KEY=, else uncomment #KEY=, else append.
# Keeps the file's own line endings.
function Set-DotEnvVar([string]$Key, [string]$Value) {
    $path = Join-Path (Get-Location).Path ".env"
    $text = [IO.File]::ReadAllText($path, $Utf8NoBom)
    $nl = if ($text.Contains("`r`n")) { "`r`n" } else { "`n" }
    $lines = [System.Collections.Generic.List[string]]($text -split "\r?\n")
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match "^$Key=") { $lines[$i] = "$Key=$Value"; $done = $true; break }
    }
    if (-not $done) {
        for ($i = 0; $i -lt $lines.Count; $i++) {
            if ($lines[$i] -match "^#\s*$Key=") { $lines[$i] = "$Key=$Value"; $done = $true; break }
        }
    }
    if (-not $done) {
        if ($lines.Count -gt 0 -and $lines[$lines.Count - 1] -eq "") { $lines.Insert($lines.Count - 1, "$Key=$Value") }
        else { $lines.Add("$Key=$Value") }
    }
    [IO.File]::WriteAllText($path, ($lines -join $nl), $Utf8NoBom)
}

function Get-UpdCid([string]$Service) {
    $id = docker compose @script:UpdCa ps -q $Service 2>$null | Select-Object -First 1
    return $id
}

function Get-UpdProject {
    $cid = Get-UpdCid "opennvr-core"
    if ($cid) {
        $p = docker inspect $cid --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>$null
        if ($p) { return $p }
    }
    if ($env:COMPOSE_PROJECT_NAME) { return $env:COMPOSE_PROJECT_NAME }
    return ((Split-Path -Leaf (Get-Location).Path).ToLower() -replace '[^a-z0-9_-]', '')
}

# Run the updater in a core image; $Tag (when set) picks the image through
# CORE_TAG, which wins over .env for compose's interpolation.
function Invoke-Updater([string]$Tag, [string[]]$UpdArgs, [switch]$Quiet) {
    $here = (Get-Location).Path
    $pass = @()
    # A mirror: the shell's UPDATE_SOURCE_URL wins over the one in .env.
    $src = if ($env:UPDATE_SOURCE_URL) { $env:UPDATE_SOURCE_URL } else { Get-EnvVar "UPDATE_SOURCE_URL" }
    if ($src) { $pass += @("-e", "UPDATE_SOURCE_URL=$src") }
    if ($env:OPENNVR_UPDATE_EXTRA_KEYS -and (Test-Path $env:OPENNVR_UPDATE_EXTRA_KEYS)) {
        Copy-Item $env:OPENNVR_UPDATE_EXTRA_KEYS (Join-Path $UpdDir "extra-keys.json") -Force
        $pass += @("-e", "OPENNVR_UPDATE_EXTRA_KEYS=/work/extra-keys.json")
    }
    $saved = $env:CORE_TAG
    $env:COMPOSE_IGNORE_ORPHANS = "True"   # a one-off container is not the place to warn about them
    try {
        if ($Tag) { $env:CORE_TAG = $Tag }
        $run = @("--progress", "quiet") + $script:UpdCa + @("run", "--rm", "--no-deps", "-T") + $pass + @(
            "-v", "${here}:/repo:ro", "-v", "$(Join-Path $here $UpdDir):/work",
            "--entrypoint", "python", "opennvr-core", "-m", "updater") + $UpdArgs
        # To the screen, not the pipeline: a function returns everything its
        # commands print, and the caller wants only the exit code. (Never
        # 2>&1 here: Windows PowerShell turns each stderr line into an error.)
        if ($Quiet) { docker compose @run 2>$null | Out-Null }
        else { docker compose @run | Out-Host }
        return $LASTEXITCODE
    } finally {
        $env:CORE_TAG = $saved
        Remove-Item Env:COMPOSE_IGNORE_ORPHANS -ErrorAction SilentlyContinue
    }
}

function Write-UpdRunning {
    $ids = @(docker ps -q --filter "label=com.docker.compose.project=$script:UpdProject")
    $out = @()
    if ($ids.Count -gt 0) {
        $out = @(docker inspect --format '{{.Name}} {{.Image}} {{.Config.Image}}' @ids)
    }
    [IO.File]::WriteAllLines((Join-Path (Get-Location).Path "$UpdDir\running.txt"), [string[]]$out, $Utf8NoBom)
}

function Lock-Update {
    New-Item -ItemType Directory -Force $UpdDir | Out-Null
    $lock = Join-Path $UpdDir ".lock"
    if (Test-Path $lock) {
        $old = Get-Content $lock -ErrorAction SilentlyContinue
        if ($old -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) {
            Write-UpdFail "Another update is running (pid $old)."
            exit 1
        }
        Write-UpdWarn "Taking over a stale update lock."
    }
    Set-Content -Path $lock -Value $PID -Encoding ascii
}
function Unlock-Update { Remove-Item (Join-Path $UpdDir ".lock") -Force -ErrorAction SilentlyContinue }

# ── backups ──────────────────────────────────────────────────────────

# Run a shell script inside a container. Copied in as a file, never passed
# as an argument: Windows PowerShell 5.1 does not escape the double quotes
# inside a native command's argument, so `sh -c '... -c "DROP SCHEMA x"'`
# reaches psql as `-c DROP` (found by the UPD-207 rollback test).
function Invoke-InContainer([string]$Cid, [string]$Script) {
    $file = Join-Path (Get-Location).Path "$UpdDir\container-step.sh"
    [IO.File]::WriteAllText($file, ($Script -replace "`r`n", "`n") + "`n", $Utf8NoBom)
    docker cp $file "${Cid}:/tmp/opennvr-update-step.sh" | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    docker exec $Cid sh /tmp/opennvr-update-step.sh | Out-Host
    $ok = ($LASTEXITCODE -eq 0)
    docker exec $Cid rm -f /tmp/opennvr-update-step.sh | Out-Null
    Remove-Item $file -ErrorAction SilentlyContinue
    return $ok
}

function Backup-UpdSite([string]$Dir) {
    New-Item -ItemType Directory -Force $Dir | Out-Null
    Write-UpdSay "Backing up the database ..."
    $db = Get-UpdCid "db"
    if (-not $db) { Write-UpdFail "the database container is not running"; return $false }
    # Dumped inside the container and copied out: never through a PowerShell
    # pipe or redirect, which re-encodes the binary archive and corrupts it.
    if (-not (Invoke-InContainer $db 'set -e
pg_dump -U "$POSTGRES_USER" -Fc -d "$POSTGRES_DB" -f /tmp/opennvr-update.dump')) { return $false }
    docker cp "${db}:/tmp/opennvr-update.dump" (Join-Path $Dir "db.dump") | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    docker exec $db rm -f /tmp/opennvr-update.dump | Out-Null
    $head = [Text.Encoding]::ASCII.GetString([IO.File]::ReadAllBytes((Resolve-Path (Join-Path $Dir "db.dump")))[0..4])
    if ($head -ne "PGDMP") { Write-UpdFail "the database dump is not a valid archive"; return $false }
    $image = docker inspect (Get-UpdCid "opennvr-core") --format '{{.Config.Image}}'
    $abs = (Resolve-Path $Dir).Path
    foreach ($v in $UpdVolumes) {
        $name = docker volume ls -q --filter "label=com.docker.compose.project=$script:UpdProject" --filter "label=com.docker.compose.volume=$v"
        if (-not $name) { continue }
        docker run --rm -v "${name}:/v:ro" -v "${abs}:/b" --entrypoint tar $image czf "/b/$v.tgz" -C /v . | Out-Host
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    Copy-Item .env (Join-Path $Dir "env")
    # Only this user may read the backup (it holds .env and the database).
    icacls $abs /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
    Write-UpdOk "Backup in $Dir"
    return $true
}

function Restore-UpdSite([string]$Dir, [string]$Image) {
    if (-not (Test-Path (Join-Path $Dir "db.dump"))) { Write-UpdFail "$Dir has no db.dump"; return $false }
    Write-UpdSay "Restoring the database from $Dir ..."
    docker compose @script:UpdCa stop opennvr-core 2>$null | Out-Null
    $db = Get-UpdCid "db"
    if (-not $db) { Write-UpdFail "the database container is not running"; return $false }
    docker cp (Join-Path $Dir "db.dump") "${db}:/tmp/opennvr-restore.dump" | Out-Null
    if ($LASTEXITCODE -ne 0) { return $false }
    $restore = @'
set -e
export PGOPTIONS="-c client_min_messages=warning"
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q -c "DROP SCHEMA public CASCADE" -c "CREATE SCHEMA public"
pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error /tmp/opennvr-restore.dump
rm -f /tmp/opennvr-restore.dump
'@
    if (-not (Invoke-InContainer $db $restore)) { return $false }
    $abs = (Resolve-Path $Dir).Path
    foreach ($v in $UpdVolumes) {
        if (-not (Test-Path (Join-Path $Dir "$v.tgz"))) { continue }
        $name = docker volume ls -q --filter "label=com.docker.compose.project=$script:UpdProject" --filter "label=com.docker.compose.volume=$v"
        if (-not $name) { continue }
        docker run --rm -v "${name}:/v" -v "${abs}:/b:ro" --entrypoint sh $Image -c "find /v -mindepth 1 -delete && tar xzf /b/$v.tgz -C /v" | Out-Host
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    Write-UpdOk "Database and volumes restored."
    return $true
}

function Remove-OldUpdBackups {
    if (-not (Test-Path $UpdBackups)) { return }
    Get-ChildItem $UpdBackups -Directory | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $UpdKeep | ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
}

# ── getting the release onto the host (no downtime yet) ─────────────

function Get-UpdCommit([string]$Tag, [string]$Commit, [string]$Bundle) {
    if (-not $Tag) {
        # An edge build: an untagged main commit, fetched by its hash.
        git fetch -q origin $Commit | Out-Host
        if ($LASTEXITCODE -ne 0) { return $false }
        git cat-file -e "$Commit^{commit}" | Out-Host
        return ($LASTEXITCODE -eq 0)
    }
    if ($Bundle) { git fetch -q (Join-Path $Bundle "repo.bundle") "refs/tags/${Tag}:refs/tags/${Tag}" | Out-Host }
    else { git fetch -q origin "refs/tags/${Tag}:refs/tags/${Tag}" | Out-Host }
    if ($LASTEXITCODE -ne 0) { return $false }
    $got = git rev-parse "$Tag^{commit}"
    if ($got -ne $Commit) { Write-UpdFail "tag $Tag is commit $got, but the signed manifest says $Commit"; return $false }
    return $true
}

function Get-UpdImages([string]$Bundle) {
    if ($Bundle) {
        Write-UpdSay "Loading the images from the bundle ..."
        docker load -q -i (Join-Path $Bundle "images.tar") | Out-Null
        if ($LASTEXITCODE -ne 0) { return $false }
    }
    foreach ($line in [IO.File]::ReadAllLines((Resolve-Path "$UpdDir\images.txt"), $Utf8NoBom)) {
        $parts = $line -split ' '
        if ($parts.Count -lt 3) { continue }
        $ref, $digest, $accept = $parts[0], $parts[1], ($parts[2] -split ',')
        $repo = $ref.Substring(0, $ref.LastIndexOf(':'))
        if (-not $Bundle) {
            Write-UpdSay "Pulling $ref ..."
            docker pull -q "$repo@$digest" | Out-Null
            if ($LASTEXITCODE -ne 0) { return $false }
            docker tag "$repo@$digest" $ref | Out-Host
            if ($LASTEXITCODE -ne 0) { return $false }
        }
        $id = docker image inspect --format '{{.Id}}' $ref 2>$null
        if (-not $id) {
            # A delta bundle leaves out what the site should already have.
            Write-UpdFail "$ref is neither in the bundle nor on this host: make a full bundle (no --since)"
            return $false
        }
        if ($accept -notcontains $id) { Write-UpdFail "$ref is $id, not the image the release signed"; return $false }
    }
    Write-UpdOk "All release images are here and match the signed manifest."
    return $true
}

# ── the switch, verification, rollback ───────────────────────────────

function Invoke-UpdUp {
    # start.ps1's own `up` in a child PowerShell, without --remove-orphans
    # and rebuilding the images the release builds from its own files --
    # except from a bundle: an air-gapped site cannot build.
    $env:KEEP_ORPHANS = "1"
    $env:UP_BUILD = if ($script:UpdNoBuild) { "0" } else { "1" }
    try {
        $shell = (Get-Process -Id $PID).Path
        # $PSScriptRoot is scripts\ here (this file is dot-sourced from there).
        $startPs1 = Join-Path (Split-Path $PSScriptRoot -Parent) "start.ps1"
        & $shell -NoProfile -ExecutionPolicy Bypass -File $startPs1 up *> (Join-Path $UpdDir "up.log")
        return ($LASTEXITCODE -eq 0)
    } finally {
        Remove-Item Env:KEEP_ORPHANS, Env:UP_BUILD -ErrorAction SilentlyContinue
    }
}

function Test-UpdVerify([string]$ManifestDir) {
    Write-UpdRunning
    $timeout = if ($env:OPENNVR_UPDATE_VERIFY_TIMEOUT) { $env:OPENNVR_UPDATE_VERIFY_TIMEOUT } else { "300" }
    $code = Invoke-Updater "" @("verify", "--manifest-dir", "/work/$ManifestDir", "--running", "/work/running.txt",
                                "--platform", $script:UpdPlatform, "--timeout", $timeout)
    return ($code -eq 0)
}

function Invoke-UpdRollback {
    $plan = "$UpdDir\plan.env"
    $fromCommit = Get-UpdKV $plan FROM_COMMIT
    $restoreDb  = Get-UpdKV $plan DB_RESTORE_ON_ROLLBACK
    $mode       = Get-UpdKV $plan ROLLBACK
    $backup     = Get-UpdKV "$UpdDir\state.env" BACKUP -Last
    Add-UpdState @("STEP=rolling_back")
    if ($mode -eq "manual") {
        Write-UpdFail "This release changes data outside the database, so it is not rolled back automatically."
        Write-UpdSay "The stack is left as it is. Your backup: $backup"
        Write-UpdSay "To go back anyway: .\start.ps1 update --restore $backup"
        Complete-UpdRun "failed"; return $false
    }
    Write-UpdWarn "Rolling back to $(Get-UpdKV $plan FROM_VERSION) ..."
    git checkout -q -B opennvr-release $fromCommit | Out-Host
    if ($LASTEXITCODE -ne 0) { Complete-UpdRun "failed"; return $false }
    Copy-Item (Join-Path $backup "env") .env -Force
    $script:UpdCa = Get-ComposeArgs
    $oldImage = "ghcr.io/open-nvr/core:$(Get-EnvVar CORE_TAG)"
    if ($restoreDb -eq "1") {
        if (-not (Restore-UpdSite $backup $oldImage)) { Complete-UpdRun "failed"; return $false }
    }
    if (-not (Invoke-UpdUp)) {
        Complete-UpdRun "failed"
        Write-UpdFail "The old version did not start either. See $UpdDir\up.log; backup: $backup"
        return $false
    }
    if ((Test-Path "$UpdDir\installed\release-manifest.json") -and -not (Test-UpdVerify "installed")) {
        Complete-UpdRun "failed"
        Write-UpdFail "Rolled back, but the old version does not check out healthy. Backup: $backup"
        return $false
    }
    Complete-UpdRun "rolled_back"
    Write-UpdWarn "Rolled back: the site runs $(Get-UpdKV $plan FROM_VERSION) again, with its data."
    return $true
}

function Complete-Update {
    Complete-UpdRun "succeeded"
    Remove-Item "$UpdDir\installed" -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item "$UpdDir\candidate" "$UpdDir\installed"
    Remove-OldUpdBackups
    Write-UpdOk "OpenNVR $(Get-UpdKV "$UpdDir\plan.env" TO_VERSION) is running and healthy."
}

function Invoke-UpdSwitch {
    Add-UpdState @("STEP=switching")
    git checkout -q -B opennvr-release (Get-UpdKV "$UpdDir\plan.env" GIT_COMMIT) | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-UpdFail "could not check out the release"; Invoke-UpdRollback | Out-Null; return $false }
    foreach ($kv in [IO.File]::ReadAllLines((Resolve-Path "$UpdDir\pins.env"), $Utf8NoBom)) {
        if ($kv) { $k, $v = $kv -split '=', 2; Set-DotEnvVar $k $v }
    }
    $script:UpdCa = Get-ComposeArgs
    Write-UpdSay "Starting the new version (log: $UpdDir\up.log) ..."
    if (-not (Invoke-UpdUp)) { Write-UpdFail "compose up failed"; Invoke-UpdRollback | Out-Null; return $false }
    Add-UpdState @("STEP=verifying")
    if (-not (Test-UpdVerify "candidate")) { Invoke-UpdRollback | Out-Null; return $false }
    Complete-Update
    return $true
}

# ── the runner: one tick a minute, from the Scheduled Task ────────────
# Same as upd_queue in update-host.sh: heartbeat; an "Update now" asked for
# in the app; else, unless updates are off, a check every few hours, and in
# auto mode the install when updater/policy.py says it is due.

function Invoke-UpdateQueue {
    $here = (Get-Location).Path
    New-Item -ItemType Directory -Force (Join-Path $here $UpdDir), (Join-Path $here "data\update-requests") | Out-Null
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    [IO.File]::WriteAllText((Join-Path $here "$UpdDir\runner.env"), "LAST_SEEN=$now`n", $Utf8NoBom)
    $lock = Join-Path $UpdDir ".lock"
    if (Test-Path $lock) {
        $old = Get-Content $lock -ErrorAction SilentlyContinue
        if ($old -and (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)) { return }
    }
    $req = "data\update-requests\request.env"
    $policy = "data\update-requests\policy.env"
    $id = Get-UpdKV $req ID
    $handled = if (Test-Path "$UpdDir\last-request") { (Get-Content "$UpdDir\last-request" -Raw).Trim() } else { "" }
    if ($id -and $id -ne $handled) {
        # Marked first: a request is tried once, never in a loop.
        [IO.File]::WriteAllText((Join-Path $here "$UpdDir\last-request"), "$id`n", $Utf8NoBom)
        Invoke-Update -UpdateArgs @("--to", (Get-UpdKV $req TO), "--yes")
        return
    }
    $mode = Get-UpdKV $policy MODE
    if (-not $mode -or $mode -eq "off") { return }
    $last = Get-UpdKV "$UpdDir\check.env" CHECKED_EPOCH
    $every = if ($env:OPENNVR_UPDATE_CHECK_EVERY) { [long]$env:OPENNVR_UPDATE_CHECK_EVERY } else { 21600 }
    if (-not $last -or ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$last) -ge $every) {
        Invoke-Update -UpdateArgs @("--dry-run")
    }
    if ($mode -ne "auto" -or (Get-UpdKV "$UpdDir\check.env" CODE -Last) -ne "0") { return }
    $script:UpdCa = Get-ComposeArgs
    $due = Invoke-Updater "" @("due", "--policy-file", "/repo/data/update-requests/policy.env",
                               "--local-time", (Get-Date -Format "yyyy-MM-ddTHH:mm"))
    if ($due -eq 0) { Invoke-Update -UpdateArgs @("--to", (Get-UpdKV "$UpdDir\plan.env" TO_VERSION), "--yes") }
}

# Called by `up`. The runner is on unless .env says UPDATE_RUNNER=off, so a
# fresh install and an existing site that upgrades both get it. On its own
# it only leaves a heartbeat: nothing is checked or installed until the
# update policy in the app says so (off by default, plan D6).
#
# Only from an administrator PowerShell: without admin rights the task can
# only run as a logged-on task, which flashes a console window every
# minute, and that is not something to switch on unasked.
function Update-UpdateRunner {
    if ((Get-EnvVar "UPDATE_RUNNER") -eq "off") { return }
    if (Get-ScheduledTask -TaskName "OpenNVR update runner" -ErrorAction SilentlyContinue) { return }
    $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if ($admin) {
        Write-UpdSay "Installing the update runner, so OpenNVR Updates in the app can work"
        Write-UpdSay "(it does nothing until an update policy is chosen there; opt out: .\start.ps1 auto-update disable)."
        try { Set-AutoUpdate "enable" }
        catch { Write-UpdWarn "The update runner is not installed: $($_.Exception.Message)" }
    } else {
        Write-UpdWarn "The update runner is not installed. From an administrator PowerShell: .\start.ps1 auto-update enable"
    }
}

# .\start.ps1 auto-update enable|disable|status: the Scheduled Task that runs the tick.
function Set-AutoUpdate([string]$Action) {
    $name = "OpenNVR update runner"
    $here = (Get-Location).Path
    switch ($Action) {
        "enable" {
            $shell = (Get-Process -Id $PID).Path
            $act = New-ScheduledTaskAction -Execute $shell -WorkingDirectory $here `
                -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$here\start.ps1`" update --from-queue"
            $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
            $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
                -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            $desc = "Runs '.\start.ps1 update --from-queue' in $here each minute: the app's Update now and automatic updates."
            # S4U runs without a window (a logged-on task flashes a console
            # every minute) and stores no password; Windows may refuse it to a
            # non-admin, and then the task runs while this user is logged on.
            try {
                $who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U
                Register-ScheduledTask -TaskName $name -Action $act -Trigger $trigger -Settings $settings `
                    -Principal $who -Description $desc -Force -ErrorAction Stop | Out-Null
                Write-UpdOk "The update runner checks in every minute, in the background. The update policy is set in the app."
            } catch {
                $who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
                Register-ScheduledTask -TaskName $name -Action $act -Trigger $trigger -Settings $settings `
                    -Principal $who -Description $desc -Force | Out-Null
                Write-UpdOk "The update runner checks in every minute while you are logged on."
                Write-UpdSay "(Run this from an administrator PowerShell to let it run in the background instead.)"
            }
            if (Test-Path ".env") { Set-DotEnvVar "UPDATE_RUNNER" "on" }
        }
        "disable" {
            # The flag first: the next `up` must not put it back.
            if (Test-Path ".env") { Set-DotEnvVar "UPDATE_RUNNER" "off" }
            Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
            Write-UpdOk "The update runner is off. Updates can still be run by hand: .\start.ps1 update"
        }
        default {
            $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
            $seen = Get-UpdKV "$UpdDir\runner.env" LAST_SEEN
            if ($task) { Write-UpdSay "The update runner is installed ($($task.State))." }
            else { Write-UpdSay "The update runner is not installed. Turn it on with: .\start.ps1 auto-update enable" }
            if ($seen) { Write-UpdSay "It last checked in at $seen (UTC)." }
        }
    }
}

# ── entry point ──────────────────────────────────────────────────────

function Invoke-Update([string[]]$UpdateArgs) {
    $to = ""; $bundleFile = ""; $dry = $false; $yes = $false; $restore = ""; $extra = @()
    for ($i = 0; $i -lt $UpdateArgs.Count; $i++) {
        switch ($UpdateArgs[$i]) {
            "--to"            { $to = $UpdateArgs[++$i] }
            "--bundle"        { $bundleFile = $UpdateArgs[++$i] }
            "--dry-run"       { $dry = $true }
            { $_ -in "--yes", "-y" } { $yes = $true }
            "--adopt"         { $extra += "--adopt" }
            "--accept-manual" { $extra += "--accept-manual" }
            "--channel"       { $extra += @("--channel", $UpdateArgs[++$i]) }
            "--restore"       { $restore = $UpdateArgs[++$i] }
            "--from-queue"    { Invoke-UpdateQueue; return }
            "--list-images"   {
                # For scripts\make-offline-bundle.py: what this site runs.
                $ca = Get-ComposeArgs
                docker compose @ca config --images
                return
            }
            default           { Write-UpdFail "unknown option: $($UpdateArgs[$i])"; exit 1 }
        }
    }
    if (-not (Test-Path .env)) { Write-UpdFail "No .env here: run this from the OpenNVR folder."; exit 1 }
    # Git's own words: "not a git repository" and "dubious ownership" (the
    # folder belongs to another user) need different fixes.
    $gitErr = git rev-parse --git-dir 2>&1 | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] } | ForEach-Object { "$_" }
    if ($LASTEXITCODE -ne 0) {
        Write-UpdFail "git cannot use this OpenNVR folder:"
        $gitErr | ForEach-Object { Write-Host "    $_" }
        exit 1
    }

    Lock-Update
    try {
        $script:UpdCa = Get-ComposeArgs
        $script:UpdProject = Get-UpdProject
        $script:UpdPlatform = docker version --format '{{.Server.Os}}/{{.Server.Arch}}'
        if ($LASTEXITCODE -ne 0) { Write-UpdFail "Docker is not running."; exit 1 }

        if ($restore) {
            if (-not (Test-Path $restore)) { Write-UpdFail "no backup at $restore"; exit 1 }
            if (-not ((Test-Path (Join-Path $restore "db.dump")) -and (Test-Path (Join-Path $restore "env")))) {
                Write-UpdFail "$restore is not a complete backup"; exit 1 }
            # Everything checked before anything changes: a refusal must not leave
            # the backup's .env in place over a stack still running another release.
            if (-not (Get-UpdCid "db")) { Write-UpdFail "the database container is not running; start the stack first"; exit 1 }
            Copy-Item (Join-Path $restore "env") .env -Force
            $script:UpdCa = Get-ComposeArgs
            if (-not (Restore-UpdSite $restore "ghcr.io/open-nvr/core:$(Get-EnvVar CORE_TAG)")) { exit 1 }
            Write-UpdSay "Note: the OpenNVR folder is not moved back; check out the matching release yourself if needed."
            if (-not (Invoke-UpdUp)) { Write-UpdFail "compose up failed; see $UpdDir\up.log"; exit 1 }
            Write-UpdOk "Restored from $restore."
            return
        }

        # An update that was interrupted after the switch is finished first.
        $step = Get-UpdKV "$UpdDir\state.env" STEP -Last
        if ($step -in "switching", "verifying") {
            Write-UpdWarn "An update to $(Get-UpdKV "$UpdDir\plan.env" TO_VERSION) was interrupted; checking it now."
            if (Test-UpdVerify "candidate") { Complete-Update } elseif (-not (Invoke-UpdRollback)) { exit 1 }
            return
        }
        if ($step -eq "rolling_back") {
            Write-UpdWarn "A rollback was interrupted; finishing it."
            if (-not (Invoke-UpdRollback)) { exit 1 }
            return
        }

        $probe = Invoke-Updater "" @("--help") -Quiet
        if ($probe -ne 0) {
            Write-UpdFail "The running OpenNVR ($(Get-EnvVar CORE_TAG)) predates the updater."
            Write-UpdSay "Move to the first release that has it by hand, once: DOCKER_QUICKSTART.md,"
            Write-UpdSay "`"Upgrading to a new release`". After that, .\start.ps1 update works."
            exit 1
        }

        # Facts only the host can see, for the updater's preflight.
        $changed = @(git status --porcelain --untracked-files=no | ForEach-Object { $_.Substring(3) })
        [IO.File]::WriteAllLines((Join-Path (Get-Location).Path "$UpdDir\changed.txt"), [string[]]$changed, $Utf8NoBom)
        $images = @(docker compose @script:UpdCa config --images)
        if ($LASTEXITCODE -ne 0) { Write-UpdFail "compose config failed"; exit 1 }
        [IO.File]::WriteAllLines((Join-Path (Get-Location).Path "$UpdDir\site-images.txt"), [string[]]$images, $Utf8NoBom)
        $bundle = ""
        if ($bundleFile) {
            if (-not (Test-Path $bundleFile)) { Write-UpdFail "no bundle at $bundleFile"; exit 1 }
            Remove-Item "$UpdDir\bundle" -Recurse -Force -ErrorAction SilentlyContinue
            New-Item -ItemType Directory -Force "$UpdDir\bundle" | Out-Null
            Write-UpdSay "Unpacking $bundleFile ..."
            tar -xf $bundleFile -C "$UpdDir\bundle"
            if ($LASTEXITCODE -ne 0) { Write-UpdFail "the bundle could not be unpacked"; exit 1 }
            $bundle = "$UpdDir\bundle"
            $extra += @("--bundle", "/work/bundle")
            $script:UpdNoBuild = $true
        }
        if ($to) { $extra += @("--to", $to) }

        Write-UpdSay "Checking for an update ..."
        $coreId = docker inspect --format '{{.Image}}' (Get-UpdCid "opennvr-core") 2>$null
        $code = Invoke-Updater "" (@("check", "--platform", $script:UpdPlatform, "--head", (git rev-parse HEAD),
                                     "--changed-files", "/work/changed.txt", "--site-images", "/work/site-images.txt",
                                     "--core-image-id", "$coreId") + $extra)
        if ($code -eq 10) { return }
        if ($code -ne 0) { exit $code }
        if ($dry) { Write-UpdSay "Dry run: nothing changed."; return }

        $plan = "$UpdDir\plan.env"
        $from = Get-UpdKV $plan FROM_VERSION; $toV = Get-UpdKV $plan TO_VERSION
        if (-not $yes) {
            Write-Host ""
            Write-UpdSay "The stack restarts the services that change (about a minute). A backup is taken first,"
            Write-UpdSay "and if $toV does not come up healthy the site goes back to $from with its data."
            Write-UpdSay "Anything recorded or changed during the update is lost if it rolls back."
            $answer = Read-Host "  Update OpenNVR $from -> $toV now? [y/N]"
            if ($answer -notmatch '^[Yy]$') { Write-UpdSay "Nothing changed."; return }
        }

        # A mirror (UPDATE_SOURCE_URL) that serves bundles: this platform's
        # bundle brings the release's files and images (see update-host.sh).
        $srcUrl = if ($env:UPDATE_SOURCE_URL) { $env:UPDATE_SOURCE_URL } else { Get-EnvVar "UPDATE_SOURCE_URL" }
        if (-not $bundle -and $srcUrl) {
            $url = "$($srcUrl.TrimEnd('/'))/v$toV/opennvr-$($script:UpdPlatform -replace '/', '-')-offline.tar"
            $tarFile = Join-Path $UpdDir "mirror-bundle.tar"
            Write-UpdSay "Fetching the bundle from the mirror ..."
            $saved = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'   # 5.1's progress bar is slower than the download
            try {
                Invoke-WebRequest -Uri $url -OutFile $tarFile -UseBasicParsing -ErrorAction Stop
                Remove-Item "$UpdDir\bundle" -Recurse -Force -ErrorAction SilentlyContinue
                New-Item -ItemType Directory -Force "$UpdDir\bundle" | Out-Null
                tar -xf $tarFile -C "$UpdDir\bundle"
                if ($LASTEXITCODE -ne 0) { Write-UpdFail "the mirror's bundle could not be unpacked"; exit 1 }
                Remove-Item $tarFile -Force
                $bundle = "$UpdDir\bundle"
                $script:UpdNoBuild = $true
            } catch {
                Write-UpdSay "No bundle for $($script:UpdPlatform) on the mirror; images come from their registries."
            } finally { $ProgressPreference = $saved }
        }

        # 1. Everything onto the host first: nothing has stopped yet.
        if (-not (Get-UpdCommit (Get-UpdKV $plan GIT_TAG) (Get-UpdKV $plan GIT_COMMIT) $bundle)) {
            Write-UpdFail "Could not get the release's files; nothing changed."; exit 1 }
        if (-not (Get-UpdImages $bundle)) { Write-UpdFail "Could not get the release's images; nothing changed."; exit 1 }

        # 2. Back up, then switch.
        [IO.File]::WriteAllText((Join-Path (Get-Location).Path "$UpdDir\state.env"), "", $Utf8NoBom)
        $backup = Join-Path $UpdBackups "$(Get-Date -Format 'yyyyMMdd-HHmmss')-$from"
        Add-UpdState @("FROM=$from", "TO=$toV", "STARTED=$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))",
                       "STEP=backing_up", "BACKUP=$backup")
        if (-not (Backup-UpdSite $backup)) {
            Complete-UpdRun "failed"; Write-UpdFail "The backup failed; nothing changed."; exit 1 }
        if (-not (Invoke-UpdSwitch)) { exit 1 }
    } finally {
        Unlock-Update
    }
}
