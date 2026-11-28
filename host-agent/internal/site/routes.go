// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"encoding/hex"
	"net"
	"strconv"
	"strings"
)

// procRouteGateway reads one line of Linux's /proc/net/route and returns its
// next hop, or nil when the route has none. The table prints each address as
// the hex of a host-order (little-endian) 32-bit value: "0101A8C0" is
// 192.168.1.1. Kept free of build tags so it is tested on every platform.
func procRouteGateway(line string) net.IP {
	const rtfGateway = 0x2
	f := strings.Fields(line)
	if len(f) < 4 {
		return nil
	}
	flags, err := strconv.ParseUint(f[3], 16, 32)
	if err != nil || flags&rtfGateway == 0 {
		return nil
	}
	b, err := hex.DecodeString(f[2])
	if err != nil || len(b) != 4 {
		return nil
	}
	ip := net.IPv4(b[3], b[2], b[1], b[0])
	if ip.IsUnspecified() {
		return nil
	}
	return ip
}
