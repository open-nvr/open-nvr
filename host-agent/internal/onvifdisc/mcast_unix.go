// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build !windows

package onvifdisc

import (
	"net"
	"syscall"
)

// setMulticastInterface makes conn's multicast packets leave through the
// interface that owns local (IP_MULTICAST_IF). Linux ignores the bind for
// routing multicast; this is what selects the segment.
func setMulticastInterface(conn *net.UDPConn, local net.IP) error {
	raw, err := conn.SyscallConn()
	if err != nil {
		return err
	}
	var addr [4]byte
	copy(addr[:], local.To4())
	var serr error
	if err := raw.Control(func(fd uintptr) {
		serr = syscall.SetsockoptInet4Addr(int(fd), syscall.IPPROTO_IP, syscall.IP_MULTICAST_IF, addr)
	}); err != nil {
		return err
	}
	return serr
}
