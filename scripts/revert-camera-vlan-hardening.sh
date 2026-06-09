#!/usr/bin/env bash
# ============================================================
# OpenNVR — revert paper-compliant host hardening
# (ISSUE-6 v3)
# ============================================================
#
# Removes the dedicated `inet opennvr-vlan` nft table that
# apply-camera-vlan-hardening.sh installs. Because that table is
# isolated from the host's other nftables ruleset, removing it is a
# single `nft delete table` — we don't risk breaking firewalld, UFW,
# or any other pre-existing firewall config.
#
# Usage:
#   ./scripts/revert-camera-vlan-hardening.sh

set -euo pipefail

if [ -t 1 ]; then
    RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
    GRAY='\033[38;5;245m'; WHITE='\033[1;37m'; NC='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; GRAY=''; WHITE=''; NC=''
fi

if ! command -v nft >/dev/null 2>&1; then
    echo -e "${YELLOW}nft not installed — nothing to revert.${NC}" >&2
    exit 0
fi

# Check if our table exists before trying to delete it.
if ! sudo nft list table inet opennvr-vlan >/dev/null 2>&1; then
    echo -e "${GREEN}No opennvr-vlan table present — nothing to revert.${NC}"
    exit 0
fi

echo -e "${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${WHITE}OpenNVR — remove camera/uplink firewall isolation${NC}"
echo -e "${WHITE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "${WHITE}Current opennvr-vlan rules:${NC}"
echo -e "${GRAY}────────────────────────────────────────────${NC}"
sudo nft list table inet opennvr-vlan | sed 's/^/  /'
echo -e "${GRAY}────────────────────────────────────────────${NC}"
echo ""
echo -e "${WHITE}This will run:${NC}"
echo -e "  ${WHITE}sudo nft delete table inet opennvr-vlan${NC}"
echo ""
read -rp "  Revert now? [y/N]: " confirm
if ! [[ "$confirm" =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Aborted. Hardening still active.${NC}"
    exit 0
fi

if sudo nft delete table inet opennvr-vlan; then
    echo -e "${GREEN}✓ Hardening reverted.${NC}"
    # Move the active-snapshot symlink out of the way so a future
    # apply mints a fresh snapshot.
    SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
    PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
    if [ -L "${PROJECT_ROOT}/host-hardening/snapshot-active" ]; then
        rm -f "${PROJECT_ROOT}/host-hardening/snapshot-active"
    fi
else
    echo -e "${RED}Revert failed. Hardening may still be active.${NC}" >&2
    exit 1
fi
