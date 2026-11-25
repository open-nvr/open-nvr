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
#   .\start.ps1 update --apps [--app ID [--yes]]   app releases from the registry
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
        # From the JSON, not a --format template: the template's double
        # quotes do not survive 5.1's argument passing (docker exits 64), and
        # this silently fell back to the folder name.
        $json = (docker inspect $cid 2>$null) -join "`n"
        if ($LASTEXITCODE -eq 0 -and $json) {
            $c = @(ConvertFrom-Json -InputObject $json)
            $p = if ($c.Count -gt 0 -and $c[0].Config.Labels) { $c[0].Config.Labels.'com.docker.compose.project' } else { "" }
            if ($p) { return $p }
        }
    }
    if ($env:COMPOSE_PROJECT_NAME) { return $env:COMPOSE_PROJECT_NAME }
    return ((Split-Path -Leaf (Get-Location).Path).ToLower() -replace '[^a-z0-9_-]', '')
}

# Run the updater in a core image; $Tag (when set) picks the image through
# CORE_TAG, which wins over .env for compose's interpolation.
function Invoke-Updater([string]$Tag, [string[]]$UpdArgs, [switch]$Quiet, [string]$LogFile) {
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
        if ($LogFile) {
            $lines = @(docker compose @run 2>$null)
            $code = $LASTEXITCODE
            [IO.File]::WriteAllLines((Join-Path $here $LogFile), [string[]]$lines, $Utf8NoBom)
            return $code
        }
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
    # The release verifies itself from inside the running core container,
    # not a one-off `compose run` one: on Compose 2.24 that one randomly
    # comes up on a single network (docker/compose#11533) and cannot reach
    # the database. Core mounts the state folder at /app/update-state. The
    # verdict is kept in verify-<candidate|installed>.log, which the app shows
    # under a failed run, for whoever asks later why it rolled back.
    $log = Join-Path (Get-Location).Path "$UpdDir\verify-$ManifestDir.log"
    $cid = Get-UpdCid "opennvr-core"
    $running = if ($cid) { docker inspect -f '{{.State.Running}}' $cid 2>$null } else { "" }
    if ($running -ne "true") {
        Write-UpdFail "the core container is not running"
        [IO.File]::WriteAllLines($log, [string[]]@("the core container is not running"), $Utf8NoBom)
        return $false
    }
    $pass = @()
    if (Test-Path (Join-Path $UpdDir "extra-keys.json")) {
        $pass += @("-e", "OPENNVR_UPDATE_EXTRA_KEYS=/app/update-state/extra-keys.json")
    }
    $out = @(docker exec @pass $cid python -m updater verify --manifest-dir "/app/update-state/$ManifestDir" `
                 --running /app/update-state/running.txt --platform $script:UpdPlatform --timeout $timeout)
    $code = $LASTEXITCODE
    $out | Out-Host
    [IO.File]::WriteAllLines($log, [string[]]$out, $Utf8NoBom)
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
    # An adopted site never matched the installed manifest (--adopt waived
    # that), so after a rollback it is only required to come up, as it did.
    if ((Get-UpdKV $plan ADOPTED) -ne "1" -and (Test-Path "$UpdDir\installed\release-manifest.json") -and -not (Test-UpdVerify "installed")) {
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
    # Update now for one app, asked for in the app: tried once, like a release.
    $appReq = "data\update-requests\app-request.env"
    $appId = Get-UpdKV $appReq ID
    $appHandled = if (Test-Path "$UpdDir\last-app-request") { (Get-Content "$UpdDir\last-app-request" -Raw).Trim() } else { "" }
    if ($appId -and $appId -ne $appHandled) {
        [IO.File]::WriteAllText((Join-Path $here "$UpdDir\last-app-request"), "$appId`n", $Utf8NoBom)
        Invoke-UpdApps @("--app", (Get-UpdKV $appReq APP), "--now", "--yes")
        return
    }
    # Check now, asked for in the app: this tick looks for new releases of
    # OpenNVR and the apps instead of waiting for the six-hourly check. It
    # installs nothing by itself; an automatic install still waits for the
    # window. Marked first, like the requests above: tried once.
    $checkReq = "data\update-requests\check-request.env"
    $checkId = Get-UpdKV $checkReq ID
    $checkHandled = if (Test-Path "$UpdDir\last-check-request") { (Get-Content "$UpdDir\last-check-request" -Raw).Trim() } else { "" }
    $force = $false
    if ($checkId -and $checkId -ne $checkHandled) {
        [IO.File]::WriteAllText((Join-Path $here "$UpdDir\last-check-request"), "$checkId`n", $Utf8NoBom)
        $force = $true
    }
    $mode = Get-UpdKV $policy MODE
    if (-not $mode) { $mode = "auto" }  # nothing saved yet: the default, install releases by itself
    if ($mode -ne "off") {
        $last = Get-UpdKV "$UpdDir\check.env" CHECKED_EPOCH
        $every = if ($env:OPENNVR_UPDATE_CHECK_EVERY) { [long]$env:OPENNVR_UPDATE_CHECK_EVERY } else { 21600 }
        if ($force -or -not $last -or ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [long]$last) -ge $every) {
            Invoke-Update -UpdateArgs @("--dry-run")
        }
        if ($mode -eq "auto" -and (Get-UpdKV "$UpdDir\check.env" CODE -Last) -eq "0") {
            $script:UpdCa = Get-ComposeArgs
            $due = Invoke-Updater "" @("due", "--policy-file", "/repo/data/update-requests/policy.env",
                                       "--local-time", (Get-Date -Format "yyyy-MM-ddTHH:mm"))
            if ($due -eq 0) {
                Invoke-Update -UpdateArgs @("--to", (Get-UpdKV "$UpdDir\plan.env" TO_VERSION), "--yes")
                return
            }
        }
    }
    # Apps have their own mode (APPS_MODE, APP_<ID>_MODE; by default the
    # one above), which updater/policy.py applies.
    if ($force) {
        $saved = $env:OPENNVR_APPS_CHECK_EVERY
        $env:OPENNVR_APPS_CHECK_EVERY = "0"
        try { Invoke-UpdApps @("--tick") } finally { $env:OPENNVR_APPS_CHECK_EVERY = $saved }
    } else {
        Invoke-UpdApps @("--tick")
    }
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
        Write-UpdSay "(it installs releases by itself at night, with a backup and rollback; change that in the app, or opt out: .\start.ps1 auto-update disable)."
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

