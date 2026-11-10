// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package publisher streams webcams into OpenNVR: one supervisor per camera,
// each running ffmpeg (capture, H.264, RTSPS push to MediaMTX on localhost)
// and restarting it with backoff. The Manager turns core's desired state into
// running supervisors and back into the states the agent reports.
package publisher

import (
	"context"
	"fmt"
	"os/exec"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/core"
	"github.com/open-nvr/open-nvr/host-agent/internal/proc"
	"github.com/open-nvr/open-nvr/host-agent/internal/webcams"
)

// TokenSource hands out a publish token for a camera (core.Client does).
type TokenSource interface {
	PublishToken(ctx context.Context, cameraID int) (path, token string, err error)
}

// Config is what every supervisor needs.
type Config struct {
	FFmpeg string // path to ffmpeg; "" = cannot stream
	RTSPS  string // MediaMTX's RTSPS listener on this host, e.g. rtsps://127.0.0.1:8322
	Logf   func(format string, args ...any)
	// Backoff between attempts; the last value repeats.
	Backoff []time.Duration
	// LiveAfter: ffmpeg still running this long = the stream is up.
	LiveAfter time.Duration
	// Healthy: a run this long resets the backoff.
	Healthy time.Duration
	// Run starts the process; replaced in tests.
	Run func(ctx context.Context, name string, args []string) Process
}

// Process is a started ffmpeg, as the supervisor needs it.
type Process interface {
	Wait() error
	Kill()
	LastLines() []string
}

func (c *Config) defaults() {
	if c.Logf == nil {
		c.Logf = func(string, ...any) {}
	}
	if len(c.Backoff) == 0 {
		c.Backoff = []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second, 60 * time.Second}
	}
	if c.LiveAfter == 0 {
		c.LiveAfter = 5 * time.Second
	}
	if c.Healthy == 0 {
		c.Healthy = 60 * time.Second
	}
	if c.Run == nil {
		c.Run = runFFmpeg
	}
}

// Manager owns the supervisors.
type Manager struct {
	cfg    Config
	tokens TokenSource

	mu      sync.Mutex
	sups    map[int]*supervisor
	present []webcams.Device
}

// NewManager makes a manager; nothing runs until Reconcile.
func NewManager(cfg Config, tokens TokenSource) *Manager {
	cfg.defaults()
	return &Manager{cfg: cfg, tokens: tokens, sups: map[int]*supervisor{}}
}

// Reconcile makes the running supervisors match the desired state: starts the
// new ones, stops the ones no longer wanted, restarts the ones whose webcam or
// settings changed. present is the latest enumeration.
func (m *Manager) Reconcile(desired []core.Desired, present []webcams.Device) {
	want := map[int]core.Desired{}
	for _, d := range desired {
		want[d.CameraID] = d
	}
	m.mu.Lock()
	m.present = present
	var stopping []*supervisor
	for id, s := range m.sups {
		if d, ok := want[id]; !ok || !sameStream(d, s.want) {
			stopping = append(stopping, s)
			delete(m.sups, id)
		}
	}
	for id, d := range want {
		if _, ok := m.sups[id]; ok {
			continue
		}
		s := newSupervisor(d, m)
		m.sups[id] = s
		go s.run()
	}
	m.mu.Unlock()
	// Outside the lock: a stopping supervisor may be waiting for it.
	for _, s := range stopping {
		s.stop()
	}
}

// sameStream: nothing that changes the ffmpeg command line changed. The
// device's display name does not count; it only helps find a moved webcam.
func sameStream(a, b core.Desired) bool {
	return a.CameraID == b.CameraID && a.DeviceID == b.DeviceID &&
		a.Width == b.Width && a.Height == b.Height && a.FPS == b.FPS
}

func (m *Manager) presentDevices() []webcams.Device {
	m.mu.Lock()
	defer m.mu.Unlock()
	return append([]webcams.Device(nil), m.present...)
}

