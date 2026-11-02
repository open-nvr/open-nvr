# shellcheck shell=bash
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The host side of the OpenNVR updater: `./start.sh update`.
# (docs/design/auto-update-implementation-plan.md; the decisions live in
# server/updater, run inside a core image; this file only does what must
# happen on the host: git, docker pull/load/tag, backups, compose up.)
#
# Sourced by start.sh, which provides compose_args, run_up, get_env_var,
# write_env_var and the colours. start.sh runs with `set -e`; every step
# here that can fail is checked explicitly, so a failure after the switch
# always reaches the rollback instead of ending the script half-way.
#
#   ./start.sh update                 check, show the plan, ask, apply
#   ./start.sh update --to 0.1.9      a specific release instead of the latest
#   ./start.sh update --bundle F.tar  an offline bundle (air-gapped sites)
#   ./start.sh update --dry-run       check and show the plan only
#   ./start.sh update --yes           do not ask (for the scheduled runner)
#   ./start.sh update --adopt         take over a site not on a known release
#   ./start.sh update --accept-manual the release's manual steps are done
#   ./start.sh update --restore DIR   put a backup back (the escape hatch)
#   ./start.sh update --from-queue    one tick of the runner (the timer runs it)
#   ./start.sh update --list-images   this site's images (for make-offline-bundle.py)
#   sudo ./start.sh auto-update enable|disable|status   that timer
#
# Files: data/updates/ (plan, state, the installed and candidate manifests)
# and backups/<time>-<version>/ (database dump, two volumes, .env); both
# are git-ignored and private to this user.

UPD_DIR="data/updates"
UPD_BACKUPS="backups"
UPD_KEEP_BACKUPS="${OPENNVR_UPDATE_KEEP_BACKUPS:-3}"
# Small volumes whose contents are not in the database (plan F10).
UPD_VOLUMES="opennvr_jwt_keys opennvr_kai_c_state"

upd_say()  { echo -e "  $*"; }
upd_ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
upd_warn() { echo -e "  ${YELLOW}!${NC} $*"; }
upd_fail() { echo -e "  ${RED}✗ $*${NC}" >&2; }

# KEY=value files written by the updater (values are plain tokens).
upd_kv() { grep -E "^$2=" "$1" 2>/dev/null | head -1 | cut -d= -f2-; }

upd_state() {
    # Append KEY=value lines to state.env (the last one wins when read).
    local kv
    for kv in "$@"; do echo "$kv" >> "$UPD_DIR/state.env"; done
}

# A run is over: its last step, and one line in history.log for the app.
upd_end() {
    local finished
    finished=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    upd_state "STEP=$1" "FINISHED=$finished"
    echo "$finished $(upd_kv "$UPD_DIR/state.env" FROM) $(upd_kv "$UPD_DIR/state.env" TO) $1" >> "$UPD_DIR/history.log"
}

# A service's running container, found through compose rather than by its
# container_name, so a stack started under another project name works too.
upd_cid() {
    # shellcheck disable=SC2086
    docker compose $ARGS ps -q "$1" 2>/dev/null | head -1
}

upd_project() {
    local p
    p=$(docker inspect "$(upd_cid opennvr-core)" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)
    [ -n "$p" ] || p="${COMPOSE_PROJECT_NAME:-$(basename "$PWD" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')}"
    echo "$p"
}

