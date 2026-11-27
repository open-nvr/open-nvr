// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/publisher"
)

// ── TLS ────────────────────────────────────────────────────────────────

func selfSignedTLSServer(t *testing.T, alpn []string, handle func(net.Conn)) (addr string, pin string) {
	t.Helper()
	srv := httptest.NewUnstartedServer(http.NotFoundHandler())
	srv.StartTLS() // only to get a self-signed certificate for 127.0.0.1
	cert := srv.TLS.Certificates[0]
	srv.Close()
	leaf := cert.Leaf
	if leaf == nil {
		var err error
		leaf, err = parseLeaf(cert)
		if err != nil {
			t.Fatal(err)
		}
	}
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{Certificates: []tls.Certificate{cert}, NextProtos: alpn})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go handle(c)
		}
	}()
	return ln.Addr().String(), Pin(leaf)
}

func TestTLSConfigTrustsAMatchingPinAndNamesTheServersPinOtherwise(t *testing.T) {
	addr, pin := selfSignedTLSServer(t, nil, func(c net.Conn) {
		_ = c.(*tls.Conn).Handshake()
		c.Close()
	})
	// No pin: a self-signed server is refused, and the error carries its pin.
	_, err := tls.Dial("tcp", addr, TLSConfig("127.0.0.1", "", false, ""))
	var ue *ErrUntrusted
	if !errors.As(err, &ue) || ue.ServerPin != pin {
		t.Fatalf("want ErrUntrusted with the server's pin, got %v", err)
	}
	// The right pin: accepted.
	c, err := tls.Dial("tcp", addr, TLSConfig("127.0.0.1", pin, false, ""))
	if err != nil {
		t.Fatalf("pinned dial: %v", err)
	}
	c.Close()
	// A wrong pin: refused.
	if _, err := tls.Dial("tcp", addr, TLSConfig("127.0.0.1", "sha256/AAAA", false, "")); err == nil {
		t.Fatal("a wrong pin must not be accepted")
	}
}

// ── relay ──────────────────────────────────────────────────────────────

