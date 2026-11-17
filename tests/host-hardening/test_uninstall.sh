#!/usr/bin/env bash
# Regression tests for scripts/uninstall.sh and scripts/uninstall.ps1, driving
# the real scripts against a throwaway folder and a fake `docker`.
#
# The contract that matters most: the machine runs other things, and an
# OpenNVR uninstall removes NOTHING of theirs. The fixture therefore always
# holds a neighbouring stack, including the two near misses: a stranger sharing
# OpenNVR's compose project name, and base images OpenNVR also uses.
#
# The second contract: it is a clean slate. Every installed app, its volumes,
# the footage, the backups and the host agent go with the install, and only
# the typed confirmation (or --yes) lets any of it go.
#
# Never touches real Docker: every docker, sudo, systemctl, nft and uname call
# resolves to a shim in the throwaway folder.
#
# Run with: bash tests/host-hardening/test_uninstall.sh
set -u
set -o pipefail   # run_ps1 pipes through tr; the script's exit code must survive it

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

TESTS_RUN=0
TESTS_FAILED=0
start_test() { TESTS_RUN=$((TESTS_RUN + 1)); printf "  [%2d] %s ... " "$TESTS_RUN" "$1"; }
pass() { echo "PASS"; }
fail() { echo "FAIL"; echo "      $1"; TESTS_FAILED=$((TESTS_FAILED + 1)); }

PHRASE="uninstall and remove all data and recordings"

# ── the shims ────────────────────────────────────────────────────────
BIN="$WORK/bin"
mkdir -p "$BIN"
cat > "$BIN/docker" <<'SHIM'
#!/usr/bin/env bash
S="$FAKE_DOCKER_STATE"
printf '%s\n' "$*" >> "$S/calls.log"
fmt=""; filter=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
    [ "${args[$i]}" = "--format" ] && fmt="${args[$((i + 1))]}"
    [ "${args[$i]}" = "--filter" ] && filter="${args[$((i + 1))]}"
done
drop() { grep -vE "^$(printf '%s' "$2" | sed 's/[.[\*^$/]/\\&/g')(\||$)" "$1" > "$1.new"; mv "$1.new" "$1"; }
list_labelled() {   # name|project rows, filtered the way docker does it
    local want="${filter#label=com.docker.compose.project=}" n p
    while IFS='|' read -r n p; do
        [ "$p" = "$want" ] && printf '%s\n' "$n"
    done < "$1"
}
case "$1" in
    info) [ ! -f "$S/daemon-down" ]; exit ;;
    ps)
        while IFS='|' read -r n p w i; do
            case "$fmt" in
                *Labels*) printf '%s|%s|com.docker.compose.project=%s,com.docker.compose.project.working_dir=%s,com.docker.compose.service=x\n' "$n" "$i" "$p" "$w" ;;
                *) printf '%s|%s|%s|%s\n' "$n" "$p" "$w" "$i" ;;
            esac
        done < "$S/containers"
        ;;
    stop) ;;
    rm) for a in "${args[@]:1}"; do case "$a" in -*) ;; *) drop "$S/containers" "$a" ;; esac; done ;;
    volume)
        case "$2" in
            ls) list_labelled "$S/volumes" ;;
            inspect) cut -d'|' -f1 "$S/volumes" | grep -qxF -- "$3"; exit ;;
            rm) [ -f "$S/volume-rm-fails" ] && exit 1; drop "$S/volumes" "$3" ;;
        esac
        ;;
    network)
        case "$2" in
            ls) list_labelled "$S/networks" ;;
            rm) drop "$S/networks" "$3" ;;
        esac
        ;;
    images)
        if [ -n "$filter" ]; then src=$(list_labelled "$S/image-labels"); else src=$(cat "$S/images"); fi
        while IFS= read -r n; do
            [ -n "$n" ] || continue
            row="$n"; [[ "$fmt" == *ID* ]] && row="$row|abc123"; [[ "$fmt" == *Size* ]] && row="$row|1.5GB"
            printf '%s\n' "$row"
        done <<< "$src"
        ;;
    image)
        # Like Docker: no removal while a container runs the image.
        if cut -d'|' -f4 "$S/containers" | grep -qxF -e "$3" -e "${3%:latest}"; then exit 1; fi
        drop "$S/images" "$3"
        ;;
    run) ;;
    *) echo "fake docker: unexpected: $*" >&2; exit 99 ;;
