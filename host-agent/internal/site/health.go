// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/bluenviron/gortsplib/v5"
	"github.com/bluenviron/gortsplib/v5/pkg/base"
)

// Health is the site machine's own state, in every report: the server
// turns it into plain problems (a clock that is off, a hot box, a full disk).
type Health struct {
	TempC      *float64 `json:"temp_c,omitempty"`
	DiskFreeMB *float64 `json:"disk_free_mb,omitempty"`
}

// machineHealth reads what it can on this platform; missing is fine.
func machineHealth(stateDir string) *Health {
	h := &Health{}
	if t, ok := cpuTempC(); ok {
		h.TempC = &t
	}
	dir := stateDir
	if dir == "" {
		dir = os.TempDir()
	}
	if mb, ok := diskFreeMB(dir); ok {
		h.DiskFreeMB = &mb
	}
	return h
}

// cpuTempC: the hottest of Linux's thermal zones (a Raspberry Pi's SoC).
func cpuTempC() (float64, bool) {
	zones, _ := filepath.Glob("/sys/class/thermal/thermal_zone*/temp")
	best, found := 0.0, false
	for _, z := range zones {
		b, err := os.ReadFile(z)
		if err != nil {
			continue
		}
		milli, err := strconv.ParseFloat(strings.TrimSpace(string(b)), 64)
		if err != nil || milli <= 0 || milli > 200000 {
			continue
		}
		if c := milli / 1000; !found || c > best {
			best, found = c, true
		}
	}
	return best, found
}

// ChecksResult is what "Run checks" found, in the report.
type ChecksResult struct {
	ID    int64       `json:"id"`
	At    float64     `json:"at"`
	Items []CheckItem `json:"items"`
}

// CheckItem is one check, in plain words.
type CheckItem struct {
	Name   string `json:"name"`
	OK     bool   `json:"ok"`
	Detail string `json:"detail,omitempty"`
	MS     int64  `json:"ms,omitempty"`
}

const checkTimeout = 8 * time.Second

// runChecks tests, from the site: the server's name, the server, the video
// route, and each relayed camera (a connection, then its stream).
func (a *Agent) runChecks(ctx context.Context, id int64) {
	var items []CheckItem
	add := func(name string, start time.Time, err error, okDetail string) {
		it := CheckItem{Name: name, OK: err == nil, MS: time.Since(start).Milliseconds()}
		if err != nil {
			it.Detail = redactURL(err.Error())
		} else {
			it.Detail = okDetail
		}
		items = append(items, it)
	}
	host := a.o.Config.Hostname()
	if net.ParseIP(host) == nil {
		t := time.Now()
		ips, err := net.DefaultResolver.LookupHost(ctx, host)
		add("The server's name ("+host+")", t, err, strings.Join(ips, ", "))
	}
	port := "443"
	if u, err := url.Parse(a.o.Config.Server); err == nil && u.Port() != "" {
		port = u.Port()
	}
	t := time.Now()
	add("The server (HTTPS)", t, a.dialPinned(ctx, net.JoinHostPort(host, port), ""), "reached, and its key is the one this agent trusts")

	a.mu.Lock()
	ingest := a.ingest
	want := make([]wantStream, 0, len(a.want))
	for _, w := range a.want {
		if w.kind == "main" {
			want = append(want, w)
		}
	}
	a.mu.Unlock()
	if ingest.Host != "" {
		p := ingest.Port
		if p == 0 {
			p = 443
		}
		t = time.Now()
		add("The video route", t, a.dialPinned(ctx, net.JoinHostPort(ingest.Host, strconv.Itoa(p)), ingest.ALPN), "open")
	}
	for _, w := range want {
		name := fmt.Sprintf("Camera %d", w.cameraID)
		t = time.Now()
		add(name, t, a.checkCamera(ctx, w.url), "its stream answers")
	}
	res := &ChecksResult{ID: id, At: nowF(), Items: items}
	a.mu.Lock()
	a.checks = res
	a.mu.Unlock()
	if _, err := a.client.Sync(ctx, a.report()); err != nil && ctx.Err() == nil {
		a.o.Logf("reporting the checks: %v", err)
	}
}

func (a *Agent) dialPinned(ctx context.Context, addr, alpn string) error {
	cctx, cancel := context.WithTimeout(ctx, checkTimeout)
	defer cancel()
	cfg := PinnedTLSConfig(a.o.Config.Hostname(), a.pins, alpn)
	if a.o.Config.Insecure {
		cfg = TLSConfig(a.o.Config.Hostname(), "", true, alpn)
	}
	d := tls.Dialer{Config: cfg}
	c, err := d.DialContext(cctx, "tcp", addr)
	if err != nil {
		var ue *ErrUntrusted
		if errors.As(err, &ue) {
			return errors.New("something in between replaced the server's certificate (a firewall that inspects HTTPS?)")
		}
		return err
	}
	defer c.Close()
	if alpn != "" && c.(*tls.Conn).ConnectionState().NegotiatedProtocol != alpn {
		return errors.New("the server (or something in front of it) does not take the video route on this port")
	}
	return nil
}

// checkCamera: the agent's own rule, a connection, then a DESCRIBE with
// the camera's credentials (as the relay would start).
func (a *Agent) checkCamera(ctx context.Context, lanURL string) error {
	if a.policy != nil {
		if err := a.policy.CheckStreamURL(ctx, lanURL); err != nil {
			return fmt.Errorf("not relayed: %w", err)
		}
	}
	u, err := base.ParseURL(lanURL)
	if err != nil {
		return err
	}
	tcp := gortsplib.ProtocolTCP
	c := &gortsplib.Client{Scheme: u.Scheme, Host: u.Host, Protocol: &tcp, ReadTimeout: checkTimeout,
		WriteTimeout: checkTimeout, UserAgent: nativeUserAgent,
		TLSConfig: &tls.Config{InsecureSkipVerify: true}} //nolint:gosec // LAN cameras are self-signed
	if a.policy != nil {
		c.DialContext = a.policy.Dial
	}
	if err := c.Start(); err != nil {
		return err
	}
	defer c.Close()
	_, _, err = c.Describe(u)
	return err
}