func TestRelayCarriesBytesOverTLSWithTheIngestALPN(t *testing.T) {
	got := make(chan string, 1)
	addr, pin := selfSignedTLSServer(t, []string{IngestALPN}, func(c net.Conn) {
		defer c.Close()
		tc := c.(*tls.Conn)
		_ = tc.Handshake()
		if tc.ConnectionState().NegotiatedProtocol != IngestALPN {
			got <- "no alpn"
			return
		}
		buf := make([]byte, 64)
		n, _ := c.Read(buf)
		got <- string(buf[:n])
		_, _ = c.Write([]byte("RTSP/1.0 200 OK\r\n\r\n"))
	})
	host, port := splitHostPort(t, addr)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r, err := StartRelay(ctx, host, port, TLSConfig(host, pin, false, IngestALPN), t.Logf)
	if err != nil {
		t.Fatal(err)
	}
	c, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", itoaT(r.Port())))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_, _ = c.Write([]byte("OPTIONS rtsp://x/ RTSP/1.0\r\n"))
	select {
	case s := <-got:
		if !strings.HasPrefix(s, "OPTIONS") {
			t.Fatalf("server got %q", s)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("nothing reached the server")
	}
	reply, _ := io.ReadAll(io.LimitReader(c, 64))
	if !strings.HasPrefix(string(reply), "RTSP/1.0 200") {
		t.Fatalf("reply %q", reply)
	}
}

func TestRelayRefusesAServerThatDidNotTakeTheALPN(t *testing.T) {
	reached := make(chan struct{}, 1)
	addr, pin := selfSignedTLSServer(t, nil, func(c net.Conn) {
		defer c.Close()
		_ = c.(*tls.Conn).Handshake()
		buf := make([]byte, 64)
		if n, _ := c.Read(buf); n > 0 {
			reached <- struct{}{}
		}
	})
	host, port := splitHostPort(t, addr)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r, _ := StartRelay(ctx, host, port, TLSConfig(host, pin, false, IngestALPN), t.Logf)
	c, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", itoaT(r.Port())))
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_, _ = c.Write([]byte("ANNOUNCE rtsp://x/ RTSP/1.0\r\n"))
	select {
	case <-reached:
		t.Fatal("video was sent to a server that did not accept the ingest ALPN")
	case <-time.After(1 * time.Second):
	}
}

// ── ffmpeg command line ───────────────────────────────────────────────

func TestArgsCopyTheCameraAndPublishToTheRelayWithTheToken(t *testing.T) {
	a := strings.Join(Args("rtsp://u:p@192.168.1.64:554/main", 40123, "cam-7", "TOK"), " ")
	for _, want := range []string{
		"-rtsp_transport tcp -timeout 10000000 -i rtsp://u:p@192.168.1.64:554/main",
		"-map 0:v:0 -c:v copy -an",
		"-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:40123/cam-7?jwt=TOK",
	} {
		if !strings.Contains(a, want) {
			t.Fatalf("args lack %q:\n%s", want, a)
		}
	}
	if strings.Contains(a, "libx264") {
		t.Fatal("a site camera must not be re-encoded")
	}
}

func TestRedactURLHidesCameraCredentials(t *testing.T) {
	got := redactURL("rtsp://admin:p%40ss@192.168.1.64/main: Connection refused")
	if strings.Contains(got, "p%40ss") || !strings.Contains(got, "rtsp://***@192.168.1.64") {
		t.Fatalf("got %q", got)
	}
}

// ── supervisor and reconcile ──────────────────────────────────────────

type fakeProc struct {
	done chan struct{}
	once sync.Once
}

func (p *fakeProc) Wait() error         { <-p.done; return errors.New("killed") }
func (p *fakeProc) Kill()               { p.once.Do(func() { close(p.done) }) }
func (p *fakeProc) LastLines() []string { return nil }

type recorder struct {
	mu    sync.Mutex
	runs  [][]string
	procs []*fakeProc
}

func (r *recorder) run(name string, args []string) publisher.Process {
	r.mu.Lock()
	defer r.mu.Unlock()
	p := &fakeProc{done: make(chan struct{})}
	r.runs = append(r.runs, args)
	r.procs = append(r.procs, p)
	return p
}

func (r *recorder) count() int { r.mu.Lock(); defer r.mu.Unlock(); return len(r.runs) }

func testAgent(rec *recorder) *Agent {
	a := &Agent{o: Options{Config: &Config{}, Logf: func(string, ...any) {}}, streams: map[string]*stream{}}
	a.d = &deps{
		ffmpeg:    "ffmpeg",
		relayPort: func() int { return 40000 },
		tokens: func(_ context.Context, id int) (*Token, error) {
			return &Token{CameraID: id, StreamName: "cam-" + itoaT(id), SubStreamName: "cam-" + itoaT(id) + "-sub", Token: "T"}, nil
		},
		logf:      func(string, ...any) {},
		run:       rec.run,
		backoff:   []time.Duration{10 * time.Millisecond},
		liveAfter: 20 * time.Millisecond,
		healthy:   time.Hour,
	}
	return a
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	for i := 0; i < 200; i++ {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met")
}

func TestApplyStartsStopsAndRestartsStreamsToMatchTheServer(t *testing.T) {
	rec := &recorder{}
	a := testAgent(rec)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer a.stopAll()

	a.apply(ctx, &Answer{Desired: []Desired{
		{CameraID: 7, StreamName: "cam-7", SubStreamName: "cam-7-sub", MainURL: "rtsp://a/main", SubURL: "rtsp://a/sub"},
		{CameraID: 8, StreamName: "cam-8", MainURL: "rtsp://b/main"},
	}})
	waitFor(t, func() bool { return rec.count() == 3 })
	waitFor(t, func() bool {
		for _, s := range a.report().Publishers {
			if s.State != "live" {
				return false
			}
		}
		return true
	})
	rep := a.report().Publishers
	if len(rep) != 3 || rep[0].CameraID != 7 || rep[0].Stream != "main" || rep[1].Stream != "sub" {
		t.Fatalf("report %+v", rep)
	}

	// Camera 8 removed, camera 7's main URL changed: 8 stops, 7/main restarts,
	// 7/sub keeps running.
	a.apply(ctx, &Answer{Desired: []Desired{
		{CameraID: 7, StreamName: "cam-7", SubStreamName: "cam-7-sub", MainURL: "rtsp://a/main2", SubURL: "rtsp://a/sub"},
	}})
	waitFor(t, func() bool { return rec.count() == 4 })
	rec.mu.Lock()
	last := strings.Join(rec.runs[3], " ")
	rec.mu.Unlock()
	if !strings.Contains(last, "rtsp://a/main2") || !strings.Contains(last, "rtsp://127.0.0.1:40000/cam-7?jwt=T") {
		t.Fatalf("restart args %q", last)
	}
	if n := len(a.report().Publishers); n != 2 {
		t.Fatalf("want 2 streams, got %d", n)
	}
}

func TestATokenFailureIsReportedAndRetried(t *testing.T) {
	rec := &recorder{}
	a := testAgent(rec)
	calls := 0
	var mu sync.Mutex
	a.d.tokens = func(context.Context, int) (*Token, error) {
		mu.Lock()
		defer mu.Unlock()
		calls++
		if calls < 3 {
			return nil, ErrRejected
		}
		return &Token{StreamName: "cam-9", Token: "T"}, nil
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer a.stopAll()
	a.apply(ctx, &Answer{Desired: []Desired{{CameraID: 9, StreamName: "cam-9", MainURL: "rtsp://c/main"}}})
	waitFor(t, func() bool { return rec.count() == 1 })
	mu.Lock()
	defer mu.Unlock()
	if calls != 3 {
		t.Fatalf("token asked %d times", calls)
	}
}

// ── client ─────────────────────────────────────────────────────────────

func TestClientSendsTheCredentialAndMaps404ToRejected(t *testing.T) {
	var gotAuth, gotEtag string
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		switch r.URL.Path {
		case "/api/v1/site-agent/wait":
			gotEtag = r.URL.Query().Get("etag")
			_ = json.NewEncoder(w).Encode(Answer{ETag: "e2", Ingest: Ingest{Host: "nvr.example.com", Port: 443, ALPN: IngestALPN}})
		case "/api/v1/site-agent/sync":
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	c := NewClient(srv.URL, "site_1_secret", "", true)
	ans, err := c.Wait(context.Background(), "e1")
	if err != nil || ans.ETag != "e2" || ans.Ingest.ALPN != IngestALPN {
		t.Fatalf("wait: %+v %v", ans, err)
	}
	if gotAuth != "Bearer site_1_secret" || gotEtag != "e1" {
		t.Fatalf("auth %q etag %q", gotAuth, gotEtag)
	}
	if _, err := c.Sync(context.Background(), Report{}); !errors.Is(err, ErrRejected) {
		t.Fatalf("want ErrRejected, got %v", err)
	}
}

// ── config ─────────────────────────────────────────────────────────────

func TestConfigIsSavedPrivatelyAndLoadedBack(t *testing.T) {
	p := filepath.Join(t.TempDir(), "sub", "config.json")
	c := &Config{Server: "https://nvr.example.com", SiteID: 3, Token: "site_3_x", Pin: "sha256/abc"}
	if err := c.Save(p); err != nil {
		t.Fatal(err)
	}
	if runtime.GOOS != "windows" {
		if st, _ := os.Stat(p); st.Mode().Perm() != 0o600 {
			t.Fatalf("mode %v", st.Mode().Perm())
		}
	}
	got, err := Load(p)
	if err != nil || *got != *c {
		t.Fatalf("loaded %+v %v", got, err)
	}
	if _, err := os.Stat(p + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("tmp file left behind")
	}
}

func TestNormaliseServer(t *testing.T) {
	for in, want := range map[string]string{
		"nvr.example.com":              "https://nvr.example.com",
		"https://nvr.example.com/x":    "https://nvr.example.com",
		"https://nvr.example.com:8443": "https://nvr.example.com:8443",
	} {
		if got, err := NormaliseServer(in); err != nil || got != want {
			t.Fatalf("%q: %q %v", in, got, err)
		}
	}
	if _, err := NormaliseServer("http://nvr.example.com"); err == nil {
		t.Fatal("plain http must be refused")
	}
}
