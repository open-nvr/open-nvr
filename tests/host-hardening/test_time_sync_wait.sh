#!/usr/bin/env bash
# ============================================================
# Docker waits for NTP at boot on hosts with no usable RTC
# (scripts/time-sync.sh, issue #1023).
#
# Never touches the real system: systemctl and sudo are stubs that log
# their calls, the systemd folder and the RTC are scratch directories.
#
# Run: bash tests/host-hardening/test_time_sync_wait.sh
# ============================================================
set -u

ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
WORK=$(mktemp -d)
[ -n "$WORK" ] && [ -d "$WORK" ] || { echo "no scratch dir"; exit 2; }
trap 'rm -rf "$WORK"' EXIT

PASS=0; FAIL=0
pass() { PASS=$((PASS + 1)); printf '  ✓ %s\n' "$CASE"; }
fail() { FAIL=$((FAIL + 1)); printf '  ✗ %s\n' "$CASE"; [ -z "${1:-}" ] || printf '      %s\n' "$1"; }

BIN="$WORK/bin"; mkdir -p "$BIN"
# systemctl: is-active answers from $ACTIVE (space-separated units), cat
# from $UNITS, is-enabled from $ENABLED; every call is logged.
cat > "$BIN/systemctl" <<'EOF'
#!/usr/bin/env bash
printf 'systemctl %s\n' "$*" >> "$CALLS"
case "$1" in
    is-active) u="${3:-$2}"; case " $ACTIVE " in *" $u "*) exit 0 ;; esac; exit 3 ;;
    cat)       case " $UNITS " in *" $2 "*) exit 0 ;; esac; exit 1 ;;
    is-enabled) u="${3:-$2}"; case " $ENABLED " in *" $u "*) exit 0 ;; esac; exit 1 ;;
esac
exit 0
EOF
printf '#!/usr/bin/env bash\nprintf "sudo %%s\\n" "$*" >> "$CALLS"\n"$@"\n' > "$BIN/sudo"
chmod +x "$BIN/systemctl" "$BIN/sudo"

# One fresh world per case. $1 = RTC epoch ("" = no RTC at all).
setup() {
    CASE="$1"; shift
    W="$WORK/case$PASS$FAIL$RANDOM"; mkdir -p "$W/systemd" "$W/rtc"
    [ -z "$1" ] || printf '%s\n' "$1" > "$W/rtc/since_epoch"
    export CALLS="$W/calls.log"; : > "$CALLS"
    export ACTIVE="systemd-timesyncd" UNITS="systemd-time-wait-sync.service" ENABLED=""
    ENV_FILE="$W/env"; : > "$ENV_FILE"
}

