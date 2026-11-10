// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package onvifdisc finds ONVIF cameras with WS-Discovery: one multicast
// Probe to 239.255.255.250:3702 on each LAN interface, and every camera on
// that network segment answers with its device-service address.
//
// This is why discovery runs in the host agent: from a container on a
// Docker bridge (and from Docker Desktop's VM at all) multicast never
// reaches the LAN, so core can only sweep subnets address by address. Here,
// on the host, a probe takes a few seconds and needs no subnet configured.
// It does not cross routers; core's unicast sweep still covers routed
// camera networks.
package onvifdisc

import (
	"context"
	"crypto/rand"
	"encoding/xml"
	"fmt"
	"net"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Device is one camera that answered a probe.
type Device struct {
	IP       string   `json:"ip"`
	Port     int      `json:"port"`
	Scheme   string   `json:"scheme"`
	XAddrs   []string `json:"xaddrs"`
	Name     string   `json:"name,omitempty"`
	Hardware string   `json:"hardware,omitempty"`
	Location string   `json:"location,omitempty"`
	Endpoint string   `json:"endpoint,omitempty"`
}

var multicastAddr = &net.UDPAddr{IP: net.IPv4(239, 255, 255, 250), Port: 3702}

// Two probes: some cameras only answer a probe for NetworkVideoTransmitter,
// others only for the generic Device type.
var probeTypes = []string{
	`<d:Types xmlns:dn="http://www.onvif.org/ver10/network/wsdl">dn:NetworkVideoTransmitter</d:Types>`,
	`<d:Types xmlns:tds="http://www.onvif.org/ver10/device/wsdl">tds:Device</d:Types>`,
}

func uuid() string {
	var b [16]byte
	_, _ = rand.Read(b[:])
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

// ProbeMessage is a WS-Discovery Probe (SOAP 1.2, WS-Addressing 2004/08).
func ProbeMessage(types string) []byte {
	return []byte(`<?xml version="1.0" encoding="UTF-8"?>` +
		`<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" ` +
		`xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" ` +
		`xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">` +
		`<s:Header>` +
		`<a:Action s:mustUnderstand="1">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>` +
		`<a:MessageID>uuid:` + uuid() + `</a:MessageID>` +
		`<a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>` +
		`<a:To s:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>` +
		`</s:Header>` +
		`<s:Body><d:Probe>` + types + `</d:Probe></s:Body>` +
		`</s:Envelope>`)
}

// The parts of a ProbeMatches reply we need, matched by local name so any
// namespace prefix a camera uses works.
type probeMatch struct {
	Endpoint string `xml:"EndpointReference>Address"`
	Types    string `xml:"Types"`
	Scopes   string `xml:"Scopes"`
	XAddrs   string `xml:"XAddrs"`
}

type envelope struct {
	Matches []probeMatch `xml:"Body>ProbeMatches>ProbeMatch"`
}

// Bounds for what a reply can put into a report. Replies are unauthenticated
// UDP from anything on the LAN; nothing in one may be able to make core
// refuse the agent's sync (core rejects oversized fields), so everything is
// clamped here, and core clamps again.
const (
	MaxDevices  = 256
	maxXAddrs   = 4
	maxURLLen   = 300
	maxFieldLen = 200
)

// ParseProbeMatches reads one reply datagram. source is the address it came
// from, and it is the camera's address: a reply cannot list a camera under
// any other host (XAddrs naming another address are dropped), so a device on
// the LAN cannot plant an entry that points users at someone else.
func ParseProbeMatches(data []byte, source net.IP) []Device {
	src := source.To4()
	if src == nil {
		return nil
	}
	var env envelope
	if err := xml.Unmarshal(data, &env); err != nil {
		return nil
	}
	var out []Device
	for _, m := range env.Matches {
		if !isCamera(m) {
			continue
		}
		d := Device{IP: src.String(), Endpoint: clip(m.Endpoint)}
		for _, x := range strings.Fields(m.XAddrs) {
			if len(x) > maxURLLen || len(d.XAddrs) >= maxXAddrs {
				continue
			}
			u, err := url.Parse(x)
			if err != nil || (u.Scheme != "http" && u.Scheme != "https") {
				continue
			}
			ip := net.ParseIP(u.Hostname())
			if ip == nil || !ip.Equal(src) {
				continue // another host, an IPv6 or a link-local address
			}
			port := 80
			if u.Scheme == "https" {
				port = 443
			}
			if p := u.Port(); p != "" {
				n, err := strconv.Atoi(p)
				if err != nil || n < 1 || n > 65535 {
					continue
				}
				port = n
			}
			if len(d.XAddrs) == 0 {
				d.Scheme, d.Port = u.Scheme, port
			}
			d.XAddrs = append(d.XAddrs, x)
		}
		if len(d.XAddrs) == 0 {
			// Announced only addresses we will not use: report the host it
			// answered from, on the standard ONVIF path.
			d.Scheme, d.Port = "http", 80
		}
		for _, s := range strings.Fields(m.Scopes) {
			key, val := scope(s)
			switch key {
			case "name":
				d.Name = clip(val)
			case "hardware":
				d.Hardware = clip(val)
			case "location":
				if d.Location == "" {
					d.Location = clip(val)
				}
			}
		}
		out = append(out, d)
	}
	return out
}

// clip makes a reply's text safe to report: printable, trimmed, bounded.
func clip(s string) string {
	s = strings.Map(func(r rune) rune {
		if r < 0x20 || r == 0x7f {
			return -1
		}
		return r
	}, strings.TrimSpace(s))
	if r := []rune(s); len(r) > maxFieldLen {
		s = string(r[:maxFieldLen])
	}
	return s
}

// isCamera: printers and other WS-Discovery devices answer probes too, even
// probes for another type (an HP printer here answered with its own service
// on :3911). A camera declares the ONVIF NetworkVideoTransmitter type or
// serves an ONVIF device service; anything else is not offered as a camera.
func isCamera(m probeMatch) bool {
	if strings.Contains(m.Types, "NetworkVideoTransmitter") {
		return true
	}
	for _, x := range strings.Fields(m.XAddrs) {
		if strings.Contains(strings.ToLower(x), "/onvif") {
			return true
		}
	}
	return false
}

// scope turns "onvif://www.onvif.org/name/Front%20Door" into ("name", "Front Door").
func scope(s string) (string, string) {
	const prefix = "onvif://www.onvif.org/"
	if !strings.HasPrefix(strings.ToLower(s), prefix) {
		return "", ""
	}
	rest := s[len(prefix):]
	i := strings.IndexByte(rest, '/')
	if i < 0 {
		return "", ""
	}
	val, err := url.PathUnescape(rest[i+1:])
	if err != nil {
		val = rest[i+1:]
	}
	return strings.ToLower(rest[:i]), strings.TrimSpace(strings.ReplaceAll(val, "_", " "))
}

// Interfaces are the local IPv4 addresses to probe from: up, not loopback,
// private (a camera LAN), and not an obviously virtual adapter (Hyper-V/WSL,
// Docker, VirtualBox), where nothing would answer.
func Interfaces() []net.IP {
	ifs, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []net.IP
	for _, ifc := range ifs {
		if ifc.Flags&net.FlagUp == 0 || ifc.Flags&net.FlagLoopback != 0 || ifc.Flags&net.FlagMulticast == 0 {
			continue
		}
		n := strings.ToLower(ifc.Name)
		if strings.Contains(n, "vethernet") || strings.HasPrefix(n, "docker") || strings.HasPrefix(n, "br-") ||
			strings.HasPrefix(n, "veth") || strings.HasPrefix(n, "virbr") || strings.Contains(n, "virtualbox") ||
			strings.Contains(n, "vmware") {
			continue
		}
		addrs, _ := ifc.Addrs()
		for _, a := range addrs {
			ipn, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipn.IP.To4()
			if ip == nil || !ip.IsPrivate() {
				continue
			}
			out = append(out, ip)
		}
	}
	return out
}

// Probe sends the probes from every interface and collects answers for wait.
func Probe(ctx context.Context, wait time.Duration) []Device {
	var (
		mu    sync.Mutex
		found = map[string]Device{}
		wg    sync.WaitGroup
	)
	for _, local := range Interfaces() {
		wg.Add(1)
		go func(local net.IP) {
			defer wg.Done()
			// Bound to the interface's own address, so the multicast goes
			// out of that interface and the replies come back to it.
			conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: local, Port: 0})
			if err != nil {
				return
			}
			defer conn.Close()
			for _, t := range probeTypes {
				msg := ProbeMessage(t)
				_, _ = conn.WriteToUDP(msg, multicastAddr)
				time.Sleep(50 * time.Millisecond)
				_, _ = conn.WriteToUDP(msg, multicastAddr) // UDP: say it twice
			}
			deadline := time.Now().Add(wait)
			if d, ok := ctx.Deadline(); ok && d.Before(deadline) {
				deadline = d
			}
			_ = conn.SetReadDeadline(deadline)
			buf := make([]byte, 64*1024)
			for {
				n, from, err := conn.ReadFromUDP(buf)
				if err != nil {
					return
				}
				for _, d := range ParseProbeMatches(buf[:n], from.IP) {
					mu.Lock()
					prev, seen := found[d.IP]
					// A bounded round: replies are anyone's UDP on the LAN.
					if (!seen && len(found) < MaxDevices) || (seen && prev.Name == "" && d.Name != "") {
						found[d.IP] = d
					}
					mu.Unlock()
				}
			}
		}(local)
	}
	wg.Wait()
	out := make([]Device, 0, len(found))
	for _, d := range found {
		out = append(out, d)
	}
	sort.Slice(out, func(i, j int) bool { return ipLess(out[i].IP, out[j].IP) })
	return out
}

func ipLess(a, b string) bool {
	x, y := net.ParseIP(a).To4(), net.ParseIP(b).To4()
	if x == nil || y == nil {
		return a < b
	}
	for i := range x {
		if x[i] != y[i] {
			return x[i] < y[i]
		}
	}
	return false
}
