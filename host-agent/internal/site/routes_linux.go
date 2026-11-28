// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bufio"
	"io"
	"net"
	"os"
)

// routerAddrs: every IPv4 next hop in the routing table (/proc/net/route),
// so the agent never relays to a router's admin page.
func routerAddrs() []net.IP {
	f, err := os.Open("/proc/net/route")
	if err != nil {
		return nil
	}
	defer f.Close()
	return parseProcRoute(f)
}

func parseProcRoute(r io.Reader) []net.IP {
	sc := bufio.NewScanner(r)
	var out []net.IP
	first := true
	for sc.Scan() {
		if first { // header line
			first = false
			continue
		}
		if ip := procRouteGateway(sc.Text()); ip != nil {
			out = append(out, ip)
		}
	}
	return out
}