# ── apps from the registry ───────────────────────────────────────────
# Same steps, files and rules as "apps from the registry" in
# update-host.sh (read that for the story): check, cosign, due, then one app
# at a time: pull, stop, back up its writable volumes, pin <APP>_IMAGE in
# .env, start, wait for healthy, and put it all back when it is not.
#
# Docker is read as JSON (docker inspect) rather than through Go templates,
# and docker itself runs through Start-Process: a template's double quotes do
# not survive Windows PowerShell 5.1's native argument passing, and a native
# command's stderr redirected in 5.1 comes back wrapped in error records.

$UpdAppsBackups = Join-Path $UpdBackups "apps"
$UpdAppsKeep    = if ($env:OPENNVR_APP_KEEP_BACKUPS) { [int]$env:OPENNVR_APP_KEEP_BACKUPS } else { 2 }
# The image that checks signatures is itself pinned by digest.
$UpdCosign      = "ghcr.io/sigstore/cosign/cosign:v2.4.1@sha256:b03690aa52bfe94054187142fba24dc54137650682810633901767d8a3e15b31"

function Add-UpdAppsState([string[]]$Pairs) {
    [IO.File]::AppendAllText((Join-Path (Get-Location).Path "$UpdDir\apps-state.env"), (($Pairs -join "`n") + "`n"), $Utf8NoBom)
}