# Run the updater in a core image. CORE_TAG in the environment wins over
# .env for compose's interpolation, so `upd_run 0.1.9 …` uses that image.
upd_run() {
    local tag="$1"; shift
    local user=()
    [ "$(uname -s)" = "Linux" ] && user=(--user "$(id -u):$(id -g)")
    local env_prefix=()
    [ -n "$tag" ] && env_prefix=(env "CORE_TAG=$tag")
    # A mirror (UPDATE_SOURCE_URL in .env), and extra trusted keys for
    # testing the whole flow with a release you signed yourself
    # (OPENNVR_UPDATE_EXTRA_KEYS=<keys.json> in the shell running this).
    local pass=() src
    # The shell's UPDATE_SOURCE_URL wins over the one in .env.
    src="${UPDATE_SOURCE_URL:-$(get_env_var UPDATE_SOURCE_URL)}"
    [ -n "$src" ] && pass+=(-e "UPDATE_SOURCE_URL=$src")
    if [ -n "${OPENNVR_UPDATE_EXTRA_KEYS:-}" ] && [ -f "$OPENNVR_UPDATE_EXTRA_KEYS" ]; then
        cp "$OPENNVR_UPDATE_EXTRA_KEYS" "$UPD_DIR/extra-keys.json"
        pass+=(-e "OPENNVR_UPDATE_EXTRA_KEYS=/work/extra-keys.json")
    fi
    # shellcheck disable=SC2086  # $ARGS is a word list by design (compose_args)
    COMPOSE_IGNORE_ORPHANS=True "${env_prefix[@]}" docker compose --progress quiet $ARGS run --rm --no-deps -T "${user[@]}" "${pass[@]}" \
        -v "$PWD:/repo:ro" -v "$PWD/$UPD_DIR:/work" \
        --entrypoint python opennvr-core -m updater "$@"
}

upd_running_images() {
    local ids
    ids=$(docker ps -q --filter "label=com.docker.compose.project=$PROJECT")
    [ -n "$ids" ] || return 0
    # shellcheck disable=SC2086
    docker inspect --format '{{.Name}} {{.Image}} {{.Config.Image}}' $ids
}

upd_lock() {
    mkdir -p "$UPD_DIR"
    # Readable by core (mounted read-only into it for the app); it holds
    # manifests, plans and run state, no secrets: the backups are elsewhere.
    chmod 755 "$UPD_DIR" 2>/dev/null || true
    if ! mkdir "$UPD_DIR/.lock" 2>/dev/null; then
        local pid
        pid=$(cat "$UPD_DIR/.lock/pid" 2>/dev/null || true)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            upd_fail "Another update is running (pid $pid)."
            exit 1
        fi
        upd_warn "Taking over a stale update lock."
    fi
    echo $$ > "$UPD_DIR/.lock/pid"
    trap 'rm -rf "$UPD_DIR/.lock"' EXIT
}

# ── backups ──────────────────────────────────────────────────────────

upd_backup() {
    local dir="$1"
    mkdir -p "$dir" && chmod 700 "$dir" || return 1
    upd_say "Backing up the database ..."
    local db
    db=$(upd_cid db)
    [ -n "$db" ] || { upd_fail "the database container is not running"; return 1; }
    # Dumped inside the container and copied out: never through a pipe
    # that might re-encode a binary archive.
    docker exec "$db" sh -c 'pg_dump -U "$POSTGRES_USER" -Fc -d "$POSTGRES_DB" -f /tmp/opennvr-update.dump' || return 1
    docker cp "$db:/tmp/opennvr-update.dump" "$dir/db.dump" >/dev/null || return 1
    docker exec "$db" rm -f /tmp/opennvr-update.dump || true
    [ "$(head -c 5 "$dir/db.dump")" = "PGDMP" ] || { upd_fail "the database dump is not a valid archive"; return 1; }
    local v name image
    image=$(docker inspect "$(upd_cid opennvr-core)" --format '{{.Config.Image}}' 2>/dev/null) || return 1
    for v in $UPD_VOLUMES; do
        name=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.volume=$v")
        [ -n "$name" ] || continue
        docker run --rm -v "$name:/v:ro" -v "$PWD/$dir:/b" --entrypoint tar "$image" czf "/b/$v.tgz" -C /v . || return 1
    done
    cp .env "$dir/env" || return 1
    chmod 600 "$dir"/* 2>/dev/null || true
    upd_ok "Backup in $dir ($(du -sh "$dir" 2>/dev/null | cut -f1))"
}

upd_restore() {
    # Put a backup back: the database (schema dropped first, so tables a
    # newer release added do not linger), the volumes and .env.
    local dir="$1" image="$2"
    [ -f "$dir/db.dump" ] || { upd_fail "$dir has no db.dump"; return 1; }
    upd_say "Restoring the database from $dir ..."
    # shellcheck disable=SC2086
    docker compose $ARGS stop opennvr-core >/dev/null 2>&1 || true
    local db
    db=$(upd_cid db)
    [ -n "$db" ] || { upd_fail "the database container is not running"; return 1; }
    docker cp "$dir/db.dump" "$db:/tmp/opennvr-restore.dump" >/dev/null || return 1
    docker exec "$db" sh -c '
        set -e
        export PGOPTIONS="-c client_min_messages=warning"
        psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -q \
            -c "DROP SCHEMA public CASCADE" -c "CREATE SCHEMA public"
        pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error /tmp/opennvr-restore.dump
        rm -f /tmp/opennvr-restore.dump' || return 1
    local v name
    for v in $UPD_VOLUMES; do
        [ -f "$dir/$v.tgz" ] || continue
        name=$(docker volume ls -q --filter "label=com.docker.compose.project=$PROJECT" --filter "label=com.docker.compose.volume=$v")
        [ -n "$name" ] || continue
        docker run --rm -v "$name:/v" -v "$PWD/$dir:/b:ro" --entrypoint sh "$image" \
            -c "find /v -mindepth 1 -delete && tar xzf /b/$v.tgz -C /v" || return 1
    done
    upd_ok "Database and volumes restored."
}

upd_prune_backups() {
    local keep="$UPD_KEEP_BACKUPS" old
    # Newest first; everything past the newest $keep goes.
    # shellcheck disable=SC2012
    old=$(ls -1dt "$UPD_BACKUPS"/*/ 2>/dev/null | tail -n +"$((keep + 1))")
    [ -n "$old" ] || return 0
    echo "$old" | while read -r d; do rm -rf "$d"; done
}

