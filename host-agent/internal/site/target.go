// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strconv"
	"sync"
	"time"
)

// TargetPolicy is the agent's OWN rule for where it may connect on the
// site's network: camera streams it relays and the camera requests (ONVIF,
// PTZ) core sends through it. The server names cameras, but it cannot widen
// this rule, so a compromised or malicious server can only reach what looks
// like a camera on the site's own camera network, never the router, this
// machine, other networks, or the internet.
//
// A target is allowed when its address is:
//   - not loopback, link-local, multicast or unspecified;
//   - not one of this machine's own addresses, and not a router (a next hop
//     in this machine's routing table);
//   - on a private network this machine is directly connected to (RFC 1918,
//     100.64.0.0/10, IPv6 ULA), or in a subnet listed in the agent's local
//     config (allow_subnets);
//
// and its port is a usual camera port, one the camera announced itself in
// ONVIF discovery, or one listed in the local config (allow_ports).
//
// Names are resolved and every address they resolve to must pass; the RPC
// path checks again the address it actually dialled, so a name that changes
// its answer (DNS rebinding) is still caught there.
type TargetPolicy struct {
	mu         sync.Mutex
	extraNets  []*net.IPNet
	extraPorts map[int]bool
	observed   map[string]map[int]bool // ip -> ports announced by ONVIF discovery

	local    []*net.IPNet
	self     map[string]bool
	routers  map[string]bool
	loadedAt time.Time
	frozen   bool // tests: never re-read the machine's interfaces
	logf     func(string, ...any)
}

// cameraPorts are the ports cameras and NVRs commonly serve RTSP, ONVIF or
// their HTTP APIs on.
var cameraPorts = map[int]bool{
	80: true, 81: true, 88: true, 443: true, 554: true, 322: true, 1554: true, 2020: true,
	5000: true, 7447: true, 8000: true, 8080: true, 8081: true, 8443: true, 8554: true,
	8899: true, 10080: true, 10554: true, 34567: true, 37777: true,
}

var privateNets = mustNets("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7")

func mustNets(cidrs ...string) []*net.IPNet {
	out := make([]*net.IPNet, 0, len(cidrs))
	for _, c := range cidrs {
		_, n, err := net.ParseCIDR(c)
		if err != nil {
			panic(err)
		}
		out = append(out, n)
	}
	return out
}

// ParseSubnets reads allow_subnets entries ("192.168.20.0/24", or a single
// address); bad entries are returned as errors and skipped.
func ParseSubnets(entries []string) ([]*net.IPNet, []error) {
	var nets []*net.IPNet
	var errs []error
	for _, e := range entries {
		if ip := net.ParseIP(e); ip != nil {
			bits := 32
			if ip.To4() == nil {
				bits = 128
			}
			nets = append(nets, &net.IPNet{IP: ip, Mask: net.CIDRMask(bits, bits)})
			continue
		}
		_, n, err := net.ParseCIDR(e)
		if err != nil {
			errs = append(errs, fmt.Errorf("allow_subnets: %q is not a subnet", e))
			continue
		}
		nets = append(nets, n)
	}
	return nets, errs
}

// NewTargetPolicy builds the policy from the agent's local config.
func NewTargetPolicy(allowSubnets []string, allowPorts []int, logf func(string, ...any)) *TargetPolicy {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	nets, errs := ParseSubnets(allowSubnets)
	for _, err := range errs {
		logf("%v", err)
	}
	ports := map[int]bool{}
	for _, p := range allowPorts {
		if p > 0 && p < 65536 {
			ports[p] = true
		}
	}
	return &TargetPolicy{extraNets: nets, extraPorts: ports, observed: map[string]map[int]bool{}, logf: logf}
}

// newFixedPolicy: tests, with the machine's networks given instead of read.
func newFixedPolicy(local []*net.IPNet, self, routers []string) *TargetPolicy {
	p := NewTargetPolicy(nil, nil, nil)
	p.local, p.self, p.routers, p.frozen = local, set(self), set(routers), true
	return p
}

func set(ips []string) map[string]bool {
	m := map[string]bool{}
	for _, s := range ips {
		if ip := net.ParseIP(s); ip != nil {
			m[canon(ip).String()] = true
		}
	}
	return m
}

func canon(ip net.IP) net.IP {
	if v4 := ip.To4(); v4 != nil {
		return v4
	}
	return ip
}

// SetObserved records the ports cameras announced in ONVIF discovery.
func (p *TargetPolicy) SetObserved(devices map[string][]int) {
	m := make(map[string]map[int]bool, len(devices))
	for ip, ports := range devices {
		parsed := net.ParseIP(ip)
		if parsed == nil {
			continue
		}
		ps := map[int]bool{}
		for _, port := range ports {
			if port > 0 && port < 65536 {
				ps[port] = true
			}
		}
		m[canon(parsed).String()] = ps
	}
	p.mu.Lock()
	p.observed = m
	p.mu.Unlock()
}

