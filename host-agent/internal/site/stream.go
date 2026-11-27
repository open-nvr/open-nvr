// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"fmt"
	"sync"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/publisher"
)

// stream relays one camera stream (main or sub) for as long as it is wanted:
// ask for a token, run ffmpeg from the camera into the local relay, and when
// ffmpeg stops, wait (backoff) and do it again with a fresh token. A camera
// that is down, a server that is unreachable or a revoked site all end the
// same way: an error state and a slower retry.
type stream struct {
	key      string // "<camera>/<main|sub>"
	cameraID int
	kind     string // main | sub
	lanURL   string // with credentials
	path     string // MediaMTX path

	deps *deps

	mu     sync.Mutex
	st     PublisherState
	ctx    context.Context
	cancel context.CancelFunc
	done   chan struct{}
}

// deps is what every stream shares.
type deps struct {
	ffmpeg    string
	relayPort func() int // 0 while there is no relay
	tokens    func(ctx context.Context, cameraID int) (*Token, error)
	logf      func(string, ...any)
	run       func(name string, args []string) publisher.Process // replaced in tests
	backoff   []time.Duration
	liveAfter time.Duration
	healthy   time.Duration
}

func newStream(parent context.Context, key string, cameraID int, kind, lanURL, path string, d *deps) *stream {
	ctx, cancel := context.WithCancel(parent)
	s := &stream{key: key, cameraID: cameraID, kind: kind, lanURL: lanURL, path: path,
		deps: d, ctx: ctx, cancel: cancel, done: make(chan struct{})}
	s.set("starting", "")
	go s.run()
	return s
}

func nowF() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func (s *stream) set(state, detail string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.st.State != state || s.st.Detail != detail {
		s.st = PublisherState{CameraID: s.cameraID, Stream: s.kind, State: state, Detail: detail, Since: nowF()}
	}
}

func (s *stream) state() PublisherState {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.st
}

func (s *stream) stop() {
	s.cancel()
	<-s.done
}

func (s *stream) sleep(d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-s.ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// Args is the ffmpeg command line: the camera's RTSP over TCP, its first
// video stream copied as it is (no decode, no encode: a site box can be a
// small one), out as RTSP to the local relay with the publish token.
func Args(lanURL string, relayPort int, path, token string) []string {
	return []string{
		"-hide_banner", "-loglevel", "warning", "-nostdin",
		"-rtsp_transport", "tcp",
		// A camera that stops sending ends ffmpeg in 10 s (microseconds),
		// so the stream is retried instead of hanging on a dead socket.
		"-timeout", "10000000",
		"-i", lanURL,
		"-map", "0:v:0", "-c:v", "copy", "-an",
		"-f", "rtsp", "-rtsp_transport", "tcp",
		fmt.Sprintf("rtsp://127.0.0.1:%d/%s?jwt=%s", relayPort, path, token),
	}
}

func (s *stream) run() {
	defer close(s.done)
	d := s.deps
	attempt := 0
	backoff := func() time.Duration {
		b := d.backoff[min(attempt, len(d.backoff)-1)]
		attempt++
		return b
	}
	for s.ctx.Err() == nil {
		if d.ffmpeg == "" {
			s.set("error", "ffmpeg is not installed on this machine")
			if !s.sleep(30 * time.Second) {
				return
			}
			continue
		}
		port := d.relayPort()
		if port == 0 {
			s.set("waiting", "no connection to the server yet")
			if !s.sleep(2 * time.Second) {
				return
			}
			continue
		}
		s.set("starting", "")
		tok, err := d.tokens(s.ctx, s.cameraID)
		if err != nil {
			if s.ctx.Err() != nil {
				return
			}
			s.set("error", "publish token: "+err.Error())
			if !s.sleep(backoff()) {
				return
			}
			continue
		}
		path := s.path
		if s.kind == "main" && tok.StreamName != "" {
			path = tok.StreamName
		} else if s.kind == "sub" && tok.SubStreamName != "" {
			path = tok.SubStreamName
		}
		started := time.Now()
		p := d.run(d.ffmpeg, Args(s.lanURL, port, path, tok.Token))
		exited := make(chan error, 1)
		go func() { exited <- p.Wait() }()
		live := time.NewTimer(d.liveAfter)
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
				return
			}
		}
		live.Stop()
		if time.Since(started) >= d.healthy {
			attempt = 0
		}
		detail := "ffmpeg stopped"
		if lines := p.LastLines(); len(lines) > 0 {
			detail = lines[len(lines)-1]
		} else if werr != nil {
			detail = werr.Error()
		}
		s.set("error", publisher.Redact(redactURL(detail)))
		d.logf("camera %d %s: %s", s.cameraID, s.kind, publisher.Redact(redactURL(detail)))
		if !s.sleep(backoff()) {
			return
		}
	}
}