# ── getting the release onto the host (no downtime yet) ─────────────

upd_fetch_commit() {
    local tag="$1" commit="$2" bundle="$3"
    if [ -z "$tag" ]; then
        # An edge build: an untagged main commit, fetched by its hash, which
        # is the signed manifest's own (so nothing else can stand in for it).
        git fetch -q origin "$commit" || return 1
        git cat-file -e "$commit^{commit}" || return 1
        return 0
    fi
    if [ -n "$bundle" ]; then
        git fetch -q "$bundle/repo.bundle" "refs/tags/$tag:refs/tags/$tag" || return 1
    else
        git fetch -q origin "refs/tags/$tag:refs/tags/$tag" || return 1
    fi
    local got
    got=$(git rev-parse "$tag^{commit}" 2>/dev/null)
    if [ "$got" != "$commit" ]; then
        upd_fail "tag $tag is commit $got, but the signed manifest says $commit"
        return 1
    fi
}

upd_fetch_images() {
    local bundle="$1" ref digest accept repo id
    if [ -n "$bundle" ]; then
        upd_say "Loading the images from the bundle ..."
        docker load -q -i "$bundle/images.tar" >/dev/null || return 1
    fi
    while read -r ref digest accept; do
        [ -n "$ref" ] || continue
        repo="${ref%:*}"
        if [ -z "$bundle" ]; then
            upd_say "Pulling $ref ..."
            docker pull -q "$repo@$digest" >/dev/null || return 1
            docker tag "$repo@$digest" "$ref" || return 1
        fi
        id=$(docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null || true)
        if [ -z "$id" ]; then
            # A delta bundle leaves out what the site should already have.
            upd_fail "$ref is neither in the bundle nor on this host: make a full bundle (no --since)"
            return 1
        fi
        case ",$accept," in
            *",$id,"*) ;;
            *) upd_fail "$ref is $id, not the image the release signed"; return 1 ;;
        esac
    done < "$UPD_DIR/images.txt"
    upd_ok "All release images are here and match the signed manifest."
}

# ── the switch, verification, rollback ───────────────────────────────

upd_up() {
    # start.sh's own `up`, without --remove-orphans (it would remove the
    # containers this site runs from overlays compose was not told about)
    # and rebuilding the images the release builds from its own files --
    # except from a bundle: an air-gapped site cannot build (that needs the
    # internet), and the bundle brings whatever it must use instead.
    # In a subshell: run_up exits on failure, and we must reach the rollback.
    ( KEEP_ORPHANS=1 UP_BUILD="${UPD_BUILD:-1}" run_up ) >"$UPD_DIR/up.log" 2>&1
}

upd_verify() {
    local manifest_dir="$1"
    upd_running_images > "$UPD_DIR/running.txt"
    # The image now named in .env runs the check: the release verifies itself.
    upd_run "" verify --manifest-dir "/work/$manifest_dir" --running /work/running.txt \
        --platform "$PLATFORM" --timeout "${OPENNVR_UPDATE_VERIFY_TIMEOUT:-300}"
}