# Run one time-sync.sh function in a subshell with the stubs on PATH.
ts() {
    ( cd "$W" && PATH="$BIN:$PATH" OPENNVR_SYSTEMD_DIR="$W/systemd" OPENNVR_RTC_DIR="$W/rtc" \
        ENV_FILE="$ENV_FILE" bash -c '
            get_env_var() { sed -n "s/^$1=//p" "$ENV_FILE" | tail -1; }
            write_env_var() { printf "%s=%s\n" "$1" "$2" >> "$ENV_FILE"; }
            . "'"$ROOT_DIR"'/scripts/time-sync.sh"
            "$@"' ts "$@" ) </dev/null > "$W/out.log" 2>&1
}

DOCKER_DROPIN='systemd/docker.service.d/opennvr-time-sync.conf'
WAIT_DROPIN='systemd/systemd-time-wait-sync.service.d/opennvr-time-sync.conf'

echo "time-sync-wait"

setup "no RTC (reads 1970): enable writes both drop-ins, caps the wait, enables the wait unit" 5
if ts run_time_sync_wait enable \
   && grep -qx 'After=time-sync.target' "$W/$DOCKER_DROPIN" \
   && grep -qx 'Wants=time-sync.target' "$W/$DOCKER_DROPIN" \
   && grep -qx 'TimeoutStartSec=120' "$W/$WAIT_DROPIN" \
   && grep -qx '# opennvr-enabled-unit=systemd-time-wait-sync.service' "$W/$DOCKER_DROPIN" \
   && grep -qx "# Installed by OpenNVR (./start.sh time-sync-wait) from $W" "$W/$DOCKER_DROPIN" \
   && grep -q 'systemctl enable systemd-time-wait-sync.service' "$CALLS" \
   && grep -q 'systemctl daemon-reload' "$CALLS" \
   && grep -qx 'TIME_SYNC_WAIT=on' "$ENV_FILE"; then pass; else fail "$(cat "$W/out.log")"; fi

setup "no RTC device at all counts as unusable" ""
if ts run_time_sync_wait enable && [ -f "$W/$DOCKER_DROPIN" ]; then pass; else fail "$(cat "$W/out.log")"; fi

setup "a working RTC: enable changes nothing" 1795000000
if ts run_time_sync_wait enable && [ ! -e "$W/$DOCKER_DROPIN" ] && ! grep -q sudo "$CALLS"; then pass; else fail "$(cat "$W/out.log")"; fi

setup "a working RTC with --force: set up anyway" 1795000000
if ts run_time_sync_wait enable --force && [ -f "$W/$DOCKER_DROPIN" ]; then pass; else fail "$(cat "$W/out.log")"; fi

setup "no time service: refuses, writes nothing" 5
ACTIVE=""
if ! ts run_time_sync_wait enable && [ ! -e "$W/$DOCKER_DROPIN" ] && grep -q 'set-ntp' "$W/out.log"; then pass; else fail "$(cat "$W/out.log")"; fi

setup "chrony host: waits on chrony-wait.service" 5
ACTIVE="chrony"; UNITS="chrony-wait.service"
if ts run_time_sync_wait enable && [ -f "$W/systemd/chrony-wait.service.d/opennvr-time-sync.conf" ] \
   && grep -q 'systemctl enable chrony-wait.service' "$CALLS"; then pass; else fail "$(cat "$W/out.log")"; fi

setup "disable removes the drop-ins and disables the unit it enabled" 5
ts run_time_sync_wait enable; : > "$CALLS"
if ts run_time_sync_wait disable && [ ! -e "$W/$DOCKER_DROPIN" ] && [ ! -e "$W/$WAIT_DROPIN" ] \
   && grep -q 'systemctl disable systemd-time-wait-sync.service' "$CALLS" \
   && [ "$(tail -1 "$ENV_FILE")" = 'TIME_SYNC_WAIT=off' ]; then pass; else fail "$(cat "$W/out.log") $(cat "$CALLS")"; fi

setup "a wait unit the admin had enabled is left enabled on disable" 5
ENABLED="systemd-time-wait-sync.service"
ts run_time_sync_wait enable; : > "$CALLS"
if grep -qx '# opennvr-enabled-unit=' "$W/$DOCKER_DROPIN" \
   && ts run_time_sync_wait disable && ! grep -q 'systemctl disable' "$CALLS"; then pass; else fail "$(cat "$CALLS")"; fi

setup "up without a terminal: only a hint, nothing installed" 5
if ts run_time_sync_wait_refresh && [ ! -e "$W/$DOCKER_DROPIN" ] \
   && grep -q 'time-sync-wait enable' "$W/out.log"; then pass; else fail "$(cat "$W/out.log")"; fi

setup "up with TIME_SYNC_WAIT=off: silent" 5
printf 'TIME_SYNC_WAIT=off\n' > "$ENV_FILE"
if ts run_time_sync_wait_refresh && [ ! -s "$W/out.log" ]; then pass; else fail "$(cat "$W/out.log")"; fi

setup "up on a host with a working RTC: silent" 1795000000
if ts run_time_sync_wait_refresh && [ ! -s "$W/out.log" ]; then pass; else fail "$(cat "$W/out.log")"; fi

setup "status names the RTC, the time service and the state" 5
if ts run_time_sync_wait status && grep -q 'Hardware clock: none usable' "$W/out.log" \
   && grep -q 'Time service: systemd-time-wait-sync.service' "$W/out.log" \
   && grep -q 'does not wait' "$W/out.log"; then pass; else fail "$(cat "$W/out.log")"; fi

echo
echo "  ${PASS} passed, ${FAIL} failed"
[ "$FAIL" = 0 ]
