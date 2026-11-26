#!/usr/bin/env bash
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Start Docker only once the clock is right, on hosts with no usable
# hardware clock (issue #1023).
#
# A board with no battery-backed RTC (Arduino Ventuno Q, many SBCs) boots on
# the time it saved at shutdown. If the stack starts before NTP answers, the
# clock is stepped under running streams: recordings carry the wrong time,
# MFA codes are rejected, and core has to reconnect every camera. This makes
# docker.service wait for time-sync.target, which the system's own wait
# service (systemd-time-wait-sync or chrony-wait) reaches once NTP has set
# the clock, with that wait capped so an offline site still starts.
#
#   ./start.sh time-sync-wait status
#   ./start.sh time-sync-wait enable [--force]   (sudo once; --force also on
#                                                 a host whose RTC works)
#   ./start.sh time-sync-wait disable
#
# `up` offers it once at a terminal when it would help; TIME_SYNC_WAIT=off in
# .env opts out. Sourced by start.sh (and install.sh for the RTC check);
# the paths are overridable for tests.

TS_SYSTEMD_DIR="${OPENNVR_SYSTEMD_DIR:-/etc/systemd/system}"
TS_RTC_DIR="${OPENNVR_RTC_DIR:-/sys/class/rtc/rtc0}"
TS_DROPIN_NAME="opennvr-time-sync.conf"
TS_DOCKER_DROPIN="$TS_SYSTEMD_DIR/docker.service.d/$TS_DROPIN_NAME"
# Longest Docker waits for the clock at boot. Past it Docker starts anyway
# (time-sync.target only orders, it does not require the wait to succeed)
# and core's clock-jump watcher repairs the streams when NTP steps later.
TS_WAIT_SECONDS="${OPENNVR_TIME_SYNC_WAIT_SECONDS:-120}"
# An RTC reading before this (2020-01-01) is not keeping time: the board's
# reads 1970 because nothing ever set it, or its battery is flat.
TS_RTC_SANE_EPOCH=1577836800

ts_say()  { printf '  %s\n' "$*"; }
ts_ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
ts_warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
ts_root() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }

# 0 when the hardware clock can be trusted to carry the time across a
# power-off: it exists and reads a plausible date.
ts_rtc_usable() {
    local epoch
    [ -r "$TS_RTC_DIR/since_epoch" ] || return 1
    epoch=$(cat "$TS_RTC_DIR/since_epoch" 2>/dev/null) || return 1
    case "$epoch" in ''|*[!0-9]*) return 1 ;; esac
    [ "$epoch" -ge "$TS_RTC_SANE_EPOCH" ]
}

# The unit that holds time-sync.target back until NTP has set the clock,
# for the time service this host actually runs; empty when there is none.
ts_wait_unit() {
    if systemctl is-active --quiet systemd-timesyncd 2>/dev/null \
       && systemctl cat systemd-time-wait-sync.service >/dev/null 2>&1; then
        printf 'systemd-time-wait-sync.service'
    elif { systemctl is-active --quiet chrony 2>/dev/null || systemctl is-active --quiet chronyd 2>/dev/null; } \
       && systemctl cat chrony-wait.service >/dev/null 2>&1; then
        printf 'chrony-wait.service'
    fi
}

ts_installed() { [ -f "$TS_DOCKER_DROPIN" ]; }

# The wait unit OpenNVR enabled itself (recorded in the drop-in), so disable
# leaves alone one the administrator had enabled before.
ts_enabled_by_us() {
    [ -f "$TS_DOCKER_DROPIN" ] || return 0
    sed -n 's/^# opennvr-enabled-unit=//p' "$TS_DOCKER_DROPIN"
}

ts_applicable() {
    [ "$(uname -s)" = Linux ] || return 1
    command -v systemctl >/dev/null 2>&1 || return 1
    # PID 1 is systemd (not a container or WSL without it); tests point
    # OPENNVR_SYSTEMD_DIR at a scratch folder instead.
    [ -d /run/systemd/system ] || [ -n "${OPENNVR_SYSTEMD_DIR:-}" ]
}

