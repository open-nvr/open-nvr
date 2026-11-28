// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"net"
	"unsafe"

	"golang.org/x/sys/windows"
)

// mibIPForwardRow is MIB_IPFORWARDROW (iphlpapi): 14 DWORDs.
type mibIPForwardRow struct {
	Dest, Mask, Policy, NextHop, IfIndex, Type, Proto, Age, NextHopAS uint32
	Metric1, Metric2, Metric3, Metric4, Metric5                       uint32
}

// iphlpapi from System32 only (NewLazySystemDLL), never from the current
// directory.
var procGetBestRoute = windows.NewLazySystemDLL("iphlpapi.dll").NewProc("GetBestRoute")

// routerAddrs: the next hop of the default route (the network's router),
// so the agent never relays to its admin page.
func routerAddrs() []net.IP {
	if procGetBestRoute.Find() != nil {
		return nil
	}
	var row mibIPForwardRow
	// Destination 8.8.8.8 as an in_addr (network byte order in memory); only
	// asks the routing table, nothing is sent.
	dest := uint32(8) | uint32(8)<<8 | uint32(8)<<16 | uint32(8)<<24
	r, _, _ := procGetBestRoute.Call(uintptr(dest), 0, uintptr(unsafe.Pointer(&row)))
	if r != 0 {
		return nil
	}
	h := row.NextHop
	ip := net.IPv4(byte(h), byte(h>>8), byte(h>>16), byte(h>>24))
	if ip.IsUnspecified() {
		return nil
	}
	return []net.IP{ip}
}
