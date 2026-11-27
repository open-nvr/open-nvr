// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"net/url"
	"strings"
	"sync"
	"time"
)

// Meter counts the bytes each MediaMTX path sends through the relay, so the
// agent knows what every stream costs the site's uplink.
type Meter struct {
	mu    sync.Mutex
	bytes map[string]uint64
	last  map[string]uint64
	at    time.Time
	kbps  map[string]float64
}

func NewMeter() *Meter {
	return &Meter{bytes: map[string]uint64{}, last: map[string]uint64{}, kbps: map[string]float64{}, at: time.Now()}
}

// Add counts n bytes sent for path.
func (m *Meter) Add(path string, n int) {
	if m == nil || path == "" || n <= 0 {
		return
	}
	m.mu.Lock()
	m.bytes[path] += uint64(n)
	m.mu.Unlock()
}

// Sample turns the bytes since the last sample into kbps per path.
func (m *Meter) Sample(now time.Time) map[string]float64 {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	secs := now.Sub(m.at).Seconds()
	if secs <= 0 {
		return copyRates(m.kbps)
	}
	rates := map[string]float64{}
	for p, b := range m.bytes {
		rates[p] = float64(b-m.last[p]) * 8 / 1000 / secs
		m.last[p] = b
	}
	m.kbps, m.at = rates, now
	return copyRates(rates)
}

// Rate is the last sampled kbps of path.
func (m *Meter) Rate(path string) float64 {
	if m == nil {
		return 0
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.kbps[path]
}

func copyRates(in map[string]float64) map[string]float64 {
	out := make(map[string]float64, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// rtspPath is the MediaMTX path in the first request ffmpeg sends on a
// relay connection ("OPTIONS rtsp://127.0.0.1:40123/cam-7?jwt=... RTSP/1.0").
func rtspPath(first []byte) string {
	s := string(first)
	if i := strings.IndexAny(s, "\r\n"); i >= 0 {
		s = s[:i]
	}
	parts := strings.Fields(s)
	if len(parts) < 2 {
		return ""
	}
	u, err := url.Parse(parts[1])
	if err != nil {
		return ""
	}
	return strings.Trim(u.Path, "/")
}