esac
exit 0
SHIM
printf '#!/usr/bin/env bash\necho Linux\n' > "$BIN/uname"
printf '#!/usr/bin/env bash\nprintf "sudo %%s\\n" "$*" >> "$FAKE_DOCKER_STATE/calls.log"\n"$@"\n' > "$BIN/sudo"
printf '#!/usr/bin/env bash\nprintf "systemctl %%s\\n" "$*" >> "$FAKE_DOCKER_STATE/calls.log"\n' > "$BIN/systemctl"
printf '#!/usr/bin/env bash\nprintf "nft %%s\\n" "$*" >> "$FAKE_DOCKER_STATE/calls.log"\nexit 0\n' > "$BIN/nft"
chmod +x "$BIN"/*
BASH_WIN=""
if command -v powershell.exe >/dev/null 2>&1 && command -v cygpath >/dev/null 2>&1; then
    BASH_WIN="$(cygpath -w "$(command -v bash)")"
    # PowerShell finds docker.ps1 first on PATH; it hands over to the shim above.
    cat > "$BIN/docker.ps1" <<'SHIM'
& $env:FAKE_BASH (Join-Path $PSScriptRoot "docker") @args
exit $LASTEXITCODE
SHIM
fi

# ── a site: this install, its apps, beside a neighbouring stack ──────
#   fixture <workdir label to record>   → sets SITE, REC, STATE
# The apps the catalog installs run under the same compose project. On
# Windows the nested compose records the Docker VM's path as the working
# dir, not the host's; opennvr_loitering_detection below stands for that.
fixture() {
    local n=$((${FIXTURES:-0} + 1)); FIXTURES=$n
    SITE="$WORK/s$n/mysite"; REC="$WORK/s$n/footage"; STATE="$WORK/s$n/state"
    mkdir -p "$SITE/scripts" "$REC" "$STATE"
    cp "$REPO_ROOT/scripts/uninstall.sh" "$REPO_ROOT/scripts/uninstall.ps1" "$SITE/scripts/"
    touch "$SITE/start.sh" "$SITE/start.ps1" "$SITE/.env.example"
    # What this install pins: postgres (a neighbour runs it too), alpine (nobody
    # else), nats (not on this machine), and its own images.
    printf 'services:\n  db:\n    image: postgres:15-alpine\n  helper:\n    image: "alpine:3.20"  # certs\n  bus:\n    image: nats:2-alpine\n  core:\n    image: ${CORE_TAG_IMAGE:-ghcr.io/open-nvr/core:${CORE_TAG:-latest}}\n' > "$SITE/docker-compose.yml"
    printf 'POSTGRES_PASSWORD=x\nRECORDINGS_PATH=%s\n' "${REC_LABEL:-$REC}" > "$SITE/.env"
    printf 'keep me\n' > "$SITE/.env.bak.mine"
    mkdir -p "$SITE/nginx-certs" "$SITE/mediamtx-certs" "$SITE/data/updates" "$SITE/data/update-requests" \
             "$SITE/data/net-hints" "$SITE/data/host-agent" "$SITE/data/host-agent-key" "$SITE/data/fake-cameras" \
             "$SITE/bin" "$SITE/backups/20260101-0.1.0" "$SITE/server" \
             "$REC/cam-1/2026-11-01" "$REC/cam-12" "$REC/orphaned/old" "$REC/.evidence" "$REC/holiday-photos"
    touch "$SITE/nginx-certs/a.pem" "$SITE/data/fake-cameras/demo1.mp4" "$SITE/backups/20260101-0.1.0/db.dump" \
          "$SITE/data/host-agent/agent.key.sha256" "$SITE/data/host-agent-key/agent.key" \
          "$SITE/bin/opennvr-host" "$SITE/bin/opennvr-host.exe" "$SITE/bin/opennvr-host.build" "$SITE/bin/my-tool" \
          "$REC/cam-1/2026-11-01/a.mp4" "$REC/holiday-photos/beach.jpg" "$REC/notes.txt"
    local wd="${1:-$SITE}"
    cat > "$STATE/containers" <<EOF
opennvr_db|mysite|$wd|postgres:15-alpine
opennvr_core|mysite|$wd|ghcr.io/open-nvr/core:0.1.10
opennvr_app_installer|mysite|$wd|opennvr/app-installer:local
opennvr_attendly|mysite|$wd|ghcr.io/open-nvr/attendly:0.3.0
opennvr_loitering_detection|mysite|/run/desktop/mnt/host/d/mysite|ghcr.io/open-nvr/loitering-detection:0.1.10
mysite-whisper-1|mysite|$wd|mysite-whisper
my_postgres|webapp|/srv/webapp|postgres:15-alpine
my_cache|webapp|/srv/webapp|redis:7
loose|||nginx:1.27
pgadmin|mysite|/srv/other|dpage/pgadmin4:8
opennvr_old_db|oldsite|/gone/oldsite|postgres:15-alpine
EOF
    cat > "$STATE/volumes" <<'EOF'
mysite_opennvr_db_data|mysite
mysite_opennvr_jwt_keys|mysite
mysite_opennvr_attendly_data|mysite
mysite_opennvr_loitering_config|mysite
mysite_shared_frames|mysite
mysite_pgadmin_data|mysite
webapp_db|webapp
oldsite_opennvr_db_data|oldsite
loose_data|
EOF
    cat > "$STATE/networks" <<'EOF'
mysite_opennvr_internal|mysite
mysite_opennvr_apps|mysite
mysite_default|mysite
mysite_pgadmin|mysite
webapp_default|webapp
EOF
    cat > "$STATE/images" <<'EOF'
ghcr.io/open-nvr/core:0.1.10
ghcr.io/open-nvr/attendly:0.3.0
opennvr/app-installer:local
mysite-whisper:latest
mysite-mediamtx:latest
mysite-unrelated:latest
postgres:15-alpine
nginx:1.27
alpine:3.20
busybox:1.36
redis:7
dpage/pgadmin4:8
EOF
    # Compose's project label: whisper and a mediamtx no container runs any
    # more (a rebuild re-tagged it) are this project's; the stranger's is not.
    cat > "$STATE/image-labels" <<'EOF'
mysite-whisper:latest|mysite
mysite-mediamtx:latest|mysite
EOF
    : > "$STATE/calls.log"
}

# STDIN_FILE feeds the confirmation prompt; the default is no input at all.
run_sh() {
    FAKE_DOCKER_STATE="$STATE" OPENNVR_SYSTEMD_DIR="${SYSTEMD_DIR_TEST:-$WORK/no-systemd}" PATH="$BIN:$PATH" \
        bash "$SITE/scripts/uninstall.sh" "$@" <"${STDIN_FILE:-/dev/null}" 2>&1
}
run_ps1() {
    FAKE_DOCKER_STATE="$(cygpath -w "$STATE")" FAKE_BASH="$BASH_WIN" PATH="$BIN:$PATH" \
        powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$(cygpath -w "$SITE/scripts/uninstall.ps1")" "$@" <"${STDIN_FILE:-/dev/null}" 2>&1 | tr -d '\r'
}
# The lines the operator types, in order: the images answer, then the sentence.
typed() { STDIN_FILE="$WORK/answer"; printf '%s\n' "$@" > "$STDIN_FILE"; }
untyped() { STDIN_FILE=""; }

has() { grep -qE "^$(printf '%s' "$2" | sed 's/[.[\*^$/]/\\&/g')(\||$)" "$STATE/$1"; }

# Nothing of the neighbours may be gone, ever. Prints what is missing.
neighbours_missing() {
    local item missing=""
    for item in my_postgres my_cache loose pgadmin opennvr_old_db; do has containers "$item" || missing="$missing container:$item"; done
    for item in webapp_db oldsite_opennvr_db_data loose_data mysite_pgadmin_data; do has volumes "$item" || missing="$missing volume:$item"; done
    for item in webapp_default mysite_pgadmin; do has networks "$item" || missing="$missing network:$item"; done
    for item in postgres:15-alpine nginx:1.27 busybox:1.36 redis:7 dpage/pgadmin4:8 mysite-unrelated:latest; do has images "$item" || missing="$missing image:$item"; done
    [ -f "$REC/holiday-photos/beach.jpg" ] || missing="$missing file:holiday-photos"
    [ -f "$REC/notes.txt" ] || missing="$missing file:notes.txt"
    [ -f "$SITE/.env.bak.mine" ] || missing="$missing file:.env.bak.mine"
    [ -f "$SITE/data/fake-cameras/demo1.mp4" ] || missing="$missing file:fake-cameras"
    [ -f "$SITE/bin/my-tool" ] || missing="$missing file:bin/my-tool"
    printf '%s' "$missing"
}

# The install, apps included, is gone. Prints what is still there.
install_left() {
    local item left=""
    for item in opennvr_db opennvr_core opennvr_app_installer opennvr_attendly opennvr_loitering_detection mysite-whisper-1; do
        has containers "$item" && left="$left container:$item"
    done
    for item in mysite_opennvr_db_data mysite_opennvr_jwt_keys mysite_opennvr_attendly_data mysite_opennvr_loitering_config mysite_shared_frames; do
        has volumes "$item" && left="$left volume:$item"
    done
    for item in mysite_opennvr_internal mysite_opennvr_apps mysite_default; do has networks "$item" && left="$left network:$item"; done
    for item in .env nginx-certs data/updates data/host-agent data/host-agent-key bin/opennvr-host bin/opennvr-host.exe bin/opennvr-host.build; do
        [ -e "$SITE/$item" ] && left="$left file:$item"
    done
    printf '%s' "$left"
}

untouched() {   # nothing at all changed
    has containers opennvr_db && has containers opennvr_attendly && has volumes mysite_opennvr_db_data \
        && [ -f "$SITE/.env" ] && [ -d "$REC/cam-1" ] && [ -d "$SITE/backups" ] \
        && ! grep -qE ' rm |^rm |image rm' "$STATE/calls.log"
}

# The contract, whichever script is under test ($1 = run_sh | run_ps1).
# The sentence is drawn, never printed: at least three wide rows after the
# prompt, and the sentence itself nowhere in the output as text.
drawn() {
    [ "$(printf '%s\n' "$1" | sed -n '/type the sentence/,$p' | awk 'length($0) >= 30' | wc -l)" -ge 3 ] \
        && ! printf '%s\n' "$1" | grep -qiF "$PHRASE"
}

suite() {
    local run="$1" out rc missing left

    start_test "$run: --dry-run removes nothing and names no stranger"
    fixture "${WD_LABEL:-}"
    untyped
    out=$($run --dry-run); rc=$?
    missing=$(neighbours_missing)
    # What each script says it leaves alone must not include the .env it removes.
    if printf '%s' "$out" | grep -E 'your own copies' | grep -qE ' \.env( |$)'; then missing="$missing listed:.env"; fi
    if [ $rc -eq 0 ] && [ -z "$missing" ] && untouched \
       && printf '%s' "$out" | grep -q 'opennvr_attendly' \
       && printf '%s' "$out" | grep -qE 'OpenNVR images \(5, 7\.5 GB\) and base images nothing else uses \(1, 1\.5 GB\)' \
       && printf '%s' "$out" | grep -qE 'base images: alpine:3.20 *$' \
       && ! printf '%s' "$out" | grep -qE '^ +(my_postgres|pgadmin|webapp_db|mysite_pgadmin_data)$'; then pass
    else fail "rc=$rc missing=[$missing] $(grep -E 'rm ' "$STATE/calls.log" | head -3)"; fi

    start_test "$run: no terminal and no --yes changes nothing"
    fixture "${WD_LABEL:-}"
    untyped
    out=$($run); rc=$?
    if [ $rc -ne 0 ] && untouched; then pass
    else fail "rc=$rc: $out"; fi

    start_test "$run: the wrong sentence changes nothing"
    fixture "${WD_LABEL:-}"
    typed "" "yes"   # images: default; sentence: wrong
    out=$($run); rc=$?
    if [ $rc -ne 0 ] && untouched && printf '%s' "$out" | grep -q 'not the confirmation'; then pass
    else fail "rc=$rc: $out"; fi

    start_test "$run: warns before asking: database, apps, footage, no undo; the sentence is drawn, not printed"
    if printf '%s' "$out" | grep -q 'WARNING' && printf '%s' "$out" | grep -q 'every installed app' \
       && printf '%s' "$out" | grep -q 'all footage in' && printf '%s' "$out" | grep -q 'no undo' \
       && printf '%s' "$out" | grep -q 'type the sentence drawn here' && drawn "$out"; then pass
    else fail "$out"; fi

    start_test "$run: Enter + the typed sentence removes the install, apps, footage, backups, host agent files and images, base images only it used included"
    fixture "${WD_LABEL:-}"
    typed "" "  Uninstall and  remove all data and recordings "   # images: Enter = yes; case and spacing forgiven
    out=$($run); rc=$?
    missing=$(neighbours_missing); left=$(install_left)
    if [ $rc -eq 0 ] && [ -z "$missing" ] && [ -z "$left" ] \
       && [ ! -e "$REC/cam-1" ] && [ ! -e "$REC/cam-12" ] && [ ! -e "$REC/orphaned" ] && [ ! -e "$REC/.evidence" ] \
       && [ -d "$REC" ] && [ ! -e "$SITE/backups" ] \
       && ! has images ghcr.io/open-nvr/core:0.1.10 && ! has images ghcr.io/open-nvr/attendly:0.3.0 \
       && ! has images opennvr/app-installer:local && ! has images mysite-whisper:latest \
       && ! has images mysite-mediamtx:latest && ! has images alpine:3.20 \
       && has images postgres:15-alpine && has images busybox:1.36 \
       && printf '%s' "$out" | grep -q "Also delete OpenNVR's 6 images" && printf '%s' "$out" | grep -q 'Base images removed'; then pass
    else fail "rc=$rc missing=[$missing] left=[$left]: $out"; fi

    start_test "$run: answering n to the images keeps them, and only them"
    fixture "${WD_LABEL:-}"
    typed "n" "$PHRASE"
    out=$($run); rc=$?
    missing=$(neighbours_missing); left=$(install_left)
    if [ $rc -eq 0 ] && [ -z "$missing" ] && [ -z "$left" ] && [ ! -e "$REC/cam-1" ] \
       && has images ghcr.io/open-nvr/core:0.1.10 && has images ghcr.io/open-nvr/attendly:0.3.0 && has images alpine:3.20 \
       && printf '%s' "$out" | grep -q 'The images stay'; then pass
    else fail "rc=$rc missing=[$missing] left=[$left]: $out"; fi

    start_test "$run: --yes does the same without asking, images included"
    fixture "${WD_LABEL:-}"
    untyped
    out=$($run --yes); rc=$?
    missing=$(neighbours_missing); left=$(install_left)
    if [ $rc -eq 0 ] && [ -z "$missing" ] && [ -z "$left" ] && [ ! -e "$REC/cam-1" ] && [ ! -e "$SITE/backups" ] \
       && ! has images ghcr.io/open-nvr/core:0.1.10 && ! has images alpine:3.20 && has images postgres:15-alpine \
       && printf '%s' "$out" | grep -q 'WARNING' && ! printf '%s' "$out" | grep -q "Also delete OpenNVR's"; then pass
    else fail "rc=$rc missing=[$missing] left=[$left]: $out"; fi

    start_test "$run: --keep-recordings --keep-backups --keep-images leave footage, backups and images, and ask nothing about images"
    fixture "${WD_LABEL:-}"
    typed "uninstall and remove all data"   # the shorter sentence: footage stays; no images question
    out=$($run --keep-recordings --keep-backups --keep-images); rc=$?
    missing=$(neighbours_missing); left=$(install_left)
    if [ $rc -eq 0 ] && [ -z "$missing" ] && [ -z "$left" ] \
       && [ -f "$REC/cam-1/2026-11-01/a.mp4" ] && [ -d "$SITE/backups/20260101-0.1.0" ] \
       && has images ghcr.io/open-nvr/core:0.1.10 && has images ghcr.io/open-nvr/attendly:0.3.0 \
       && ! printf '%s' "$out" | grep -q "Also delete OpenNVR's"; then pass
    else fail "rc=$rc missing=[$missing] left=[$left]: $out"; fi

    start_test "$run: with footage kept, the long sentence is not accepted"
    fixture "${WD_LABEL:-}"
    typed "" "$PHRASE"
    out=$($run --keep-recordings); rc=$?
    if [ $rc -ne 0 ] && untouched; then pass; else fail "rc=$rc: $out"; fi

    start_test "$run: never prunes, never forces an image or volume"
    if ! grep -qE 'prune|image rm .*-f|volume rm .*-f|rmi' "$STATE/calls.log"; then pass
    else fail "$(grep -E 'prune|-f' "$STATE/calls.log" | head -3)"; fi

    start_test "$run: warns about opennvr_* containers it leaves, naming them"
    if printf '%s' "$out" | grep -q 'opennvr_old_db'; then pass; else fail "no warning: $out"; fi

    start_test "$run: an old install's folder is gone: --project takes it, and only its own"
    fixture "${WD_LABEL:-}"
    untyped
    out=$($run --yes --project oldsite); rc=$?
    if [ $rc -eq 0 ] && ! has containers opennvr_old_db && ! has volumes oldsite_opennvr_db_data \
       && has containers my_postgres && has volumes webapp_db; then pass
    else fail "rc=$rc: $out"; fi

    start_test "$run: Docker down changes nothing"
    fixture "${WD_LABEL:-}"
    touch "$STATE/daemon-down"
    out=$($run --yes); rc=$?
    if [ $rc -ne 0 ] && [ -f "$SITE/.env" ] && [ -d "$SITE/nginx-certs" ] && [ -d "$REC/cam-1" ]; then pass
    else fail "rc=$rc: $out"; fi

    start_test "$run: a volume that will not go keeps .env and the footage (they are a pair)"
    fixture "${WD_LABEL:-}"
    touch "$STATE/volume-rm-fails"
    out=$($run --yes); rc=$?
    if [ $rc -ne 0 ] && [ -f "$SITE/.env" ] && [ -d "$SITE/nginx-certs" ] && [ -d "$REC/cam-1" ] && [ -d "$SITE/backups" ]; then pass
    else fail "rc=$rc: $out"; fi

    start_test "$run: refuses a folder that is not OpenNVR"
    fixture "${WD_LABEL:-}"
    rm -f "$SITE/docker-compose.yml"
    out=$($run --yes); rc=$?
    if [ $rc -ne 0 ] && [ -f "$SITE/.env" ] && has volumes mysite_opennvr_db_data; then pass
    else fail "rc=$rc: $out"; fi
}

echo "Running uninstall tests"
echo ""
REC_LABEL="" WD_LABEL=""
suite run_sh

# Linux host pieces: only this folder's units, and the firewall table with it.
start_test "run_sh: the update runner and the host agent go only if they run from this folder"
fixture
untyped
SYSTEMD_DIR_TEST="$WORK/systemd"; mkdir -p "$SYSTEMD_DIR_TEST"
printf 'WorkingDirectory=/srv/another-install\n' > "$SYSTEMD_DIR_TEST/opennvr-update.service"
printf 'WorkingDirectory=/srv/another-install\n' > "$SYSTEMD_DIR_TEST/opennvr-host.service"
out=$(run_sh --yes)
first_ok=1
[ -f "$SYSTEMD_DIR_TEST/opennvr-update.service" ] && [ -f "$SYSTEMD_DIR_TEST/opennvr-host.service" ] || first_ok=0
grep -q 'opennvr-host' "$STATE/calls.log" && first_ok=0
fixture
printf 'WorkingDirectory=%s\n' "$SITE" > "$SYSTEMD_DIR_TEST/opennvr-update.service"
touch "$SYSTEMD_DIR_TEST/opennvr-update.timer"
printf 'WorkingDirectory=%s\nExecStart=%s/bin/opennvr-host\n' "$SITE" "$SITE" > "$SYSTEMD_DIR_TEST/opennvr-host.service"
out=$(run_sh --yes)
if [ "$first_ok" = 1 ] && [ ! -f "$SYSTEMD_DIR_TEST/opennvr-update.service" ] && [ ! -f "$SYSTEMD_DIR_TEST/opennvr-update.timer" ] \
   && [ ! -f "$SYSTEMD_DIR_TEST/opennvr-host.service" ] && grep -q 'systemctl disable --now opennvr-host.service' "$STATE/calls.log"; then pass
else fail "first_ok=$first_ok: $out"; fi

start_test "run_sh: containers are stopped before they are removed (a kill leaves NPU buffers behind)"
fixture
untyped
out=$(run_sh --yes)
stop_line=$(grep -n '^stop -t 20 ' "$STATE/calls.log" | head -1 | cut -d: -f1)
rm_line=$(grep -n '^rm -f -v ' "$STATE/calls.log" | head -1 | cut -d: -f1)
if [ -n "$stop_line" ] && [ -n "$rm_line" ] && [ "$stop_line" -lt "$rm_line" ] \
   && grep '^stop -t 20 ' "$STATE/calls.log" | grep -q 'opennvr_core' && ! has containers opennvr_core; then pass
else fail "stop at line '$stop_line', first rm at '$rm_line': $(grep -E '^(stop|rm) ' "$STATE/calls.log" | head -4)"; fi

# The update lock: the runner's once-a-minute check holds it too.
start_test "run_sh: a busy update runner is waited for, then the uninstall goes on"
fixture
untyped
mkdir -p "$SITE/data/updates/.lock"
( sleep 3 ) & holder=$!
echo "$holder" > "$SITE/data/updates/.lock/pid"
out=$(OPENNVR_UNINSTALL_LOCK_WAIT=20 run_sh --yes)
wait "$holder" 2>/dev/null || true
if echo "$out" | grep -q 'update runner is busy' && ! has containers opennvr_core && [ -z "$(neighbours_missing)" ]; then pass
else fail "$out"; fi

start_test "run_sh: an update still running after the wait changes nothing and names the way out"
fixture
untyped
mkdir -p "$SITE/data/updates/.lock"
( sleep 30 ) & holder=$!
echo "$holder" > "$SITE/data/updates/.lock/pid"
out=$(OPENNVR_UNINSTALL_LOCK_WAIT=2 run_sh --yes); rc=$?
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null || true
if [ "$rc" != 0 ] && echo "$out" | grep -q 'auto-update disable' && echo "$out" | grep -q 'nothing has been changed' && has containers opennvr_core; then pass
else fail "rc=$rc: $out"; fi

start_test "run_sh: --dry-run does not wait for a busy update runner"
fixture
untyped
mkdir -p "$SITE/data/updates/.lock"
( sleep 30 ) & holder=$!
echo "$holder" > "$SITE/data/updates/.lock/pid"
t0=$(date +%s)
out=$(OPENNVR_UNINSTALL_LOCK_WAIT=60 run_sh --dry-run); rc=$?
t1=$(date +%s)
kill "$holder" 2>/dev/null; wait "$holder" 2>/dev/null || true
if [ "$rc" = 0 ] && [ $((t1 - t0)) -lt 20 ] && echo "$out" | grep -q 'Dry run: nothing changed' && has containers opennvr_core; then pass
else fail "rc=$rc after $((t1 - t0)) s: $out"; fi

start_test "run_sh: Ollama's drop-in is reported, not removed, and Ollama is not restarted"
fixture
mkdir -p "$SYSTEMD_DIR_TEST/ollama.service.d"
touch "$SYSTEMD_DIR_TEST/ollama.service.d/10-opennvr-host.conf"
out=$(run_sh --yes)
if [ -f "$SYSTEMD_DIR_TEST/ollama.service.d/10-opennvr-host.conf" ] && ! grep -q 'restart ollama' "$STATE/calls.log" \
   && printf '%s' "$out" | grep -q '10-opennvr-host.conf'; then pass
else fail "$out"; fi
SYSTEMD_DIR_TEST=""

start_test "run_sh: the firewall table goes only with this folder's host-hardening/"
fixture
out=$(run_sh --yes)
first_ok=1; grep -q 'nft delete' "$STATE/calls.log" && first_ok=0
fixture
mkdir -p "$SITE/host-hardening"
out=$(run_sh --yes)
if [ "$first_ok" = 1 ] && grep -q 'nft delete table inet opennvr-vlan' "$STATE/calls.log"; then pass
else fail "first_ok=$first_ok: $(cat "$STATE/calls.log" | grep nft)"; fi

# The launcher verb hands over to the script, options and all.
start_test "start.sh and start.ps1 have an uninstall verb that runs the script"
if grep -qE '^\s*uninstall\)' "$REPO_ROOT/start.sh" && grep -q 'scripts/uninstall.sh' "$REPO_ROOT/start.sh" \
   && grep -qE '^\s*"uninstall"\s*\{' "$REPO_ROOT/start.ps1" && grep -q 'scripts\\uninstall.ps1' "$REPO_ROOT/start.ps1" \
   && grep -q 'start.sh uninstall' "$REPO_ROOT/start.sh" && grep -q 'start.ps1 uninstall' "$REPO_ROOT/start.ps1"; then pass
else fail "the verb or its usage line is missing from a launcher"; fi

if [ -n "$BASH_WIN" ]; then
    # Windows paths in .env and in the compose label, as a real install has them.
    echo ""
    ps1_fixture_paths() { WD_LABEL="$(cygpath -w "$WORK/s$((${FIXTURES:-0} + 1))/mysite")"; REC_LABEL="$(cygpath -m "$WORK/s$((${FIXTURES:-0} + 1))/footage")"; }
    # Re-derive the labels before every fixture the suite makes.
    eval "$(declare -f fixture | sed '1s/fixture/_fixture/')"
    fixture() { ps1_fixture_paths; _fixture "$WD_LABEL"; }
    suite run_ps1
else
    echo "SKIP: uninstall.ps1 tests need Windows PowerShell."
fi

echo ""
echo "Tests run    : ${TESTS_RUN}"
echo "Tests failed : ${TESTS_FAILED}"
[ "$TESTS_FAILED" -eq 0 ]