// InUse is the devices being streamed right now. Some capture stacks stop
// listing a device while it is open; the agent still reports these as present.
func (m *Manager) InUse() []webcams.Device {
	m.mu.Lock()
	defer m.mu.Unlock()
	var out []webcams.Device
	for _, s := range m.sups {
		if d, ok := s.streaming(); ok {
			out = append(out, d)
		}
	}
	return out
}

// States is how each stream is doing, by camera id.
func (m *Manager) States() []core.PublisherState {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := make([]core.PublisherState, 0, len(m.sups))
	for _, s := range m.sups {
		out = append(out, s.state())
	}
	sort.Slice(out, func(i, j int) bool { return out[i].CameraID < out[j].CameraID })
	return out
}

// StopAll stops every stream (the agent is exiting).
func (m *Manager) StopAll() {
	m.mu.Lock()
	sups := m.sups
	m.sups = map[int]*supervisor{}
	m.mu.Unlock()
	for _, s := range sups {
		s.stop()
	}
}

// Running is how many supervisors exist (tests).
func (m *Manager) Running() int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return len(m.sups)
}

// ── One camera ─────────────────────────────────────────────────────────

type supervisor struct {
	want   core.Desired
	m      *Manager
	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{}

	mu      sync.Mutex
	st      core.PublisherState
	dev     webcams.Device
	running bool
}

