# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# The OpenNVR host agent on Linux and macOS: ./start.sh host-agent install|uninstall|status|logs
# Sourced by start.sh (which provides get_env_var and the colour variables).
#
# The agent (host-agent/, docs/HOST_AGENT.md) streams USB webcams into their
# OpenNVR cameras and reports the machine's own CPU, memory, disks and
# temperatures. It opens no port: it calls core on 127.0.0.1 with its own key.
#
#   bin/opennvr-host                 the agent, copied out of the core image
#   bin/opennvr-host.build           the build it is (compared on every `up`)
#   data/host-agent-key/agent.key    its key (mode 600; never mounted)
#   data/host-agent/agent.key.sha256 the key's hash, mounted read-only into core
#   Linux: systemd service opennvr-host, as this user, from boot (sudo once).
#   macOS: LaunchAgent org.opennvr.host, at login.
#
# Everything here runs as the normal user — start.sh refuses sudo — and only
# the systemd unit itself is written through `sudo`.

HA_EXE="bin/opennvr-host"
HA_BUILD="bin/opennvr-host.build"
HA_KEY_DIR="data/host-agent-key"
HA_KEY="data/host-agent-key/agent.key"
HA_HASH_DIR="data/host-agent"
HA_HASH="data/host-agent/agent.key.sha256"
HA_LOG="data/host-agent-key/agent.log"
HA_UNIT="opennvr-host.service"
HA_PLIST="$HOME/Library/LaunchAgents/org.opennvr.host.plist"

ha_say()  { printf '  %s\n' "$*"; }
ha_ok()   { printf '  \033[32mOK\033[0m  %s\n' "$*"; }
ha_warn() { printf '  \033[33m!\033[0m   %s\n' "$*"; }

ha_platform() {
    local os arch
    case "$(uname -s)" in
        Darwin) os=darwin ;;
        *) os=linux ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) arch=amd64 ;;
        aarch64|arm64) arch=arm64 ;;
        *) arch=unsupported ;;
    esac
    echo "$os-$arch"
}

ha_sha256() {
    if command -v sha256sum >/dev/null 2>&1; then sha256sum | cut -d' ' -f1
    else shasum -a 256 | cut -d' ' -f1; fi
}

# HOST_AGENT=on|off in .env, every other line kept.
ha_set_env_flag() {
    [ -f .env ] || return 0
    if grep -q '^HOST_AGENT=' .env; then
        local tmp; tmp=$(mktemp .env.XXXXXX)
        sed "s/^HOST_AGENT=.*/HOST_AGENT=$1/" .env >"$tmp" && cat "$tmp" >.env
        rm -f "$tmp"
    else
        printf 'HOST_AGENT=%s\n' "$1" >>.env
    fi
}

ha_init_key() {
    mkdir -p "$HA_KEY_DIR" "$HA_HASH_DIR"
    chmod 700 "$HA_KEY_DIR"
    if [ ! -f "$HA_KEY" ]; then
        ( umask 077
          printf 'ohk_%s\n' "$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '=\n')" >"$HA_KEY" )
        ha_ok "Created the agent key."
    fi
    local hex current=""
    hex=$(tr -d '\r\n' <"$HA_KEY" | ha_sha256)
    [ -f "$HA_HASH" ] && current=$(tr -d '\r\n' <"$HA_HASH")
    [ "$current" = "$hex" ] || printf '%s\n' "$hex" >"$HA_HASH"
}

# Copy the agent for this machine out of the running core container. Exit
# status 0 when a different build was installed. The swap is a rename, so a
# running agent keeps its (unlinked) binary until it restarts on the new one.
ha_install_binary() {
    local plat; plat=$(ha_platform)
    case "$plat" in *unsupported*) ha_warn "No host agent for $(uname -m)."; return 1 ;; esac
    if [ "$(docker inspect -f '{{.State.Running}}' opennvr_core 2>/dev/null)" != "true" ]; then
        ha_warn "OpenNVR is not running, so the agent cannot be copied out of it. Run ./start.sh up first."
        return 1
    fi
    mkdir -p bin
    local tmp; tmp=$(mktemp -d bin/.incoming.XXXXXX)
    if ! docker cp "opennvr_core:/opt/opennvr-host/$plat/opennvr-host" "$tmp/opennvr-host" >/dev/null 2>&1; then
        rm -rf "$tmp"
        ha_warn "This OpenNVR image has no host agent for $plat (an image from before the agent?)."
        return 1
    fi
    docker cp "opennvr_core:/opt/opennvr-host/BUILD" "$tmp/BUILD" >/dev/null 2>&1 || true
    local new old=""
    new=$(tr -d '\r\n' <"$tmp/BUILD" 2>/dev/null || true)
    [ -f "$HA_BUILD" ] && old=$(tr -d '\r\n' <"$HA_BUILD")
    if [ -x "$HA_EXE" ] && [ -n "$new" ] && [ "$new" = "$old" ]; then
        rm -rf "$tmp"
        return 1
    fi
    chmod 755 "$tmp/opennvr-host"
    mv -f "$tmp/opennvr-host" "$HA_EXE"
    printf '%s\n' "$new" >"$HA_BUILD"
    rm -rf "$tmp"
    ha_ok "Host agent $new installed in bin/."
    return 0
}