ts_enable() {
    local force="$1" unit enabled_by_us="" tmp
    if ! ts_applicable; then
        ts_warn "Only for Linux hosts run by systemd; nothing to do here."
        return 0
    fi
    if [ "$force" != 1 ] && ts_rtc_usable; then
        ts_ok "This host's hardware clock keeps time across a power-off, so Docker does not need to wait for NTP (--force sets it up anyway)."
        return 0
    fi
    unit=$(ts_wait_unit)
    if [ -z "$unit" ]; then
        ts_warn "No time service with a wait unit is running (systemd-timesyncd or chrony), so there is nothing to wait for."
        ts_say "Turn one on first, e.g.: sudo timedatectl set-ntp true"
        return 1
    fi
    systemctl is-enabled --quiet "$unit" 2>/dev/null || enabled_by_us="$unit"

    tmp=$(mktemp -d) || return 1
    cat > "$tmp/docker" <<EOF
# Installed by OpenNVR (./start.sh time-sync-wait) from $PWD
# opennvr-enabled-unit=$enabled_by_us
# Start Docker once NTP has set the clock (at most ${TS_WAIT_SECONDS} s, see $unit),
# so cameras never start recording on the time this board saved at shutdown.
[Unit]
Wants=time-sync.target
After=time-sync.target
EOF
    cat > "$tmp/wait" <<EOF
# Installed by OpenNVR (./start.sh time-sync-wait) from $PWD
# Give up waiting for NTP after ${TS_WAIT_SECONDS} s: an offline site must still start.
[Service]
TimeoutStartSec=${TS_WAIT_SECONDS}
EOF
    [ "$(id -u)" = 0 ] || ts_say "Making Docker wait for the clock at boot needs sudo once:"
    if ! ts_root mkdir -p "$TS_SYSTEMD_DIR/docker.service.d" "$TS_SYSTEMD_DIR/$unit.d" \
       || ! ts_root install -m 644 "$tmp/docker" "$TS_DOCKER_DROPIN" \
       || ! ts_root install -m 644 "$tmp/wait" "$TS_SYSTEMD_DIR/$unit.d/$TS_DROPIN_NAME"; then
        rm -rf "$tmp"; ts_warn "Could not write the systemd drop-ins."; return 1
    fi
    rm -rf "$tmp"
    ts_root systemctl daemon-reload || return 1
    if [ -n "$enabled_by_us" ]; then
        ts_root systemctl enable "$unit" >/dev/null 2>&1 || { ts_warn "Could not enable $unit."; return 1; }
    fi
    type write_env_var >/dev/null 2>&1 && write_env_var TIME_SYNC_WAIT on
    ts_ok "At boot, Docker now waits for NTP to set the clock ($unit, at most ${TS_WAIT_SECONDS} s)."
}

ts_disable() {
    local unit dropins
    type write_env_var >/dev/null 2>&1 && write_env_var TIME_SYNC_WAIT off
    if ! ts_installed; then
        ts_ok "Docker does not wait for the clock at boot (nothing installed)."
        return 0
    fi
    unit=$(ts_enabled_by_us)
    dropins=$(ls "$TS_SYSTEMD_DIR"/*.service.d/"$TS_DROPIN_NAME" 2>/dev/null)
    [ "$(id -u)" = 0 ] || ts_say "Removing the systemd drop-ins needs sudo:"
    [ -z "$unit" ] || ts_root systemctl disable "$unit" >/dev/null 2>&1 || true
    # shellcheck disable=SC2086 # one path per word, none with spaces
    ts_root rm -f $dropins || { ts_warn "Could not remove $dropins"; return 1; }
    ts_root systemctl daemon-reload >/dev/null 2>&1 || true
    ts_ok "Docker no longer waits for the clock at boot."
}

ts_status() {
    local unit synced
    if ! ts_applicable; then
        ts_say "Not a systemd Linux host: Docker's start is not tied to the clock here."
        return 0
    fi
    if ts_rtc_usable; then
        ts_say "Hardware clock: keeps time ($(date -u -d "@$(cat "$TS_RTC_DIR/since_epoch")" '+%Y-%m-%d %H:%M UTC' 2>/dev/null))"
    else
        ts_say "Hardware clock: none usable, so each boot starts on the time saved at shutdown"
    fi
    unit=$(ts_wait_unit)
    ts_say "Time service: ${unit:-none with a wait unit (systemd-timesyncd or chrony)}"
    synced=$(timedatectl show -p NTPSynchronized --value 2>/dev/null)
    ts_say "Clock synchronised now: ${synced:-unknown}"
    if ts_installed; then
        ts_ok "Docker waits for the clock at boot (at most ${TS_WAIT_SECONDS} s)"
    else
        ts_say "Docker does not wait for the clock at boot. Turn on: ./start.sh time-sync-wait enable"
    fi
}

run_time_sync_wait() {
    local force=0 arg
    for arg in "${@:2}"; do [ "$arg" = --force ] && force=1; done
    case "${1:-status}" in
        enable)  ts_enable "$force" ;;
        disable) ts_disable ;;
        status)  ts_status ;;
        *) ts_warn "Usage: ./start.sh time-sync-wait [status|enable [--force]|disable]"; return 1 ;;
    esac
}

# Called by `up`. Installs it once, at a terminal, where it helps: a systemd
# Linux host with no usable RTC and a time service to wait for. Elsewhere,
# or with TIME_SYNC_WAIT=off in .env, it does nothing.
run_time_sync_wait_refresh() {
    ts_applicable || return 0
    [ "$(get_env_var TIME_SYNC_WAIT 2>/dev/null)" != off ] || return 0
    ts_installed && return 0
    ts_rtc_usable && return 0
    [ -n "$(ts_wait_unit)" ] || return 0
    if [ -t 0 ] && [ "${KEEP_ORPHANS:-}" != 1 ]; then
        ts_say "This host has no working hardware clock: making Docker wait for NTP at boot (at most ${TS_WAIT_SECONDS} s),"
        ts_say "so recordings never start on a stale time (opt out: ./start.sh time-sync-wait disable)."
        ts_enable 0 || ts_warn "Not set up. Later: ./start.sh time-sync-wait enable"
    else
        ts_warn "This host has no working hardware clock. Make Docker wait for NTP at boot: ./start.sh time-sync-wait enable"
    fi
}