upd_rollback() {
    local from_commit backup restore_db mode
    from_commit=$(upd_kv "$UPD_DIR/plan.env" FROM_COMMIT)
    restore_db=$(upd_kv "$UPD_DIR/plan.env" DB_RESTORE_ON_ROLLBACK)
    mode=$(upd_kv "$UPD_DIR/plan.env" ROLLBACK)
    backup=$(upd_kv "$UPD_DIR/state.env" BACKUP)
    upd_state STEP=rolling_back
    if [ "$mode" = "manual" ]; then
        upd_fail "This release changes data outside the database, so it is not rolled back automatically."
        upd_say "The stack is left as it is. Your backup: $backup"
        upd_say "To go back anyway: ./start.sh update --restore $backup"
        upd_end failed
        return 1
    fi
    upd_warn "Rolling back to $(upd_kv "$UPD_DIR/plan.env" FROM_VERSION) ..."
    git checkout -q -B opennvr-release "$from_commit" || { upd_end failed; return 1; }
    cp "$backup/env" .env || { upd_end failed; return 1; }
    ARGS=$(compose_args) || { upd_end failed; return 1; }
    local old_image
    old_image="ghcr.io/open-nvr/core:$(get_env_var CORE_TAG)"
    if [ "$restore_db" = "1" ]; then
        upd_restore "$backup" "$old_image" || { upd_end failed; return 1; }
    fi
    if ! upd_up; then
        upd_end failed
        upd_fail "The old version did not start either. See $UPD_DIR/up.log; backup: $backup"
        return 1
    fi
    if [ -f "$UPD_DIR/installed/release-manifest.json" ] && ! upd_verify installed; then
        upd_end failed
        upd_fail "Rolled back, but the old version does not check out healthy. Backup: $backup"
        return 1
    fi
    upd_end rolled_back
    upd_warn "Rolled back: the site runs $(upd_kv "$UPD_DIR/plan.env" FROM_VERSION) again, with its data."
    return 0
}

upd_finish() {
    upd_end succeeded
    rm -rf "$UPD_DIR/installed"
    mv "$UPD_DIR/candidate" "$UPD_DIR/installed"
    upd_prune_backups
    upd_ok "OpenNVR $(upd_kv "$UPD_DIR/plan.env" TO_VERSION) is running and healthy."
}

upd_switch_and_verify() {
    upd_state STEP=switching
    local commit
    commit=$(upd_kv "$UPD_DIR/plan.env" GIT_COMMIT)
    if ! git checkout -q -B opennvr-release "$commit"; then
        upd_fail "could not check out the release"
        upd_rollback; return 1
    fi
    local kv
    while IFS= read -r kv; do
        [ -n "$kv" ] && write_env_var "${kv%%=*}" "${kv#*=}"
    done < "$UPD_DIR/pins.env"
    if ! ARGS=$(compose_args); then
        upd_rollback; return 1
    fi
    upd_say "Starting the new version (log: $UPD_DIR/up.log) ..."
    if ! upd_up; then
        upd_fail "compose up failed"
        upd_rollback; return 1
    fi
    upd_state STEP=verifying
    if ! upd_verify candidate; then
        upd_rollback; return 1
    fi
    upd_finish
}

# ── entry points ─────────────────────────────────────────────────────

upd_restore_cmd() {
    local dir="$1"
    [ -d "$dir" ] || { upd_fail "no backup at $dir"; exit 1; }
    [ -f "$dir/db.dump" ] && [ -f "$dir/env" ] || { upd_fail "$dir is not a complete backup"; exit 1; }
    upd_lock
    PROJECT=$(upd_project)
    ARGS=$(compose_args) || exit 1
    # Everything checked before anything changes: a refusal must not leave
    # the backup's .env in place over a stack still running another release.
    [ -n "$(upd_cid db)" ] || { upd_fail "the database container is not running; start the stack first"; exit 1; }
    cp "$dir/env" .env || exit 1
    ARGS=$(compose_args) || exit 1
    upd_restore "$dir" "ghcr.io/open-nvr/core:$(get_env_var CORE_TAG)" || exit 1
    upd_say "Note: the OpenNVR folder is not moved back; check out the matching release yourself if needed."
    upd_up || { upd_fail "compose up failed; see $UPD_DIR/up.log"; exit 1; }
    upd_ok "Restored from $dir."
}