# The per-camera units the webcam helper script used to install, before the
# agent existed. Stopped by unit/label name, never by process name.
ha_remove_script_era() {
    local f u
    if [ "$(uname -s)" = Darwin ]; then
        for f in "$HOME"/Library/LaunchAgents/org.opennvr.webcam.cam-*.plist; do
            [ -f "$f" ] || continue
            launchctl unload "$f" 2>/dev/null || true
            rm -f "$f"; ha_ok "Removed the old webcam agent $(basename "$f")."
        done
    elif command -v systemctl >/dev/null 2>&1; then
        for f in "${XDG_CONFIG_HOME:-$HOME/.config}"/systemd/user/opennvr-webcam-cam-*.service; do
            [ -f "$f" ] || continue
            u=$(basename "$f")
            systemctl --user disable --now "$u" 2>/dev/null || true
            rm -f "$f"; ha_ok "Removed the old webcam service $u."
        done
        systemctl --user daemon-reload 2>/dev/null || true
    fi
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/opennvr/webcam"
}

ha_register_linux() {
    if ! command -v systemctl >/dev/null 2>&1; then
        ha_warn "No systemd here. Start the agent from your own startup script: $PWD/$HA_EXE"
        return 1
    fi
    local me; me=$(id -un)
    local unit
    unit=$(cat <<EOF
[Unit]
Description=OpenNVR host agent (USB webcams, host monitoring) for $PWD
After=network-online.target docker.service
Wants=network-online.target

[Service]
User=$me
SupplementaryGroups=video
WorkingDirectory=$PWD
ExecStart=$PWD/$HA_EXE
Restart=always
RestartSec=5
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
EOF
)
    ha_say "Installing the systemd service needs sudo once:"
    if ! printf '%s\n' "$unit" | sudo tee "/etc/systemd/system/$HA_UNIT" >/dev/null; then
        ha_warn "Could not write /etc/systemd/system/$HA_UNIT."
        return 1
    fi
    sudo systemctl daemon-reload && sudo systemctl enable "$HA_UNIT" >/dev/null 2>&1 \
        && sudo systemctl restart "$HA_UNIT"
}

ha_register_macos() {
    mkdir -p "$(dirname "$HA_PLIST")"
    cat >"$HA_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>org.opennvr.host</string>
  <key>ProgramArguments</key><array><string>$PWD/$HA_EXE</string></array>
  <key>WorkingDirectory</key><string>$PWD</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/dev/null</string>
  <key>StandardErrorPath</key><string>/dev/null</string>
</dict></plist>
EOF
    launchctl unload "$HA_PLIST" 2>/dev/null || true
    launchctl load "$HA_PLIST"
}

ha_restart() {
    if [ "$(uname -s)" = Darwin ]; then
        [ -f "$HA_PLIST" ] && { launchctl unload "$HA_PLIST" 2>/dev/null || true; launchctl load "$HA_PLIST"; }
    fi
    # Linux: restarting the system service needs root, which `up` does not
    # have. It does not need to: the agent sees core's new build, finds a new
    # binary in its place, and exits; systemd (Restart=always) starts it.
}

