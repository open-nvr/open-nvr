// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/core"
)

const testKey = "ohk_test-key"

type fakeCore struct {
	mu      sync.Mutex
	reports []core.Report
	keys    []string
	build   string
}

func (f *fakeCore) handler(t *testing.T) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/v1/host-agent/sync" {
			http.NotFound(w, r)
			return
		}
		var rep core.Report
		if err := json.NewDecoder(r.Body).Decode(&rep); err != nil {
			t.Errorf("bad report: %v", err)
		}
		f.mu.Lock()
		f.reports = append(f.reports, rep)
		f.keys = append(f.keys, r.Header.Get("X-Host-Agent-Key"))
		build := f.build
		f.mu.Unlock()
		if r.Header.Get("X-Host-Agent-Key") != testKey {
			http.NotFound(w, r)
			return
		}
		_ = json.NewEncoder(w).Encode(core.SyncResponse{CoreBuild: build, PollAfterS: 2})
	})
}

func (f *fakeCore) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.reports)
}

func writeKey(t *testing.T, key string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "agent.key")
	if err := os.WriteFile(p, []byte(key+"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestRunSyncsWithTheKeyAndReportsTheHost(t *testing.T) {
	fc := &fakeCore{}
	srv := httptest.NewServer(fc.handler(t))
	defer srv.Close()

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan int, 1)
	go func() {
		done <- Run(ctx, Options{Core: srv.URL, KeyFile: writeKey(t, testKey),
			FFmpeg: filepath.Join(t.TempDir(), "no-ffmpeg"), Logf: t.Logf})
	}()
	deadline := time.Now().Add(10 * time.Second)
	for fc.count() < 1 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	cancel()
	if code := <-done; code != 0 {
		t.Fatalf("exit %d", code)
	}
	if fc.count() < 1 {
		t.Fatal("never synced")
	}
	fc.mu.Lock()
	defer fc.mu.Unlock()
	if fc.keys[0] != testKey {
		t.Fatalf("key header %q", fc.keys[0])
	}
	r := fc.reports[0]
	if r.OS == "" || r.Arch == "" || r.Version != buildinfo.Build || r.Role != "user" {
		t.Fatalf("report identity: %+v", r)
	}
	if r.Metrics == nil || r.Metrics.MemoryTotalBytes == 0 {
		t.Fatalf("no host metrics: %+v", r.Metrics)
	}
}

func TestReplacedNeedsANewBuildAndANewBinary(t *testing.T) {
	old := buildinfo.Build
	defer func() { buildinfo.Build = old }()
	buildinfo.Build = "0.1.10+aaa"

	exe := filepath.Join(t.TempDir(), "opennvr-host")
	os.WriteFile(exe, []byte("v1"), 0o700)
	started := statOf(exe)

	if replaced("0.1.10+aaa", exe, started) {
		t.Fatal("same build: keep running")
	}
	if replaced("0.1.11+bbb", exe, started) {
		t.Fatal("core moved on but no new agent installed: keep running, no restart loop")
	}
	os.WriteFile(exe, []byte("v2-longer"), 0o700)
	if !replaced("0.1.11+bbb", exe, started) {
		t.Fatal("new core and a new binary: exit so it starts")
	}
	buildinfo.Build = "dev"
	if replaced("0.1.11+bbb", exe, started) {
		t.Fatal("a dev build is never replaced")
	}
}

func TestRecordingsDirComesFromTheSiteEnv(t *testing.T) {
	repo := t.TempDir()
	if got := recordingsDir(repo); got != filepath.Join(repo, "recordings") {
		t.Fatalf("default: %q", got)
	}
	os.WriteFile(filepath.Join(repo, ".env"), []byte("FOO=1\nRECORDINGS_PATH=\"D:/nvr/rec\"\n"), 0o600)
	if got := recordingsDir(repo); got != filepath.Clean("D:/nvr/rec") && got != "D:/nvr/rec" {
		// On Linux "D:/nvr/rec" is relative; only check it was read.
		if got != filepath.Join(repo, "D:/nvr/rec") {
			t.Fatalf("from .env: %q", got)
		}
	}
}

func TestReadKeyRefusesAnythingElse(t *testing.T) {
	if _, err := readKey(writeKey(t, "not-a-key")); err == nil {
		t.Fatal("must refuse a file that is not an agent key")
	}
	if k, err := readKey(writeKey(t, testKey)); err != nil || k != testKey {
		t.Fatalf("%q %v", k, err)
	}
}