// refreshLocked re-reads this machine's networks, at most every 30 s (a
// DHCP renewal, a cable moved to another port).
func (p *TargetPolicy) refreshLocked() {
	if p.frozen || time.Since(p.loadedAt) < 30*time.Second {
		return
	}
	p.loadedAt = time.Now()
	var local []*net.IPNet
	self := map[string]bool{}
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, a := range addrs {
			n, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			self[canon(n.IP).String()] = true
			if isPrivate(n.IP) && !n.IP.IsLoopback() && !n.IP.IsLinkLocalUnicast() {
				local = append(local, &net.IPNet{IP: n.IP.Mask(n.Mask), Mask: n.Mask})
			}
		}
	}
	routers := map[string]bool{}
	for _, ip := range routerAddrs() {
		routers[canon(ip).String()] = true
	}
	p.local, p.self, p.routers = local, self, routers
}

func isPrivate(ip net.IP) bool {
	for _, n := range privateNets {
		if n.Contains(ip) {
			return true
		}
	}
	return false
}

// ErrTarget is why a target was refused.
type ErrTarget struct{ Reason string }

func (e *ErrTarget) Error() string { return e.Reason }

func refuse(format string, args ...any) error {
	return &ErrTarget{Reason: fmt.Sprintf(format, args...)}
}

// Check says whether the agent may connect to ip:port.
func (p *TargetPolicy) Check(ip net.IP, port int) error {
	if ip == nil {
		return refuse("no address")
	}
	ip = canon(ip)
	if ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() || ip.IsUnspecified() {
		return refuse("%s is a loopback, link-local or multicast address", ip)
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	p.refreshLocked()
	key := ip.String()
	// A single address listed in allow_subnets on this machine is a
	// deliberate choice (an NVR that is also the camera network's router, a
	// camera simulator on the box itself): it passes the self and router
	// checks. A listed subnet does not, so its router stays refused.
	if !p.listedExactlyLocked(ip) {
		if p.self[key] {
			return refuse("%s is this machine's own address", ip)
		}
		if p.routers[key] {
			return refuse("%s is a router on this network, not a camera", ip)
		}
	}
	allowed := false
	for _, n := range p.extraNets {
		if n.Contains(ip) {
			allowed = true
			break
		}
	}
	if !allowed && isPrivate(ip) {
		for _, n := range p.local {
			if n.Contains(ip) {
				allowed = true
				break
			}
		}
	}
	if !allowed {
		return refuse("%s is not on this site's camera network (to allow it, add its subnet to allow_subnets in this agent's config)", ip)
	}
	if !cameraPorts[port] && !p.extraPorts[port] && !p.observed[key][port] {
		return refuse("port %d is not a camera port (to allow it, add it to allow_ports in this agent's config)", port)
	}
	return nil
}

func (p *TargetPolicy) listedExactlyLocked(ip net.IP) bool {
	for _, n := range p.extraNets {
		ones, bits := n.Mask.Size()
		if ones == bits && n.IP.Equal(ip) {
			return true
		}
	}
	return false
}

// CheckHostPort resolves host (a name or an address) and checks every
// address it gives.
func (p *TargetPolicy) CheckHostPort(ctx context.Context, host string, port int) error {
	if ip := net.ParseIP(host); ip != nil {
		return p.Check(ip, port)
	}
	if host == "" {
		return refuse("no host")
	}
	rctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	ips, err := net.DefaultResolver.LookupIP(rctx, "ip", host)
	if err != nil {
		return refuse("cannot resolve %s: %v", host, err)
	}
	if len(ips) == 0 {
		return refuse("%s has no address", host)
	}
	for _, ip := range ips {
		if err := p.Check(ip, port); err != nil {
			return err
		}
	}
	return nil
}

// CheckStreamURL: a camera stream the server asks the agent to relay must be
// rtsp:// or rtsps:// to an allowed target. Anything else (file:, http:,
// concat:, ...) would make ffmpeg read whatever the server named.
func (p *TargetPolicy) CheckStreamURL(ctx context.Context, raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return refuse("not a URL")
	}
	port := 554
	switch u.Scheme {
	case "rtsp":
	case "rtsps":
		port = 322
	default:
		return refuse("only rtsp:// and rtsps:// camera streams are relayed, not %q", u.Scheme)
	}
	if s := u.Port(); s != "" {
		n, err := strconv.Atoi(s)
		if err != nil || n <= 0 || n > 65535 {
			return refuse("bad port %q", s)
		}
		port = n
	}
	return p.CheckHostPort(ctx, u.Hostname(), port)
}

// IsTargetRefusal tells a policy refusal from a network error.
func IsTargetRefusal(err error) bool {
	var t *ErrTarget
	return errors.As(err, &t)
}