func newSupervisor(d core.Desired, m *Manager) *supervisor {
	ctx, cancel := context.WithCancel(context.Background())
	return &supervisor{
		want: d, m: m, ctx: ctx, cancel: cancel, done: make(chan struct{}),
		st: core.PublisherState{CameraID: d.CameraID, State: "waiting", Since: now()},
	}
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func (s *supervisor) set(state, detail string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.st.State != state || s.st.Detail != detail {
		s.st = core.PublisherState{CameraID: s.want.CameraID, State: state, Detail: detail, Since: now()}
		s.m.cfg.Logf("camera %d: %s %s", s.want.CameraID, state, detail)
	}
}

func (s *supervisor) state() core.PublisherState {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.st
}

func (s *supervisor) streaming() (webcams.Device, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.dev, s.running
}

func (s *supervisor) stop() {
	s.cancel()
	<-s.done
}

// sleep waits d, or less if the supervisor is stopped; false when stopped.
func (s *supervisor) sleep(d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-s.ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func (s *supervisor) run() {
	defer close(s.done)
	cfg := s.m.cfg
	attempt := 0
	backoff := func() time.Duration {
		d := cfg.Backoff[min(attempt, len(cfg.Backoff)-1)]
		attempt++
		return d
	}
	for s.ctx.Err() == nil {
		if cfg.FFmpeg == "" {
			s.set("error", "ffmpeg is not installed on this machine")
			if !s.sleep(30 * time.Second) {
				return
			}
			continue
		}
		dev, ok := webcams.Resolve(s.want.DeviceID, s.want.DeviceName, s.m.presentDevices())
		if !ok {
			s.set("device_missing", "the webcam is not connected")
			if !s.sleep(10 * time.Second) {
				return
			}
			continue
		}
		s.set("starting", "")
		path, token, err := s.m.tokens.PublishToken(s.ctx, s.want.CameraID)
		if err != nil {
			if s.ctx.Err() != nil {
				return
			}
			s.set("error", err.Error())
			if !s.sleep(backoff()) {
				return
			}
			continue
		}
		args := Args(dev, s.want, cfg.RTSPS, path, token)
		started := time.Now()
		p := cfg.Run(s.ctx, cfg.FFmpeg, args)
		s.mu.Lock()
		s.dev, s.running = dev, true
		s.mu.Unlock()

		exited := make(chan error, 1)
		go func() { exited <- p.Wait() }()
		live := time.NewTimer(cfg.LiveAfter)
		var werr error
	wait:
		for {
			select {
			case <-live.C:
				s.set("live", "")
			case werr = <-exited:
				break wait
			case <-s.ctx.Done():
				p.Kill()
				<-exited
				live.Stop()
				s.mu.Lock()
				s.running = false
				s.mu.Unlock()
				return
			}
		}
		live.Stop()
		s.mu.Lock()
		s.running = false
		s.mu.Unlock()
		if time.Since(started) >= cfg.Healthy {
			attempt = 0
		}
		detail := "ffmpeg stopped"
		if lines := p.LastLines(); len(lines) > 0 {
			detail = lines[len(lines)-1]
		} else if werr != nil {
			detail = werr.Error()
		}
		s.set("error", Redact(detail))
		if !s.sleep(backoff()) {
			return
		}
	}
}

// Args is the ffmpeg command line: capture → core's fps (25 by default), at
// most 720 lines → H.264 tuned for live (no B-frames, a keyframe every 2 s)
// → RTSP over TLS to MediaMTX with the publish token.
//
// ultrafast, not veryfast: on an NVR the host CPU is often busy with
// detection, and an encoder that falls behind real time shows up in Live
// View as lag (frames queue in the capture buffer). ultrafast costs about
// half the CPU; the higher bitrate pays back its weaker compression.
func Args(dev webcams.Device, d core.Desired, rtsps, path, token string) []string {
	fps := d.FPS
	if fps <= 0 {
		fps = 25
	}
	height := d.Height
	if height <= 0 {
		height = 720
	}
	gop := fmt.Sprint(2 * fps)
	args := []string{"-hide_banner", "-loglevel", "warning", "-nostdin"}
	args = append(args, dev.Input...)
	args = append(args,
		"-an",
		"-vf", fmt.Sprintf("fps=%d,scale=-2:'min(%d,ih)'", fps, height),
		"-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
		"-pix_fmt", "yuv420p", "-bf", "0", "-g", gop, "-keyint_min", gop,
		"-b:v", "2500k", "-maxrate", "3000k", "-bufsize", "3000k",
		"-f", "rtsp", "-rtsp_transport", "tcp",
		strings.TrimRight(rtsps, "/")+"/"+path+"?jwt="+token,
	)
	return args
}

var jwtRe = regexp.MustCompile(`jwt=[^&\s"']+`)

// Redact removes publish tokens from anything that is logged or reported:
// ffmpeg echoes the URL it failed to open.
func Redact(s string) string { return jwtRe.ReplaceAllString(s, "jwt=***") }

// ── The real process ───────────────────────────────────────────────────

type ffmpegProcess struct {
	cmd  *exec.Cmd
	tail *tailBuffer
	err  error
}

func runFFmpeg(_ context.Context, name string, args []string) Process {
	// Not CommandContext: the supervisor stops ffmpeg itself, by its PID.
	cmd := exec.Command(name, args...)
	proc.Prepare(cmd)
	tail := &tailBuffer{max: 5}
	cmd.Stderr = tail
	cmd.Stdout = tail
	p := &ffmpegProcess{cmd: cmd, tail: tail}
	if err := cmd.Start(); err != nil {
		p.err = err
		return p
	}
	proc.Adopt(cmd.Process)
	return p
}

func (p *ffmpegProcess) Wait() error {
	if p.err != nil {
		return p.err
	}
	return p.cmd.Wait()
}

func (p *ffmpegProcess) Kill() {
	if p.cmd.Process != nil {
		_ = p.cmd.Process.Kill()
	}
}

func (p *ffmpegProcess) LastLines() []string {
	if p.err != nil {
		return []string{p.err.Error()}
	}
	return p.tail.lines()
}

// tailBuffer keeps ffmpeg's last few output lines for the error report.
type tailBuffer struct {
	mu   sync.Mutex
	max  int
	buf  []string
	part string
}

func (t *tailBuffer) Write(b []byte) (int, error) {
	t.mu.Lock()
	defer t.mu.Unlock()
	text := t.part + strings.ReplaceAll(string(b), "\r", "\n")
	parts := strings.Split(text, "\n")
	t.part = parts[len(parts)-1]
	for _, l := range parts[:len(parts)-1] {
		if l = strings.TrimSpace(l); l != "" {
			t.buf = append(t.buf, Redact(l))
			if len(t.buf) > t.max {
				t.buf = t.buf[len(t.buf)-t.max:]
			}
		}
	}
	return len(b), nil
}

func (t *tailBuffer) lines() []string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return append([]string(nil), t.buf...)
}
