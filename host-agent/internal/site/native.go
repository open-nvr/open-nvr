// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/tls"
	"errors"
	"fmt"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/bluenviron/gortsplib/v5"
	"github.com/bluenviron/gortsplib/v5/pkg/base"
	"github.com/bluenviron/gortsplib/v5/pkg/description"
	"github.com/bluenviron/gortsplib/v5/pkg/format"
	"github.com/pion/rtp"

	"github.com/open-nvr/open-nvr/host-agent/internal/publisher"
)

// The native relay copies a camera stream without ffmpeg: RTSP over TCP from
// the camera, the RTP packets of its first video track passed on exactly as
// they arrive (nothing is decoded, so any codec the camera sends works), and
// published as RTSP into the agent's loopback relay, which adds TLS, the
// ALPN and the server's pinned key as it does for ffmpeg. Like ffmpeg here,
// it sends video only (`-map 0:v:0 -an`): audio would change what is
// uploaded and recorded.
//
// Nothing goes on a command line: the camera's credentials and the publish
// token stay in this process's memory.

// nativeStallAfter: a camera that sends nothing this long is reconnected
// (ffmpeg's -timeout does the same). A variable for tests.
var nativeStallAfter = 10 * time.Second

const (
	nativeIOTimeout = 10 * time.Second
	nativeUserAgent = "OpenNVR site agent"
)

// errNoVideo: the camera's stream has no video track to relay.
var errNoVideo = errors.New("the camera's stream has no video track")

// nativeProc is one native relay run; it satisfies publisher.Process so the
// stream supervisor treats it like an ffmpeg process.
type nativeProc struct {
	cancel context.CancelFunc
	done   chan struct{}

	mu  sync.Mutex
	err error
}

func (p *nativeProc) Wait() error {
	<-p.done
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.err
}

func (p *nativeProc) Kill() { p.cancel() }

func (p *nativeProc) LastLines() []string {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.err == nil {
		return nil
	}
	return []string{p.err.Error()}
}

// startNative relays lanURL into rtsp://127.0.0.1:<relayPort>/<path>?jwt=<token>.
// dial, when set, makes the connection to the camera (the agent's target
// policy checks the address it reached).
func startNative(lanURL string, relayPort int, path, token string,
	dial func(ctx context.Context, network, addr string) (net.Conn, error)) publisher.Process {
	ctx, cancel := context.WithCancel(context.Background())
	p := &nativeProc{cancel: cancel, done: make(chan struct{})}
	go func() {
		defer close(p.done)
		err := runNative(ctx, lanURL, fmt.Sprintf("rtsp://127.0.0.1:%d/%s?jwt=%s", relayPort, path, token), dial)
		if ctx.Err() != nil {
			err = errors.New("stopped")
		}
		p.mu.Lock()
		p.err = err
		p.mu.Unlock()
	}()
	return p
}

func runNative(ctx context.Context, lanURL, target string,
	dial func(ctx context.Context, network, addr string) (net.Conn, error)) error {
	u, err := base.ParseURL(lanURL)
	if err != nil {
		return fmt.Errorf("camera URL: %w", err)
	}
	tcp := gortsplib.ProtocolTCP
	reader := &gortsplib.Client{
		Scheme:       u.Scheme,
		Host:         u.Host,
		Protocol:     &tcp,
		ReadTimeout:  nativeIOTimeout,
		WriteTimeout: nativeIOTimeout,
		UserAgent:    nativeUserAgent,
		// Cameras on a LAN present self-signed certificates for rtsps;
		// core does not verify them either.
		TLSConfig:   &tls.Config{InsecureSkipVerify: true}, //nolint:gosec
		DialContext: dial,
	}
	if err := reader.Start(); err != nil {
		return fmt.Errorf("camera: %w", err)
	}
	defer reader.Close()
	stop := context.AfterFunc(ctx, reader.Close)
	defer stop()

	desc, _, err := reader.Describe(u)
	if err != nil {
		return fmt.Errorf("camera: %w", err)
	}
	var video *description.Media
	for _, m := range desc.Medias {
		if m.Type == description.MediaTypeVideo {
			video = m
			break
		}
	}
	if video == nil {
		return errNoVideo
	}
	medias := []*description.Media{video}
	// Before StartRecording: setting up rewrites the medias' control attribute.
	if err := reader.SetupAll(desc.BaseURL, medias); err != nil {
		return fmt.Errorf("camera: %w", err)
	}

	pub := &gortsplib.Client{
		Protocol:     &tcp,
		ReadTimeout:  nativeIOTimeout,
		WriteTimeout: nativeIOTimeout,
		UserAgent:    nativeUserAgent,
	}
	if err := pub.StartRecording(target, &description.Session{Medias: medias}); err != nil {
		return fmt.Errorf("server: %s", publisher.Redact(err.Error()))
	}
	defer pub.Close()
	stopPub := context.AfterFunc(ctx, pub.Close)
	defer stopPub()

	var last atomic.Int64
	last.Store(time.Now().UnixNano())
	writeErr := make(chan error, 1)
	reader.OnPacketRTPAny(func(m *description.Media, _ format.Format, pkt *rtp.Packet) {
		if m != video {
			return
		}
		last.Store(time.Now().UnixNano())
		if err := pub.WritePacketRTP(m, pkt); err != nil {
			select {
			case writeErr <- err:
			default:
			}
		}
	})
	if _, err := reader.Play(nil); err != nil {
		return fmt.Errorf("camera: %w", err)
	}

	readerDone := make(chan error, 1)
	go func() { readerDone <- reader.Wait() }()
	pubDone := make(chan error, 1)
	go func() { pubDone <- pub.Wait() }()
	tick := time.NewTicker(time.Second)
	defer tick.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil
		case err := <-readerDone:
			return fmt.Errorf("camera: %w", err)
		case err := <-pubDone:
			return fmt.Errorf("server: %s", publisher.Redact(err.Error()))
		case err := <-writeErr:
			return fmt.Errorf("server: %s", publisher.Redact(err.Error()))
		case <-tick.C:
			if time.Since(time.Unix(0, last.Load())) > nativeStallAfter {
				return fmt.Errorf("the camera sent nothing for %s", nativeStallAfter)
			}
		}
	}
}