# ── the runner: one tick a minute, from the timer ────────────────────
#
# Leaves a heartbeat for the app; starts an "Update now" asked for in the
# app; otherwise, unless updates are off, checks for an update every few
# hours, and in auto mode installs it when updater/policy.py says it is due.

upd_queue() {
    mkdir -p "$UPD_DIR" data/update-requests
    chmod 755 "$UPD_DIR" 2>/dev/null || true
    echo "LAST_SEEN=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$UPD_DIR/runner.env"
    # Someone (or the last tick) is updating: nothing to do this minute.
    if [ -d "$UPD_DIR/.lock" ] && kill -0 "$(cat "$UPD_DIR/.lock/pid" 2>/dev/null)" 2>/dev/null; then
        return 0
    fi
    local req=data/update-requests/request.env policy=data/update-requests/policy.env id handled
    id=$(upd_kv "$req" ID)
    handled=$(cat "$UPD_DIR/last-request" 2>/dev/null || true)
    if [ -n "$id" ] && [ "$id" != "$handled" ]; then
        # Marked first: a request is tried once, never in a loop.
        echo "$id" > "$UPD_DIR/last-request"
        run_update --to "$(upd_kv "$req" TO)" --yes
        return
    fi
    local mode last now
    mode=$(upd_kv "$policy" MODE)
    [ -n "$mode" ] && [ "$mode" != off ] || return 0
    last=$(upd_kv "$UPD_DIR/check.env" CHECKED_EPOCH)
    now=$(date +%s)
    if [ -z "$last" ] || [ $((now - last)) -ge "${OPENNVR_UPDATE_CHECK_EVERY:-21600}" ]; then
        ( run_update --dry-run ) || true
    fi
    [ "$mode" = auto ] && [ "$(upd_kv "$UPD_DIR/check.env" CODE)" = 0 ] || return 0
    ARGS=$(compose_args) || return 1
    if upd_run "" due --policy-file /repo/$policy --local-time "$(date +%Y-%m-%dT%H:%M)"; then
        run_update --to "$(upd_kv "$UPD_DIR/plan.env" TO_VERSION)" --yes
    fi
}

# ./start.sh auto-update enable|disable|status: the timer that runs the tick.
run_auto_update() {
    local unit=/etc/systemd/system/opennvr-update
    local plist="$HOME/Library/LaunchAgents/org.opennvr.update.plist"
    case "${1:-status}:$(uname -s)" in
        enable:Linux)
            [ "$(id -u)" = 0 ] || { upd_fail "Run this with sudo: it installs a systemd timer."; exit 1; }
            local run_as="${SUDO_USER:-$(id -un)}"
            cat > "$unit.service" <<EOF
[Unit]
Description=OpenNVR update runner (one tick)
After=docker.service

[Service]
Type=oneshot
User=$run_as
WorkingDirectory=$PWD
ExecStart=$PWD/start.sh update --from-queue
EOF
            cat > "$unit.timer" <<EOF
[Unit]
Description=OpenNVR update runner, every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF
            systemctl daemon-reload && systemctl enable --now opennvr-update.timer || exit 1
            upd_ok "The update runner checks in every minute, as $run_as. The update policy is set in the app."
            ;;
        disable:Linux)
            [ "$(id -u)" = 0 ] || { upd_fail "Run this with sudo."; exit 1; }
            systemctl disable --now opennvr-update.timer 2>/dev/null || true
            rm -f "$unit.service" "$unit.timer"; systemctl daemon-reload
            upd_ok "The update runner is off. Updates can still be run by hand: ./start.sh update"
            ;;
        enable:Darwin)
            mkdir -p "$(dirname "$plist")"
            cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.opennvr.update</string>
  <key>ProgramArguments</key><array><string>$PWD/start.sh</string><string>update</string><string>--from-queue</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>StartInterval</key><integer>60</integer>
  <key>StandardOutPath</key><string>$PWD/$UPD_DIR/runner.log</string>
  <key>StandardErrorPath</key><string>$PWD/$UPD_DIR/runner.log</string>
