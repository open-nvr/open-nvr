// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"time"
)

// An uplink test measures how much the site can still send: when an admin
// asks for one (UplinkTest in the answer), the agent uploads throwaway bytes
// to the server for speedTestFor or speedTestMax bytes, whichever comes
// first, and the server measures the rate. It runs beside the relayed
// cameras, so it is the upload left over, which is what more cameras need.

var (
	speedTestFor = 5 * time.Second
	speedTestMax = int64(10 << 20)
)

// timedReader sends block over and over until max bytes or the deadline.
type timedReader struct {
	block    []byte
	off      int
	left     int64
	deadline time.Time
}

func (r *timedReader) Read(p []byte) (int, error) {
	if r.left <= 0 || time.Now().After(r.deadline) {
		return 0, io.EOF
	}
	n := 0
	for n < len(p) && r.left > 0 {
		c := copy(p[n:], r.block[r.off:])
		if int64(c) > r.left {
			c = int(r.left)
		}
		n += c
		r.left -= int64(c)
		r.off = (r.off + c) % len(r.block)
	}
	return n, nil
}

// UplinkResult is what the server measured.
type UplinkResult struct {
	Kbps    float64 `json:"kbps"`
	Bytes   int64   `json:"bytes"`
	Seconds float64 `json:"seconds"`
}

// SpeedTest uploads for the test id and returns the server's measurement.
func (c *Client) SpeedTest(ctx context.Context, id int64) (*UplinkResult, error) {
	block := make([]byte, 64<<10)
	if _, err := rand.Read(block); err != nil { // incompressible
		return nil, err
	}
	body := &timedReader{block: block, left: speedTestMax, deadline: time.Now().Add(speedTestFor)}
	ctx, cancel := context.WithTimeout(ctx, speedTestFor+30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, fmt.Sprintf("%s/api/v1/site-agent/speedtest/%d", c.Server, id), body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+c.Token)
	req.Header.Set("Content-Type", "application/octet-stream")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("uplink test: HTTP %d", resp.StatusCode)
	}
	var out UplinkResult
	if err := decodeJSON(resp.Body, &out); err != nil {
		return nil, err
	}
	return &out, nil
}

// runUplinkTest runs one test an admin asked for and logs the result.
func (a *Agent) runUplinkTest(ctx context.Context, id int64) {
	a.o.Logf("uplink test: uploading for up to %s", speedTestFor)
	res, err := a.client.SpeedTest(ctx, id)
	if err != nil {
		if ctx.Err() == nil {
			a.o.Logf("uplink test: %v", err)
		}
		return
	}
	a.o.Logf("uplink test: %.1f Mbit/s free (%d bytes in %.1f s)", res.Kbps/1000, res.Bytes, res.Seconds)
}

func decodeJSON(r io.Reader, out any) error {
	return json.NewDecoder(io.LimitReader(r, 1<<20)).Decode(out)
}