function Complete-UpdApps([string]$Result) {
    $s = "$UpdDir\apps-state.env"
    $finished = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    Add-UpdAppsState @("STEP=$Result", "FINISHED=$finished")
    $line = "$finished $(Get-UpdKV $s APP_ID) $(Get-UpdKV $s FROM_VERSION) $(Get-UpdKV $s VERSION) $Result`n"
    [IO.File]::AppendAllText((Join-Path (Get-Location).Path "$UpdDir\apps-history.log"), $line, $Utf8NoBom)
}

# docker, its output and errors appended to a log; returns the exit code.
# Start-Process hands docker one command line made by joining the arguments
# with spaces, so an argument with a space in it (a folder name, sh -c's
# script) is quoted here; none of ours contains a double quote.
function Invoke-UpdDocker([string[]]$DArgs, [string]$Log) {
    $here = (Get-Location).Path
    $o = Join-Path $here "$UpdDir\docker.out"; $e = Join-Path $here "$UpdDir\docker.err"
    $line = @($DArgs | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } })
    $p = Start-Process -FilePath (Get-Command docker).Source -ArgumentList $line -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $o -RedirectStandardError $e
    if ($Log) {
        foreach ($f in $o, $e) {
            if (Test-Path $f) { [IO.File]::AppendAllText((Join-Path $here $Log), [IO.File]::ReadAllText($f), $Utf8NoBom) }
        }
    }
    $script:UpdDockerOut = if (Test-Path $o) { [IO.File]::ReadAllText($o) } else { "" }
    $script:UpdDockerErr = if (Test-Path $e) { [IO.File]::ReadAllText($e) } else { "" }
    Remove-Item $o, $e -ErrorAction SilentlyContinue
    return $p.ExitCode
}

function Get-UpdInspect([string[]]$Ids) {
    $Ids = @($Ids | Where-Object { $_ })
    if ($Ids.Count -eq 0) { return }
    $json = (docker inspect @Ids 2>$null) -join "`n"
    if ($LASTEXITCODE -ne 0 -or -not $json) { return }
    return (ConvertFrom-Json -InputObject $json)
}

function Get-UpdLabel($Container, [string]$Name) {
    $labels = $Container.Config.Labels
    if (-not $labels) { return "" }
    $p = $labels.PSObject.Properties[$Name]
    if ($p) { return [string]$p.Value }
    return ""
}

function Write-UpdAppsRunning {
    $ids = @(docker ps -q --filter "label=com.docker.compose.project=$script:UpdProject")
    $out = @(foreach ($c in @(Get-UpdInspect $ids)) {
        ("$(Get-UpdLabel $c 'com.docker.compose.service') $($c.Config.Image) $(Get-UpdLabel $c 'org.opencontainers.image.version')").TrimEnd()
    })
    [IO.File]::WriteAllLines((Join-Path (Get-Location).Path "$UpdDir\apps-running.txt"), [string[]]$out, $Utf8NoBom)
}

