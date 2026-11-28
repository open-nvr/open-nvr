// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestTheUploadStopsAtItsSizeOrItsTime(t *testing.T) {
	r := &timedReader{block: make([]byte, 1000), left: 2500, deadline: time.Now().Add(time.Hour)}
	if n, _ := io.Copy(io.Discard, r); n != 2500 {
		t.Fatalf("sent %d, want the 2500 cap", n)
	}
	r = &timedReader{block: make([]byte, 1000), left: 1 << 40, deadline: time.Now().Add(-time.Second)}
	if n, _ := io.Copy(io.Discard, r); n != 0 {
		t.Fatalf("sent %d after the deadline", n)
	}
}

func TestSpeedTestUploadsForTheTestAndReturnsTheServersMeasurement(t *testing.T) {
	oldFor, oldMax := speedTestFor, speedTestMax
	speedTestFor, speedTestMax = 300*time.Millisecond, 1<<20
	defer func() { speedTestFor, speedTestMax = oldFor, oldMax }()
	var got int64
	var path, auth string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path, auth = r.URL.Path, r.Header.Get("Authorization")
		got, _ = io.Copy(io.Discard, r.Body)
		_ = json.NewEncoder(w).Encode(UplinkResult{Kbps: 1234.5, Bytes: got, Seconds: 0.3})
	}))
	defer srv.Close()
	c := NewClient(srv.URL, "site_3_x", "", true)
	res, err := c.SpeedTest(context.Background(), 42)
	if err != nil {
		t.Fatal(err)
	}
	if path != "/api/v1/site-agent/speedtest/42" || auth != "Bearer site_3_x" {
		t.Fatalf("path %q auth %q", path, auth)
	}
	if got == 0 || got > 1<<20 || res.Kbps != 1234.5 || res.Bytes != got {
		t.Fatalf("uploaded %d, result %+v", got, res)
	}
}
