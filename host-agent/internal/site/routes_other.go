// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build !linux && !windows

package site

import "net"

// routerAddrs is not read on this platform (macOS): the router is not
// refused by that rule here, only by the others (camera ports, the site's
// own network). Linux and Windows, where site boxes run, read it.
func routerAddrs() []net.IP { return nil }
