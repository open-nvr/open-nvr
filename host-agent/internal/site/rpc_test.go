// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"encoding/base64"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

func camServer(t *testing.T, h http.HandlerFunc) (*httptest.Server, string) {
	t.Helper()
	srv := httptest.NewServer(h)
	t.Cleanup(srv.Close)
	u, _ := url.Parse(srv.URL)
	return srv, u.Hostname()
}

func TestRPCReachesAnAllowedCameraAndReturnsItsAnswer(t *testing.T) {
	var gotAuth, gotAE, gotBody string
	srv, host := camServer(t, func(w http.ResponseWriter, r *http.Request) {
		gotAuth, gotAE = r.Header.Get("Authorization"), r.Header.Get("Accept-Encoding")
		b, _ := io.ReadAll(r.Body)
		gotBody = string(b)
		w.Header().Set("Content-Type", "application/soap+xml")
		_, _ = w.Write([]byte("<ok/>"))
	})
	r := newRPCRunner(t.Logf)
	r.allowLoopback = true
	r.setAllowed([]string{host})
	res := r.run(context.Background(), RPC{
		ID: "r1", Method: "POST", URL: srv.URL + "/onvif/device_service",
		Headers: map[string][]string{"Authorization": {"Digest x"}, "Accept-Encoding": {"gzip, deflate"}},
		BodyB64: base64.StdEncoding.EncodeToString([]byte("<soap/>")),
	})
	body, _ := base64.StdEncoding.DecodeString(res.BodyB64)
	if res.Error != "" || res.Status != 200 || string(body) != "<ok/>" {
		t.Fatalf("result %+v", res)
	}
	if gotAuth != "Digest x" || gotBody != "<soap/>" {
		t.Fatalf("camera got auth %q body %q", gotAuth, gotBody)
	}
	// Accept-Encoding is Go's to set (it then decompresses for core).
	if gotAE == "gzip, deflate" {
		t.Fatal("the caller's Accept-Encoding was forwarded")
	}
	if res.Headers["Content-Type"][0] != "application/soap+xml" {
		t.Fatalf("headers %v", res.Headers)
	}
}

func TestRPCRefusesAHostThatIsNotACameraOfThisSite(t *testing.T) {
	srv, _ := camServer(t, func(w http.ResponseWriter, r *http.Request) { t.Fatal("must not be reached") })
	r := newRPCRunner(t.Logf)
	r.allowLoopback = true
	r.setAllowed([]string{"192.168.1.64"})
	if res := r.run(context.Background(), RPC{Method: "GET", URL: srv.URL + "/"}); !strings.Contains(res.Error, "not a camera of this site") {
		t.Fatalf("got %+v", res)
	}
}

func TestRPCRefusesLoopbackEvenWhenListed(t *testing.T) {
	r := newRPCRunner(t.Logf)
	r.setAllowed([]string{"127.0.0.1", "169.254.169.254"})
	for _, u := range []string{"http://127.0.0.1:8000/", "http://169.254.169.254/latest/meta-data"} {
		if res := r.run(context.Background(), RPC{Method: "GET", URL: u}); res.Error == "" {
			t.Fatalf("%s reached: %+v", u, res)
		}
	}
}

func TestRPCDoesNotFollowRedirects(t *testing.T) {
	srv, host := camServer(t, func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/elsewhere" {
			t.Fatal("redirect followed")
		}
		http.Redirect(w, r, "/elsewhere", http.StatusFound)
	})
	r := newRPCRunner(t.Logf)
	r.allowLoopback = true
	r.setAllowed([]string{host})
	if res := r.run(context.Background(), RPC{Method: "GET", URL: srv.URL + "/"}); res.Status != http.StatusFound {
		t.Fatalf("got %+v", res)
	}
}

func TestRPCRefusesATooLargeAnswer(t *testing.T) {
	srv, host := camServer(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write(make([]byte, rpcMaxResponse+10))
	})
	r := newRPCRunner(t.Logf)
	r.allowLoopback = true
	r.setAllowed([]string{host})
	if res := r.run(context.Background(), RPC{Method: "GET", URL: srv.URL + "/big"}); !strings.Contains(res.Error, "too large") {
		t.Fatalf("got status %d err %q", res.Status, res.Error)
	}
}

func TestApplyAllowsExactlyTheRelayedCamerasHosts(t *testing.T) {
	rec := &recorder{}
	a := testAgent(rec)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer a.stopAll()
	a.apply(ctx, &Answer{Desired: []Desired{
		{CameraID: 7, StreamName: "cam-7", MainURL: "rtsp://u:p@192.168.1.64:554/main"},
	}})
	if !a.rpc.isAllowed("192.168.1.64") || a.rpc.isAllowed("192.168.1.65") {
		t.Fatal("allowlist does not match the relayed cameras")
	}
	a.mu.Lock()
	a.scan = &OnvifScan{Devices: nil}
	a.mu.Unlock()
	a.apply(ctx, &Answer{})
	if a.rpc.isAllowed("192.168.1.64") {
		t.Fatal("an unlinked camera's host stayed allowed")
	}
}