# Each app's list of withdrawn releases, checked with cosign against the
# signer its catalog entry names, then read at the digest cosign checked
# (see upd_apps_revocations in update-host.sh).
function Invoke-UpdAppsRevocations {
    $here = (Get-Location).Path
    if ((Invoke-Updater "" @("apps-revocations-list") -Quiet) -ne 0) { return }
    $f = Join-Path $here "$UpdDir\apps-revocations.txt"
    if (-not (Test-Path $f)) { return }
    foreach ($line in [IO.File]::ReadAllLines($f, $Utf8NoBom)) {
        $p = $line -split ' '
        if ($p.Count -lt 4) { continue }
        $id = $p[0]; $ref = $p[1]; $identity = $p[2]; $issuer = $p[3]
        $code = Invoke-UpdDocker @("run", "--rm", $UpdCosign, "verify", "--certificate-oidc-issuer", $issuer,
                                   "--certificate-identity-regexp", $identity, $ref) ""
        if ($code -eq 0) {
            [IO.File]::WriteAllText((Join-Path $here "$UpdDir\apps-rev-$id.json"), $script:UpdDockerOut, $Utf8NoBom)
            $null = Invoke-Updater "" @("apps-revocations-record", "--id", $id, "--result", "ok",
                                        "--cosign-output", "/work/apps-rev-$id.json") -Quiet
        } else {
            $why = @($script:UpdDockerErr -split "\r?\n" | Where-Object { $_.Trim() }) | Select-Object -Last 1
            if (-not $why) { $why = "cosign exited $code" }
            $why = ($why -replace '"', "'")
            if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
            $why = $why.TrimEnd('\')
            Write-UpdWarn "${id}: its list of withdrawn releases did not check out: $why"
            $null = Invoke-Updater "" @("apps-revocations-record", "--id", $id, "--result", "failed", "--message", $why) -Quiet
        }
    }
}

function Invoke-UpdAppsCheck {
    Write-UpdAppsRunning
    Invoke-UpdAppsRevocations
    $code = Invoke-Updater "" @("apps-check", "--running", "/work/apps-running.txt", "--platform", $script:UpdPlatform,
                                "--policy-file", "/repo/data/update-requests/policy.env") -LogFile "$UpdDir\apps-check.log"
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    [IO.File]::WriteAllText((Join-Path (Get-Location).Path "$UpdDir\apps-check.env"), "CHECKED_EPOCH=$now`nCODE=$code`n", $Utf8NoBom)
    if ($code -ne 0) { Write-UpdFail "Checking the apps failed; see $UpdDir\apps-check.log"; return $false }
    Invoke-UpdAppsVerify
    return $true
}

# Every release apps-check offered and cosign has not passed yet; a failure
# is recorded with its reason and tried again on the next check.
function Invoke-UpdAppsVerify {
    $f = Join-Path (Get-Location).Path "$UpdDir\apps-verify.txt"
    if (-not (Test-Path $f)) { return }
    foreach ($line in [IO.File]::ReadAllLines($f, $Utf8NoBom)) {
        $p = $line -split ' '
        if ($p.Count -lt 4) { continue }
        $id = $p[0]; $ref = $p[1]; $identity = $p[2]; $issuer = $p[3]
        $digest = ($ref -split '@', 2)[1]
        $code = Invoke-UpdDocker @("run", "--rm", $UpdCosign, "verify", "--certificate-oidc-issuer", $issuer,
                                   "--certificate-identity-regexp", $identity, $ref) ""
        if ($code -eq 0) {
            $null = Invoke-Updater "" @("apps-verified", "--id", $id, "--digest", $digest, "--result", "ok",
                                        "--message", "cosign verified the signature") -Quiet
        } else {
            $why = @($script:UpdDockerErr -split "\r?\n" | Where-Object { $_.Trim() }) | Select-Object -Last 1
            if (-not $why) { $why = "cosign exited $code" }
            # One plain argument for docker compose under 5.1: no double
            # quote, and no backslash before the quote 5.1 wraps it in.
            $why = ($why -replace '"', "'")
            if ($why.Length -gt 300) { $why = $why.Substring(0, 300) }
            $why = $why.TrimEnd('\')
            Write-UpdWarn "$id ${digest}: the signature did not check out: $why"
            $null = Invoke-Updater "" @("apps-verified", "--id", $id, "--digest", $digest, "--result", "failed",
                                        "--message", $why) -Quiet
        }
    }
}

# The app's own writable named volumes; read-only mounts (another service's
# certificates, say) are not the app's to restore.
function Get-UpdAppVolumes([string]$Cid) {
    $c = @(Get-UpdInspect @($Cid))
    if ($c.Count -eq 0) { return }
    foreach ($m in @($c[0].Mounts)) {
        if ($m.Type -eq "volume" -and $m.RW) { "$($m.Name) $($m.Destination)" }
    }
}

function Backup-UpdApp([string]$Cid, [string]$Dir, [string]$Image) {
    New-Item -ItemType Directory -Force $Dir | Out-Null
    $abs = (Resolve-Path $Dir).Path
    $vols = @(Get-UpdAppVolumes $Cid)
    [IO.File]::WriteAllLines((Join-Path $abs "volumes.txt"), [string[]]$vols, $Utf8NoBom)
    foreach ($v in $vols) {
        $name = ($v -split ' ')[0]
        $code = Invoke-UpdDocker @("run", "--rm", "-v", "${name}:/v:ro", "-v", "${abs}:/b", "--entrypoint", "tar", $Image,
                                   "czf", "/b/$name.tgz", "-C", "/v", ".") "$UpdDir\apps-up.log"
        if ($code -ne 0) { return $false }
    }
    Copy-Item .env (Join-Path $abs "env")
    icacls $abs /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" | Out-Null
    return $true
}

function Restore-UpdApp([string]$Dir, [string]$Image) {
    $abs = (Resolve-Path $Dir).Path
    foreach ($v in [IO.File]::ReadAllLines((Join-Path $abs "volumes.txt"), $Utf8NoBom)) {
        $name = ($v -split ' ')[0]
        if (-not $name -or -not (Test-Path (Join-Path $abs "$name.tgz"))) { continue }
        $code = Invoke-UpdDocker @("run", "--rm", "-v", "${name}:/v", "-v", "${abs}:/b:ro", "--entrypoint", "sh", $Image,
                                   "-c", "find /v -mindepth 1 -delete && tar xzf /b/$name.tgz -C /v") "$UpdDir\apps-up.log"
        if ($code -ne 0) { return $false }
    }
    return $true
}

# Healthy: the container runs the image $Ref names and its healthcheck says
# healthy; with no healthcheck, still running 30 s later.
function Test-UpdAppHealthy([string]$Service, [string]$Ref) {
    $want = docker image inspect --format '{{.Id}}' $Ref 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $want) { return $false }
    $timeout = if ($env:OPENNVR_APP_VERIFY_TIMEOUT) { [int]$env:OPENNVR_APP_VERIFY_TIMEOUT } else { 300 }
    $up = 0
    for ($i = 0; $i -lt $timeout; $i += 5) {
        $c = @(Get-UpdInspect @(Get-UpdCid $Service))
        if ($c.Count -gt 0) {
            if ($c[0].Image -ne $want) { Write-UpdFail "$Service runs another image than $Ref"; return $false }
            $health = if ($c[0].State.Health) { $c[0].State.Health.Status } else { "none" }
            $state = "$($c[0].State.Status) $health"
            if ($state -eq "running healthy") { return $true }
            if ($state -eq "running none") { $up++; if ($up -ge 6) { return $true } }
            if ($state -eq "running unhealthy" -or $c[0].State.Status -in "exited", "dead") {
                Write-UpdFail "$Service is $state"; return $false }
        }
        Start-Sleep -Seconds 5
    }
    Write-UpdFail "$Service was not healthy within $timeout s"
    return $false
}

function Get-UpdCoreImage { return (docker inspect (Get-UpdCid "opennvr-core") --format '{{.Config.Image}}' 2>$null) }

function Invoke-UpdAppsRollback {
    $s = "$UpdDir\apps-state.env"
    $id = Get-UpdKV $s APP_ID; $key = Get-UpdKV $s KEY; $backup = Get-UpdKV $s BACKUP
    Add-UpdAppsState @("STEP=rolling_back")
    [IO.File]::AppendAllText((Join-Path (Get-Location).Path "$UpdDir\apps-failed.txt"), "$id $(Get-UpdKV $s VERSION)`n", $Utf8NoBom)
    Write-UpdWarn "Going back to $id $(Get-UpdKV $s FROM_VERSION) ..."
    $null = Invoke-UpdDocker (@("compose") + $script:UpdCa + @("stop", $id)) "$UpdDir\apps-up.log"
    $old = [IO.File]::ReadAllText((Join-Path (Get-Location).Path "$backup\env-value"), $Utf8NoBom).TrimEnd("`r", "`n")
    Set-DotEnvVar $key $old
    $script:UpdCa = Get-ComposeArgs
    if (-not (Restore-UpdApp $backup (Get-UpdCoreImage))) {
        Complete-UpdApps "failed"; Write-UpdFail "Could not put $id's data back. Backup: $backup"; return $false }
    $code = Invoke-UpdDocker (@("compose") + $script:UpdCa + @("up", "-d", "--no-deps", $id)) "$UpdDir\apps-up.log"
    if ($code -ne 0 -or -not (Test-UpdAppHealthy $id (Get-UpdKV $s FROM_REF))) {
        Complete-UpdApps "failed"
        Write-UpdFail "The old $id did not come back healthy either. See $UpdDir\apps-up.log; backup: $backup"
        return $false
    }
    Complete-UpdApps "rolled_back"
    Write-UpdWarn "Rolled back: $id runs $(Get-UpdKV $s FROM_VERSION) again, with its data."
    return $true
}

function Remove-OldUpdAppBackups([string]$Id) {
    if (-not (Test-Path $UpdAppsBackups)) { return }
    Get-ChildItem $UpdAppsBackups -Directory -Filter "$Id-*" | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $UpdAppsKeep | ForEach-Object { Remove-Item $_.FullName -Recurse -Force }
}

function Invoke-UpdAppsApply {
    $due = "$UpdDir\apps-due.env"
    $id = Get-UpdKV $due APP_ID; $key = Get-UpdKV $due IMAGE_KEY; $ref = Get-UpdKV $due IMAGE_REF
    $ver = Get-UpdKV $due VERSION; $from = Get-UpdKV $due FROM_VERSION
    $cid = Get-UpdCid $id
    if (-not $cid) { Write-UpdWarn "$id is not running here; nothing to update."; return $true }
    # The report is from the last check: if the app was changed since (by
    # hand, or by an update), it is checked again rather than overwritten.
    $c = @(Get-UpdInspect @($cid))
    if ($c.Count -eq 0 -or $c[0].Config.Image -ne (Get-UpdKV $due FROM_REF)) {
        Write-UpdSay "$id no longer runs $(Get-UpdKV $due FROM_REF); checking again."
        $null = Invoke-UpdAppsCheck
        return $true
    }
    Write-UpdSay "Updating $id $from -> $ver ..."
    # 1. The new image onto the host first: the app is still running.
    if ((Invoke-UpdDocker @("pull", "-q", $ref) "") -ne 0) { Write-UpdFail "Could not pull $ref; nothing changed."; return $false }

    $backup = Join-Path $UpdAppsBackups "$id-$(Get-Date -Format 'yyyyMMdd-HHmmss')-$from"
    [IO.File]::WriteAllText((Join-Path (Get-Location).Path "$UpdDir\apps-state.env"), "", $Utf8NoBom)
    [IO.File]::WriteAllText((Join-Path (Get-Location).Path "$UpdDir\apps-up.log"), "", $Utf8NoBom)
    Add-UpdAppsState @("APP_ID=$id", "KEY=$key", "FROM_VERSION=$from", "VERSION=$ver", "FROM_REF=$(Get-UpdKV $due FROM_REF)",
                       "TO_REF=$ref", "BACKUP=$backup", "STARTED=$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))",
                       "STEP=stopping")
    # 2. Stopped before the backup: an app writing SQLite mid-copy leaves a
    #    backup that cannot be read back.
    $stopped = (Invoke-UpdDocker (@("compose") + $script:UpdCa + @("stop", $id)) "$UpdDir\apps-up.log") -eq 0
    if (-not $stopped -or -not (Backup-UpdApp $cid $backup (Get-UpdCoreImage))) {
        $null = Invoke-UpdDocker (@("compose") + $script:UpdCa + @("start", $id)) "$UpdDir\apps-up.log"
        Complete-UpdApps "failed"
        Write-UpdFail "The backup of $id failed; it runs $from as before."
        return $false
    }
    $old = ""
    foreach ($l in [IO.File]::ReadAllLines((Join-Path (Get-Location).Path ".env"), $Utf8NoBom)) {
        if ($l -match "^$key=") { $old = ($l -split '=', 2)[1]; break }
    }
    [IO.File]::WriteAllText((Join-Path (Resolve-Path $backup).Path "env-value"), "$old`n", $Utf8NoBom)
    # 3. The switch.
    Add-UpdAppsState @("STEP=switching")
    Set-DotEnvVar $key $ref
    $script:UpdCa = Get-ComposeArgs
    if ((Invoke-UpdDocker (@("compose") + $script:UpdCa + @("up", "-d", "--no-deps", $id)) "$UpdDir\apps-up.log") -ne 0) {
        Write-UpdFail "compose up failed for $id"
        $null = Invoke-UpdAppsRollback
        return $false
    }
    Add-UpdAppsState @("STEP=verifying")
    if (-not (Test-UpdAppHealthy $id $ref)) {
        $null = Invoke-UpdAppsRollback
        return $false
    }
    Complete-UpdApps "succeeded"
    Remove-OldUpdAppBackups $id
    Write-UpdOk "$id $ver is running and healthy."
    return $true
}

# An app update interrupted part way is finished first (see update-host.sh).
function Resume-UpdApps {
    $s = "$UpdDir\apps-state.env"
    $id = Get-UpdKV $s APP_ID
    $step = Get-UpdKV $s STEP -Last
    if ($step -eq "stopping") {
        Write-UpdWarn "An update of $id stopped during its backup; starting it again."
        $null = Invoke-UpdDocker (@("compose") + $script:UpdCa + @("up", "-d", "--no-deps", $id)) "$UpdDir\apps-up.log"
        Complete-UpdApps "failed"
    } elseif ($step -in "switching", "verifying") {
        Write-UpdWarn "An update of $id was interrupted; checking it now."
        if (Test-UpdAppHealthy $id (Get-UpdKV $s TO_REF)) { Complete-UpdApps "succeeded" } else { $null = Invoke-UpdAppsRollback }
    } elseif ($step -eq "rolling_back") {
        Write-UpdWarn "A rollback of $id was interrupted; finishing it."
        $null = Invoke-UpdAppsRollback
    }
}

#   .\start.ps1 update --apps                  check now and list what is available
#   .\start.ps1 update --apps --app attendly   install its newest release now (asks; --yes does not)
function Invoke-UpdApps([string[]]$AppsArgs) {
    $tick = $false; $app = ""; $now = $false; $yes = $false
    for ($i = 0; $i -lt $AppsArgs.Count; $i++) {
        switch ($AppsArgs[$i]) {
            "--tick" { $tick = $true }
            "--app"  { $app = $AppsArgs[++$i] }
            "--now"  { $now = $true }
            { $_ -in "--yes", "-y" } { $yes = $true }
            default  { Write-UpdFail "unknown option for --apps: $($AppsArgs[$i])"; return }
        }
    }
    if (-not (Test-Path .env)) { Write-UpdFail "No .env here: run this from the OpenNVR folder."; return }
    Lock-Update
    try {
        $script:UpdCa = Get-ComposeArgs
        $script:UpdProject = Get-UpdProject
        $script:UpdPlatform = docker version --format '{{.Server.Os}}/{{.Server.Arch}}'
        if ($LASTEXITCODE -ne 0) { Write-UpdFail "Docker is not running."; return }
        Resume-UpdApps

        $here = (Get-Location).Path
        $epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        $last = Get-UpdKV "$UpdDir\apps-check.env" CHECKED_EPOCH
        $every = if ($env:OPENNVR_APPS_CHECK_EVERY) { [long]$env:OPENNVR_APPS_CHECK_EVERY } else { 21600 }
        if (-not $tick -or -not $last -or ($epoch - [long]$last) -ge $every) {
            # A core from before app updates has no apps-check. Recorded like a
            # check, so the runner asks again in six hours, not every minute.
            if ((Invoke-Updater "" @("apps-check", "--help") -Quiet) -ne 0) {
                [IO.File]::WriteAllText((Join-Path $here "$UpdDir\apps-check.env"), "CHECKED_EPOCH=$epoch`nCODE=unsupported`n", $Utf8NoBom)
                if (-not $tick) { Write-UpdFail "The running OpenNVR ($(Get-EnvVar CORE_TAG)) predates app updates; update OpenNVR first." }
                return
            }
            if (-not $tick) { Write-UpdSay "Checking the apps for new releases ..." }
            if (-not (Invoke-UpdAppsCheck)) { return }
            if (-not $tick) { Get-Content "$UpdDir\apps-check.log" | ForEach-Object { Write-Host "  $_" } }
        }
        # Nothing on offer: no updater container a minute.
        $report = Join-Path $here "$UpdDir\apps.json"
        if (-not (Test-Path $report) -or -not ([IO.File]::ReadAllText($report) -match '"state": "available"')) { return }
        if (-not $tick -and -not $app) { Write-UpdSay "Install one with: .\start.ps1 update --apps --app <id>"; return }
        # On offer but perhaps not due for hours: asked every five minutes at most.
        if ($tick) {
            $asked = Get-UpdKV "$UpdDir\apps-due-asked.env" ASKED_EPOCH
            if ($asked -and ($epoch - [long]$asked) -lt 300) { return }
            [IO.File]::WriteAllText((Join-Path $here "$UpdDir\apps-due-asked.env"), "ASKED_EPOCH=$epoch`n", $Utf8NoBom)
        }
        $dueArgs = @("apps-due", "--policy-file", "/repo/data/update-requests/policy.env", "--local-time", (Get-Date -Format "yyyy-MM-ddTHH:mm"))
        if ($app) { $dueArgs += @("--app", $app) }
        if ($now -or -not $tick) { $dueArgs += "--now" }
        if ((Invoke-Updater "" $dueArgs -LogFile "$UpdDir\apps-due.log") -ne 0) {
            if (-not $tick) { Get-Content "$UpdDir\apps-due.log" | ForEach-Object { Write-Host "  $_" } }
            return
        }
        $due = "$UpdDir\apps-due.env"
        if (-not $tick -and -not $yes) {
            Write-Host ""
            Write-UpdSay "$(Get-UpdKV $due APP_ID) stops for about a minute; its data is backed up first,"
            Write-UpdSay "and if $(Get-UpdKV $due VERSION) does not come up healthy it goes back to $(Get-UpdKV $due FROM_VERSION) with its data."
            $answer = Read-Host "  Update $(Get-UpdKV $due APP_ID) now? [y/N]"
            if ($answer -notmatch '^[Yy]$') { Write-UpdSay "Nothing changed."; return }
        }
        $null = Invoke-UpdAppsApply
        # What runs now is not what the report says: refresh it, or the next
        # tick would offer the release just installed (or rolled back) again.
        $null = Invoke-UpdAppsCheck
    } finally {
        Unlock-Update
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
            "--apps"          {
                $rest = if ($i + 1 -lt $UpdateArgs.Count) { $UpdateArgs[($i + 1)..($UpdateArgs.Count - 1)] } else { @() }
                Invoke-UpdApps $rest
                return
            }
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