</dict></plist>
EOF
            launchctl unload "$plist" 2>/dev/null || true
            launchctl load -w "$plist" || exit 1
            upd_ok "The update runner checks in every minute. The update policy is set in the app."
            ;;
        disable:Darwin)
            launchctl unload -w "$plist" 2>/dev/null || true
            rm -f "$plist"
            upd_ok "The update runner is off. Updates can still be run by hand: ./start.sh update"
            ;;
        status:*)
            local seen
            seen=$(upd_kv "$UPD_DIR/runner.env" LAST_SEEN)
            if [ -n "$seen" ]; then upd_say "The update runner last checked in at $seen (UTC)."
            else upd_say "The update runner has never run here. Turn it on with: sudo ./start.sh auto-update enable"; fi
            ;;
        *) upd_fail "usage: ./start.sh auto-update enable|disable|status (Linux and macOS)"; exit 1 ;;
    esac
}

run_update() {
    local to="" bundle_file="" dry=0 yes=0 extra=() restore=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --to) to="$2"; shift 2 ;;
            --bundle) bundle_file="$2"; shift 2 ;;
            --dry-run) dry=1; shift ;;
            --yes|-y) yes=1; shift ;;
            --adopt) extra+=(--adopt); shift ;;
            --accept-manual) extra+=(--accept-manual); shift ;;
            --channel) extra+=(--channel "$2"); shift 2 ;;
            --restore) restore="$2"; shift 2 ;;
            --from-queue) upd_queue; return ;;
            --list-images)
                # For scripts/make-offline-bundle.py: what this site runs.
                ARGS=$(compose_args) || exit 1
                # shellcheck disable=SC2086
                docker compose $ARGS config --images
                return ;;
            *) upd_fail "unknown option: $1"; exit 1 ;;
        esac
    done
    [ -f .env ] || { upd_fail "No .env here: run this from the OpenNVR folder."; exit 1; }
    # Git's own words: "not a git repository" and "dubious ownership" (the
    # folder belongs to another user, e.g. under sudo) need different fixes.
    local git_err
    if ! git_err=$(git rev-parse --git-dir 2>&1 >/dev/null); then
        upd_fail "git cannot use this OpenNVR folder:"
        printf '%s\n' "$git_err" | sed 's/^/    /' >&2
        exit 1
    fi
    if [ -n "$restore" ]; then upd_restore_cmd "$restore"; return; fi

    upd_lock
    PROJECT=$(upd_project)
    ARGS=$(compose_args) || exit 1
    PLATFORM=$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}') || { upd_fail "Docker is not running."; exit 1; }

    # An update that was interrupted after the switch is finished first.
    local step
    step=$(grep -E '^STEP=' "$UPD_DIR/state.env" 2>/dev/null | tail -1 | cut -d= -f2)
    case "$step" in
        switching|verifying)
            upd_warn "An update to $(upd_kv "$UPD_DIR/plan.env" TO_VERSION) was interrupted; checking it now."
            if upd_verify candidate; then upd_finish; else upd_rollback || exit 1; fi
            return ;;
        rolling_back)
            upd_warn "A rollback was interrupted; finishing it."
            upd_rollback || exit 1
            return ;;
    esac

    # The running release must carry the updater: one from before it has
    # neither this command's other half nor its rules.
    if ! upd_run "" --help >/dev/null 2>&1; then
        upd_fail "The running OpenNVR ($(get_env_var CORE_TAG)) predates the updater."
        upd_say "Move to the first release that has it by hand, once:"
        upd_say "DOCKER_QUICKSTART.md, \"Upgrading to a new release\". After that, ./start.sh update works."
        exit 1
    fi

    # Facts only the host can see, for the updater's preflight.
    git status --porcelain --untracked-files=no | cut -c4- > "$UPD_DIR/changed.txt"
    # shellcheck disable=SC2086
    docker compose $ARGS config --images > "$UPD_DIR/site-images.txt" 2>/dev/null || { upd_fail "compose config failed"; exit 1; }
    local bundle=""
    if [ -n "$bundle_file" ]; then
        [ -f "$bundle_file" ] || { upd_fail "no bundle at $bundle_file"; exit 1; }
        rm -rf "$UPD_DIR/bundle" && mkdir -p "$UPD_DIR/bundle"
        upd_say "Unpacking $bundle_file ..."
        tar -xf "$bundle_file" -C "$UPD_DIR/bundle" || { upd_fail "the bundle could not be unpacked"; exit 1; }
        bundle="$UPD_DIR/bundle"
        extra+=(--bundle /work/bundle)
        UPD_BUILD=0
    fi
    [ -n "$to" ] && extra+=(--to "$to")

    upd_say "Checking for an update ..."
    local code=0
    upd_run "" check --platform "$PLATFORM" --head "$(git rev-parse HEAD)" \
        --changed-files /work/changed.txt --site-images /work/site-images.txt \
        --core-image-id "$(docker inspect --format '{{.Image}}' "$(upd_cid opennvr-core)" 2>/dev/null || true)" \
        "${extra[@]}" || code=$?
    case "$code" in
        0) ;;
        10) return 0 ;;
        *) exit "$code" ;;
    esac
    [ "$dry" = 1 ] && { upd_say "Dry run: nothing changed."; return 0; }

    local from to_v tag commit
    from=$(upd_kv "$UPD_DIR/plan.env" FROM_VERSION)
    to_v=$(upd_kv "$UPD_DIR/plan.env" TO_VERSION)
    tag=$(upd_kv "$UPD_DIR/plan.env" GIT_TAG)
    commit=$(upd_kv "$UPD_DIR/plan.env" GIT_COMMIT)
    if [ "$yes" != 1 ]; then
        echo ""
        upd_say "The stack restarts the services that change (about a minute). A backup is taken first,"
        upd_say "and if $to_v does not come up healthy the site goes back to $from with its data."
        upd_say "Anything recorded or changed during the update is lost if it rolls back."
        read -rp "  Update OpenNVR $from -> $to_v now? [y/N]: " answer
        [[ "$answer" =~ ^[Yy]$ ]] || { upd_say "Nothing changed."; return 0; }
    fi

    # A mirror (UPDATE_SOURCE_URL) that serves bundles, as one made by
    # scripts/make-offline-bundle.py --mirror-dir does: this platform's
    # bundle brings the release's files and images, so an air-gapped
    # network needs no registry. Without one, images come from registries.
    local src_url="${UPDATE_SOURCE_URL:-$(get_env_var UPDATE_SOURCE_URL)}"
    if [ -z "$bundle" ] && [ -n "$src_url" ]; then
        local url="${src_url%/}/v$to_v/opennvr-${PLATFORM//\//-}-offline.tar"
        upd_say "Fetching the bundle from the mirror ..."
        if curl -fsSL -o "$UPD_DIR/mirror-bundle.tar" "$url"; then
            rm -rf "$UPD_DIR/bundle" && mkdir -p "$UPD_DIR/bundle"
            tar -xf "$UPD_DIR/mirror-bundle.tar" -C "$UPD_DIR/bundle" || { upd_fail "the mirror's bundle could not be unpacked"; exit 1; }
            rm -f "$UPD_DIR/mirror-bundle.tar"
            bundle="$UPD_DIR/bundle"
            UPD_BUILD=0
        else
            upd_say "No bundle for $PLATFORM on the mirror; images come from their registries."
        fi
    fi

    # 1. Everything onto the host first: nothing has stopped yet.
    upd_fetch_commit "$tag" "$commit" "$bundle" || { upd_fail "Could not get the release's files; nothing changed."; exit 1; }
    upd_fetch_images "$bundle" || { upd_fail "Could not get the release's images; nothing changed."; exit 1; }

    # 2. Back up, then switch.
    : > "$UPD_DIR/state.env"
    chmod 600 "$UPD_DIR/state.env" 2>/dev/null || true
    local backup
    backup="$UPD_BACKUPS/$(date +%Y%m%d-%H%M%S)-$from"
    upd_state "FROM=$from" "TO=$to_v" "STARTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)" STEP=backing_up "BACKUP=$backup"
    if ! upd_backup "$backup"; then
        upd_end failed
        upd_fail "The backup failed; nothing changed."
        exit 1
    fi
    upd_switch_and_verify || exit 1
}
