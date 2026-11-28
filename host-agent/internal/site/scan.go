// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"sort"
	"strings"
	"sync"
	"time"
)

// A camera scan finds cameras that ONVIF discovery does not (ONVIF switched
// off, a camera on another multicast segment of the same subnet, an NVR):
// a TCP connect to a few camera ports on the subnets this machine is on,
// once, when an admin asks for it on the Sites page (never on its own).
//
// Only what looks like a camera is kept: a device that answers RTSP on 554
// or 8554, or has a camera vendor's own port open (Dahua 37777, Xiongmai
// 34567, Hikvision 8000). Routers, NAS boxes and printers answer none of
// these, so they never join the agent's list of devices camera requests may
// go to. Ports like 80 or 443 are not scanned at all.

// ScannedDevice is one camera-like device the scan found.
type ScannedDevice struct {
	IP    string `json:"ip"`
	Ports []int  `json:"ports"`
	// RTSP: it answered an RTSP OPTIONS (on Ports' 554/8554).
	RTSP bool `json:"rtsp"`
}

// CameraScan is the last scan's result, as reported.
type CameraScan struct {
	ID        int64           `json:"id"` // the request it answers
	ScannedAt float64         `json:"scanned_at"`
	Devices   []ScannedDevice `json:"devices"`
	// Subnets scanned (CIDR), and why some were not.
	Subnets []string `json:"subnets"`
	Note    string   `json:"note,omitempty"`
}

var (
	scanRTSPPorts   = []int{554, 8554}
	scanVendorPorts = []int{37777, 34567, 8000}
)

const (
	scanParallel  = 32
	scanConnectTO = 500 * time.Millisecond
	// scanMaxHosts: a subnet larger than a /24 is scanned only in the /24
	// around this machine's own address.
	scanMaxHosts = 254
)

// scanTargets: every host address on this machine's own camera networks
// (the target policy's), at most a /24 each, without this machine and the
// routers (the policy refuses those).
func (p *TargetPolicy) scanTargets() ([]net.IP, []string, string) {
	p.mu.Lock()
	p.refreshLocked()
	local := append([]*net.IPNet(nil), p.local...)
	selfs := make([]net.IP, 0, len(p.self))
	for s := range p.self {
		if ip := net.ParseIP(s); ip != nil {
			selfs = append(selfs, ip)
		}
	}
	p.mu.Unlock()
	var out []net.IP
	var subnets []string
	note := ""
	seen := map[string]bool{}
	for _, n := range local {
		v4 := n.IP.To4()
		if v4 == nil {
			continue // IPv6 subnets are far too large to scan
		}
		ones, _ := n.Mask.Size()
		base := n
		if ones < 24 {
			// Bigger than a /24: the /24 around this machine's own address.
			for _, s := range selfs {
				if s4 := s.To4(); s4 != nil && n.Contains(s4) {
					base = &net.IPNet{IP: s4.Mask(net.CIDRMask(24, 32)), Mask: net.CIDRMask(24, 32)}
					note = "subnets larger than a /24 are scanned in the /24 around this machine only"
					break
				}
			}
		}
		ones, _ = base.Mask.Size()
		if ones < 24 {
			continue
		}
		subnets = append(subnets, base.String())
		start := ipToU32(base.IP.To4())
		size := uint32(1) << (32 - ones)
		for i := uint32(1); i+1 < size && len(out) < 4*scanMaxHosts; i++ {
			ip := u32ToIP(start + i)
			if !seen[ip.String()] {
				seen[ip.String()] = true
				out = append(out, ip)
			}
		}
	}
	return out, subnets, note
}

func ipToU32(ip net.IP) uint32 {
	return uint32(ip[0])<<24 | uint32(ip[1])<<16 | uint32(ip[2])<<8 | uint32(ip[3])
}

func u32ToIP(v uint32) net.IP { return net.IPv4(byte(v>>24), byte(v>>16), byte(v>>8), byte(v)) }

// ScanCameras runs one scan.
func ScanCameras(ctx context.Context, p *TargetPolicy, id int64) *CameraScan {
	targets, subnets, note := p.scanTargets()
	type job struct {
		ip   net.IP
		port int
	}
	jobs := make(chan job)
	var mu sync.Mutex
	found := map[string]*ScannedDevice{}
	var wg sync.WaitGroup
	for w := 0; w < scanParallel; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for j := range jobs {
				if p.Check(j.ip, j.port) != nil {
					continue
				}
				isRTSPPort := j.port == 554 || j.port == 8554
				rtsp, open := probePort(ctx, j.ip, j.port, isRTSPPort)
				if !open {
					continue
				}
				if isRTSPPort && !rtsp {
					continue // open, but not RTSP: not a camera signature
				}
				mu.Lock()
				d := found[j.ip.String()]
				if d == nil {
					d = &ScannedDevice{IP: j.ip.String()}
					found[d.IP] = d
				}
				d.Ports = append(d.Ports, j.port)
				d.RTSP = d.RTSP || rtsp
				mu.Unlock()
			}
		}()
	}
	ports := append(append([]int(nil), scanRTSPPorts...), scanVendorPorts...)
feed:
	for _, ip := range targets {
		for _, port := range ports {
			select {
			case jobs <- job{ip, port}:
			case <-ctx.Done():
				break feed
			}
		}
	}
	close(jobs)
	wg.Wait()
	out := &CameraScan{ID: id, ScannedAt: nowF(), Subnets: subnets, Note: note, Devices: []ScannedDevice{}}
	for _, d := range found {
		sort.Ints(d.Ports)
		out.Devices = append(out.Devices, *d)
	}
	sort.Slice(out.Devices, func(i, j int) bool {
		return ipToU32(net.ParseIP(out.Devices[i].IP).To4()) < ipToU32(net.ParseIP(out.Devices[j].IP).To4())
	})
	return out
}

// probePort: is ip:port open, and (asked to) does it answer RTSP?
func probePort(ctx context.Context, ip net.IP, port int, askRTSP bool) (rtsp, open bool) {
	d := net.Dialer{Timeout: scanConnectTO}
	addr := net.JoinHostPort(ip.String(), fmt.Sprint(port))
	c, err := d.DialContext(ctx, "tcp", addr)
	if err != nil {
		return false, false
	}
	defer c.Close()
	if !askRTSP {
		return false, true
	}
	_ = c.SetDeadline(time.Now().Add(2 * time.Second))
	fmt.Fprintf(c, "OPTIONS rtsp://%s/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: %s\r\n\r\n", addr, nativeUserAgent)
	line, err := bufio.NewReader(c).ReadString('\n')
	return err == nil && strings.HasPrefix(line, "RTSP/1."), true
}