run_host_agent_install() {
    echo -e "  ${CYAN:-}OpenNVR host agent${NC:-}"
    ha_init_key
    ha_install_binary || true
    [ -x "$HA_EXE" ] || { ha_warn "Not installed: no agent binary."; return 1; }
    ha_remove_script_era
    if [ "$(uname -s)" = Darwin ]; then
        ha_register_macos
        ha_ok "Running in the background, and at every login (launchd: org.opennvr.host)."
        ha_say "macOS asks before an app may use the camera, and a background agent cannot show"
        ha_say "that question. If webcams stay 'not allowed', run $PWD/$HA_EXE once in Terminal"
        ha_say "and allow it, or add it under System Settings > Privacy & Security > Camera."
    else
        ha_register_linux || return 1
        ha_ok "Running as $(id -un), from boot (systemd: $HA_UNIT)."
        id -nG | grep -qw video || ha_say "(The service gets the 'video' group itself; your login does not need it.)"
    fi
    ha_set_env_flag on
    if ! command -v ffmpeg >/dev/null 2>&1; then
        ha_warn "ffmpeg is not installed: USB webcams cannot stream until it is."
        if [ "$(uname -s)" = Darwin ]; then ha_say "Install it with: brew install ffmpeg"
        else ha_say "Install it with: sudo apt install ffmpeg   (or your distribution's package)"; fi
    fi
    ha_say "Logs: $HA_LOG"
}

# Called by `up`: keep an installed agent current with the core it talks to.
#
# A fresh install (the installer set HOST_AGENT=on, nothing registered yet)
# gets the whole install here. On Linux that needs sudo for the systemd unit,
# so it happens only at a terminal; the unattended updater says how instead.
run_host_agent_refresh() {
    [ "$(get_env_var HOST_AGENT)" = on ] || return 0
    local registered=no
    if [ "$(uname -s)" = Darwin ]; then
        [ -f "$HA_PLIST" ] && registered=yes
    else
        [ -f "/etc/systemd/system/$HA_UNIT" ] && registered=yes
    fi
    if [ "$registered" = no ]; then
        if [ "$(uname -s)" = Darwin ] || { [ -t 0 ] && [ "${KEEP_ORPHANS:-}" != 1 ]; }; then
            run_host_agent_install
        else
            ha_warn "The host agent is on in .env but not installed. Install it with: ./start.sh host-agent install"
        fi
        return 0
    fi
    ha_init_key
    if ha_install_binary; then
        ha_restart
        ha_ok "Host agent updated; it restarts on the new build within a few seconds."
    fi
}

run_host_agent_uninstall() {
    if [ "$(uname -s)" = Darwin ]; then
        [ -f "$HA_PLIST" ] && { launchctl unload "$HA_PLIST" 2>/dev/null || true; rm -f "$HA_PLIST"; ha_ok "Removed the launchd agent."; }
    elif [ -f "/etc/systemd/system/$HA_UNIT" ]; then
        ha_say "Removing the systemd service needs sudo:"
        sudo systemctl disable --now "$HA_UNIT" 2>/dev/null || true
        sudo rm -f "/etc/systemd/system/$HA_UNIT" && sudo systemctl daemon-reload
        ha_ok "Removed the service $HA_UNIT."
    fi
    rm -f "$HA_EXE" "$HA_BUILD"
    rm -rf "$HA_KEY_DIR" "$HA_HASH_DIR"
    ha_set_env_flag off
    ha_ok "Host agent removed. USB webcam cameras stop streaming; browser webcams are not affected."
}

run_host_agent_status() {
    local build="?"
    [ -f "$HA_BUILD" ] && build=$(tr -d '\r\n' <"$HA_BUILD")
    if [ "$(uname -s)" = Darwin ]; then
        if [ -f "$HA_PLIST" ]; then ha_say "launchd org.opennvr.host: $(launchctl list 2>/dev/null | grep -q org.opennvr.host && echo loaded || echo not loaded); build $build"
        else ha_say "Not installed. Install it with: ./start.sh host-agent install"; return 0; fi
    elif [ -f "/etc/systemd/system/$HA_UNIT" ]; then
        ha_say "systemd $HA_UNIT: $(systemctl is-active "$HA_UNIT" 2>/dev/null); build $build"
    else
        ha_say "Not installed. Install it with: ./start.sh host-agent install"; return 0
    fi
    command -v ffmpeg >/dev/null 2>&1 && ha_say "ffmpeg: found" || ha_warn "ffmpeg: not installed"
    if [ -f "$HA_LOG" ]; then
        ha_say "Last log lines ($HA_LOG):"
        tail -n 8 "$HA_LOG" | sed 's/^/    /'
    fi
}

run_host_agent() {
    case "${1:-status}" in
        install) run_host_agent_install ;;
        uninstall) run_host_agent_uninstall ;;
        status) run_host_agent_status ;;
        logs) [ -f "$HA_LOG" ] && tail -n 50 -f "$HA_LOG" || ha_say "No log yet." ;;
        *) echo "Usage: ./start.sh host-agent install|uninstall|status|logs"; return 1 ;;
    esac
}
