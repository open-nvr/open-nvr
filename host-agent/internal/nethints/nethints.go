// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package nethints keeps ./data/net-hints/advertise-ips current: the host
// addresses core advertises to browsers as WebRTC ICE host candidates
// (server/services/webrtc_ice_host_service.py).
//
// The launchers write the file on every run; the LAN prober, which runs on
// the host's own network stack on Linux, rewrites it every half minute so a
// new DHCP lease or a VPN that came up reaches core without anyone running
// the launcher again. It writes advertise-ips only. host-ips (the camera
// LANs ONVIF discovery scans) stays the launcher's: it is built from the
// operator's choices, which this process cannot see.
package nethints

import (
	"bytes"
	"context"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// FileName is the advertise list inside the hints directory.
const FileName = "advertise-ips"

// Every is how often Run refreshes the file.
const Every = 30 * time.Second

// Iface is the part of a network interface Build looks at.
type Iface struct {
	Name     string
	Up       bool
	Loopback bool
	Addrs    []string // IP literals, any family
}

// skipped: Docker's own interfaces carry nothing a browser can reach.
var skippedPrefixes = []string{"docker", "br-", "veth"}

// tunnels: VPN interfaces. Their addresses ARE advertised (a viewer on the
// VPN reaches the server there) but after the LAN ones.
var tunnelPrefixes = []string{"wg", "tun", "tap", "utun", "tailscale", "zt", "nebula", "ppp", "fct"}

var cgnat = &net.IPNet{IP: net.IPv4(100, 64, 0, 0), Mask: net.CIDRMask(10, 32)}

func hasPrefix(name string, prefixes []string) bool {
	for _, p := range prefixes {
		if strings.HasPrefix(name, p) {
			return true
		}
	}
	return false
}

// Build is the advertise list: with a specific bind address (a dual-NIC
// host publishes the media ports there alone) just that address; otherwise
// every global IPv4 on an interface that is up, the default route's source
// address first, other LAN addresses next, VPN tunnels last.
func Build(ifaces []Iface, egress, bind string) []string {
	if b := strings.TrimSpace(bind); b != "" && b != "0.0.0.0" && b != "::" {
		return []string{b}
	}
	var lan, vpn []string
	seen := map[string]bool{}
	for _, ifc := range ifaces {
		if !ifc.Up || ifc.Loopback || hasPrefix(ifc.Name, skippedPrefixes) {
			continue
		}
		for _, a := range ifc.Addrs {
			ip := net.ParseIP(a).To4()
			if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsUnspecified() || ip.IsMulticast() {
				continue
			}
			s := ip.String()
			if seen[s] {
				continue
			}
			seen[s] = true
			if hasPrefix(ifc.Name, tunnelPrefixes) || cgnat.Contains(ip) {
				vpn = append(vpn, s)
			} else if s == egress {
				lan = append([]string{s}, lan...)
			} else {
				lan = append(lan, s)
			}
		}
	}
	return append(lan, vpn...)
}

// Interfaces lists this host's interfaces for Build.
func Interfaces() []Iface {
	list, err := net.Interfaces()
	if err != nil {
		return nil
	}
	out := make([]Iface, 0, len(list))
	for _, ifc := range list {
		it := Iface{
			Name:     ifc.Name,
			Up:       ifc.Flags&net.FlagUp != 0,
			Loopback: ifc.Flags&net.FlagLoopback != 0,
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			if n, ok := a.(*net.IPNet); ok {
				it.Addrs = append(it.Addrs, n.IP.String())
			}
		}
		out = append(out, it)
	}
	return out
}

// Egress is the source address the kernel would use toward the internet,
// or "" without a default route. A UDP "connect" picks the route and sends
// nothing, so it works on an air-gapped host too.
func Egress() string {
	c, err := net.Dial("udp4", "1.1.1.1:80")
	if err != nil {
		return ""
	}
	defer c.Close()
	if a, ok := c.LocalAddr().(*net.UDPAddr); ok {
		return a.IP.String()
	}
	return ""
}

// Write puts ips into dir/advertise-ips atomically, only when the content
// changes. An empty list leaves the file alone: core never advertises
// nothing over something, and neither does this.
func Write(dir string, ips []string) (bool, error) {
	if len(ips) == 0 {
		return false, nil
	}
	want := []byte(strings.Join(ips, " ") + "\n")
	path := filepath.Join(dir, FileName)
	if have, err := os.ReadFile(path); err == nil && bytes.Equal(have, want) {
		return false, nil
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, want, 0o644); err != nil {
		return false, err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return false, err
	}
	return true, nil
}

// Run refreshes dir/advertise-ips every Every until ctx ends. bind is
// NGINX_BIND_HOST.
func Run(ctx context.Context, dir, bind string, logf func(string, ...any)) {
	for {
		ips := Build(Interfaces(), Egress(), bind)
		changed, err := Write(dir, ips)
		switch {
		case err != nil:
			logf("cannot write %s: %v", filepath.Join(dir, FileName), err)
		case changed:
			logf("host addresses for WebRTC: %s", strings.Join(ips, " "))
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(Every):
		}
	}
}
