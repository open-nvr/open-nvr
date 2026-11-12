// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package publisher

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/core"
	"github.com/open-nvr/open-nvr/host-agent/internal/webcams"
)

type fakeTokens struct {
	mu    sync.Mutex
	calls int
	err   error
}

func (f *fakeTokens) PublishToken(_ context.Context, id int) (string, string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls++
	if f.err != nil {
		return "", "", f.err
	}
	return "cam-" + string(rune('0'+id)), "TOKEN", nil
}

// fakeProc runs until killed or told to exit.
type fakeProc struct {
	args   []string
	exit   chan error
	killed chan struct{}
	once   sync.Once
}

func (p *fakeProc) Wait() error {
	select {
	case err := <-p.exit:
		return err
	case <-p.killed:
		return errors.New("killed")
	}
}
func (p *fakeProc) Kill() { p.once.Do(func() { close(p.killed) }) }
func (p *fakeProc) LastLines() []string {
	return []string{"Connection refused rtsps://x/cam-1?jwt=SECRET"}
}

type runner struct {
	mu    sync.Mutex
	procs []*fakeProc
}

func (r *runner) run(_ context.Context, _ string, args []string) Process {
	p := &fakeProc{args: args, exit: make(chan error, 1), killed: make(chan struct{})}
	r.mu.Lock()
	r.procs = append(r.procs, p)
	r.mu.Unlock()
	return p
}

func (r *runner) count() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.procs)
}

func (r *runner) last() *fakeProc {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.procs[len(r.procs)-1]
}

func newTestManager(r *runner, tok *fakeTokens) *Manager {
	return NewManager(Config{
		FFmpeg:    "ffmpeg",
		RTSPS:     "rtsps://127.0.0.1:8322",
		Run:       r.run,
		Backoff:   []time.Duration{10 * time.Millisecond},
		LiveAfter: 20 * time.Millisecond,
		Healthy:   time.Hour,
	}, tok)
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func stateOf(m *Manager, id int) string {
	for _, s := range m.States() {
		if s.CameraID == id {
			return s.State
		}
	}
	return ""
}

var hp = webcams.Device{ID: "dshow:hp", Name: "HP HD Camera", Input: []string{"-f", "dshow", "-i", "video=hp"}}
var usb = webcams.Device{ID: "dshow:usb", Name: "USB Camera", Input: []string{"-f", "dshow", "-i", "video=usb"}}

func TestArgsBuildTheStreamWeRecord(t *testing.T) {
	a := strings.Join(Args(hp, core.Desired{CameraID: 6, FPS: 15, Height: 720}, "rtsps://127.0.0.1:8322/", "cam-6", "T"), " ")
	for _, want := range []string{
		"-f dshow -i video=hp", "-an", "fps=15,scale=-2:'min(720,ih)'", "-c:v libx264",
		"-bf 0", "-g 30", "-f rtsp -rtsp_transport tcp", "rtsps://127.0.0.1:8322/cam-6?jwt=T",
		// the sub stream for detection, from the same capture, same token
		"split=2[m][s];[s]scale=640:-2,fps=10[sub]", "-map [sub]", "-g 20", "rtsps://127.0.0.1:8322/cam-6-sub?jwt=T",
	} {
		if !strings.Contains(a, want) {
			t.Errorf("args missing %q:\n%s", want, a)
		}
	}
}

func TestRedactHidesTokens(t *testing.T) {
	got := Redact("failed: rtsps://h/cam-1?jwt=abc.def.ghi: refused")
	if strings.Contains(got, "abc") || !strings.Contains(got, "jwt=***") {
		t.Fatal(got)
	}
}

func TestReconcileStartsStopsAndSwitches(t *testing.T) {
	r, tok := &runner{}, &fakeTokens{}
	m := newTestManager(r, tok)
	defer m.StopAll()
	present := []webcams.Device{hp, usb}

	m.Reconcile([]core.Desired{{CameraID: 1, DeviceID: hp.ID, FPS: 15, Height: 720}}, present)
	eventually(t, "live", func() bool { return stateOf(m, 1) == "live" })
	if !strings.Contains(strings.Join(r.last().args, " "), "video=hp") {
		t.Fatal("wrong device")
	}
	if len(m.InUse()) != 1 {
		t.Fatal("the streamed webcam must count as present")
	}

	// Same desired state again: nothing restarts.
	m.Reconcile([]core.Desired{{CameraID: 1, DeviceID: hp.ID, DeviceName: "renamed", FPS: 15, Height: 720}}, present)
	time.Sleep(50 * time.Millisecond)
	if r.count() != 1 {
		t.Fatalf("restarted on a name-only change: %d runs", r.count())
	}

	// Another webcam: the old ffmpeg is killed, a new one starts.
	first := r.last()
	m.Reconcile([]core.Desired{{CameraID: 1, DeviceID: usb.ID, FPS: 15, Height: 720}}, present)
	eventually(t, "switch", func() bool { return r.count() == 2 })
	select {
	case <-first.killed:
	default:
		t.Fatal("old ffmpeg still running")
	}

	// Not wanted any more: stopped.
	m.Reconcile(nil, present)
	if m.Running() != 0 {
		t.Fatal("supervisor left running")
	}
	select {
	case <-r.last().killed:
	default:
		t.Fatal("ffmpeg left running")
	}
}

func TestMissingWebcamWaitsAndResumesWhenPlugged(t *testing.T) {
	r, tok := &runner{}, &fakeTokens{}
	m := newTestManager(r, tok)
	defer m.StopAll()
	want := []core.Desired{{CameraID: 2, DeviceID: usb.ID, DeviceName: usb.Name, FPS: 15, Height: 720}}
	m.Reconcile(want, []webcams.Device{hp})
	eventually(t, "device_missing", func() bool { return stateOf(m, 2) == "device_missing" })
	if r.count() != 0 || tok.calls != 0 {
		t.Fatal("must not ask for a token or start ffmpeg without the webcam")
	}
}

func TestFFmpegExitIsReportedRedactedAndRetried(t *testing.T) {
	r, tok := &runner{}, &fakeTokens{}
	m := newTestManager(r, tok)
	defer m.StopAll()
	m.Reconcile([]core.Desired{{CameraID: 3, DeviceID: hp.ID, FPS: 15, Height: 720}}, []webcams.Device{hp})
	eventually(t, "first run", func() bool { return r.count() == 1 })
	r.last().exit <- errors.New("exit status 1")
	eventually(t, "retry", func() bool { return r.count() == 2 })
	for _, s := range m.States() {
		if strings.Contains(s.Detail, "SECRET") {
			t.Fatalf("token leaked into the report: %q", s.Detail)
		}
	}
}

func TestNoFFmpegIsAnErrorNotACrash(t *testing.T) {
	m := NewManager(Config{FFmpeg: ""}, &fakeTokens{})
	defer m.StopAll()
	m.Reconcile([]core.Desired{{CameraID: 4, DeviceID: hp.ID}}, []webcams.Device{hp})
	eventually(t, "error", func() bool { return stateOf(m, 4) == "error" })
}
