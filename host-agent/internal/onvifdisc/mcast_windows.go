// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build windows

package onvifdisc

import (
	"net"
	"syscall"
)

// setMulticastInterface: see mcast_unix.go. Windows picks the egress from
// the bound address already; setting it explicitly is harmless.
func setMulticastInterface(conn *net.UDPConn, local net.IP) error {
	raw, err := conn.SyscallConn()
	if err != nil {
		return err
	}
	var addr [4]byte
	copy(addr[:], local.To4())
	var serr error
	if err := raw.Control(func(fd uintptr) {
		serr = syscall.SetsockoptInet4Addr(syscall.Handle(fd), syscall.IPPROTO_IP, syscall.IP_MULTICAST_IF, addr)
	}); err != nil {
		return err
	}
	return serr
}
