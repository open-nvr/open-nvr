// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/bluenviron/gortsplib/v5"
	"github.com/bluenviron/gortsplib/v5/pkg/base"
	"github.com/bluenviron/gortsplib/v5/pkg/description"
	"github.com/bluenviron/gortsplib/v5/pkg/format"
	"github.com/pion/rtp"

	"github.com/open-nvr/open-nvr/host-agent/internal/publisher"
)

func freeAddr(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	addr := ln.Addr().String()
	ln.Close()
	return addr
}

// fakeCamera serves one H.264 video track and one G.711 audio track, and
// sends packets on both while sending is true.
type fakeCamera struct {
	server  *gortsplib.Server
	stream  *gortsplib.ServerStream
	video   *description.Media
	audio   *description.Media
	sending atomic.Bool
	addr    string
}

func (c *fakeCamera) OnDescribe(*gortsplib.ServerHandlerOnDescribeCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, c.stream, nil
}

func (c *fakeCamera) OnSetup(*gortsplib.ServerHandlerOnSetupCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, c.stream, nil
}

func (c *fakeCamera) OnPlay(*gortsplib.ServerHandlerOnPlayCtx) (*base.Response, error) {
	return &base.Response{StatusCode: base.StatusOK}, nil
}

func startFakeCamera(t *testing.T) *fakeCamera {
	t.Helper()
	c := &fakeCamera{addr: freeAddr(t)}
	c.video = &description.Media{Type: description.MediaTypeVideo, Formats: []format.Format{&format.H264{
		PayloadTyp: 96, PacketizationMode: 1,
		SPS: []byte{0x67, 0x42, 0xc0, 0x28, 0xd9, 0x00, 0x78, 0x02, 0x27, 0xe5, 0x84, 0x00, 0x00, 0x03, 0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0xf0, 0x3c, 0x60, 0xc9, 0x20},
		PPS: []byte{0x68, 0xcb, 0x8c, 0xb2},
	}}}
	c.audio = &description.Media{Type: description.MediaTypeAudio, Formats: []format.Format{&format.G711{
		PayloadTyp: 0, MULaw: true, SampleRate: 8000, ChannelCount: 1,
	}}}
	c.server = &gortsplib.Server{Handler: c, RTSPAddress: c.addr}
	if err := c.server.Start(); err != nil {
		t.Fatal(err)
	}
	c.stream = &gortsplib.ServerStream{Server: c.server, Desc: &description.Session{Medias: []*description.Media{c.video, c.audio}}}
	if err := c.stream.Initialize(); err != nil {
		t.Fatal(err)
	}
	c.sending.Store(true)
	stop := make(chan struct{})
	go func() {
		seq := uint16(0)
		for {
			select {
			case <-stop:
				return
			case <-time.After(20 * time.Millisecond):
			}
			if !c.sending.Load() {
				continue
			}
			seq++
			_ = c.stream.WritePacketRTP(c.video, &rtp.Packet{Header: rtp.Header{Version: 2, PayloadType: 96, SequenceNumber: seq,
				Timestamp: uint32(seq) * 1800, SSRC: 1, Marker: true}, Payload: []byte{0x65, 0x88, 0x84, 0x00}})
			_ = c.stream.WritePacketRTP(c.audio, &rtp.Packet{Header: rtp.Header{Version: 2, PayloadType: 0, SequenceNumber: seq,
				Timestamp: uint32(seq) * 160, SSRC: 2}, Payload: make([]byte, 160)})
		}
	}()
	t.Cleanup(func() {
		close(stop)
		c.stream.Close()
		c.server.Close()
	})
	return c
}

// fakeIngest takes one publisher (like MediaMTX behind the relay) and counts
// what it receives.
type fakeIngest struct {
	server *gortsplib.Server
	addr   string

	mu      sync.Mutex
	path    string
	query   string
	medias  []*description.Media
	packets atomic.Int64
}

func (g *fakeIngest) OnAnnounce(ctx *gortsplib.ServerHandlerOnAnnounceCtx) (*base.Response, error) {
	g.mu.Lock()
	g.path, g.query, g.medias = ctx.Path, ctx.Query, ctx.Description.Medias
	g.mu.Unlock()
	return &base.Response{StatusCode: base.StatusOK}, nil
}

func (g *fakeIngest) OnSetup(*gortsplib.ServerHandlerOnSetupCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, nil, nil
}

func (g *fakeIngest) OnRecord(ctx *gortsplib.ServerHandlerOnRecordCtx) (*base.Response, error) {
	ctx.Session.OnPacketRTPAny(func(*description.Media, format.Format, *rtp.Packet) { g.packets.Add(1) })
	return &base.Response{StatusCode: base.StatusOK}, nil
}

func startFakeIngest(t *testing.T) *fakeIngest {
	t.Helper()
	g := &fakeIngest{addr: freeAddr(t)}
	g.server = &gortsplib.Server{Handler: g, RTSPAddress: g.addr}
	if err := g.server.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(g.server.Close)
	return g
}

func portOf(t *testing.T, addr string) int {
	_, p := splitHostPort(t, addr)
	return p
}

func TestNativeRelayCopiesTheVideoTrackOnlyIntoThePath(t *testing.T) {
	cam := startFakeCamera(t)
	ing := startFakeIngest(t)
	p := startNative("rtsp://u:p@"+cam.addr+"/main", portOf(t, ing.addr), "cam-7", "TOKEN", nil)
	defer func() { p.Kill(); _ = p.Wait() }()
	waitForN(t, 5*time.Second, func() bool { return ing.packets.Load() >= 10 })
	ing.mu.Lock()
	defer ing.mu.Unlock()
	if strings.TrimPrefix(ing.path, "/") != "cam-7" || ing.query != "jwt=TOKEN" {
		t.Fatalf("published to %q ? %q", ing.path, ing.query)
	}
	if len(ing.medias) != 1 || ing.medias[0].Type != description.MediaTypeVideo {
		t.Fatalf("medias %+v: want the video track only", ing.medias)
	}
	if _, ok := ing.medias[0].Formats[0].(*format.H264); !ok {
		t.Fatalf("format %T: the codec must pass through", ing.medias[0].Formats[0])
	}
}

func TestNativeRelayEndsWhenTheCameraGoesQuiet(t *testing.T) {
	old := nativeStallAfter
	nativeStallAfter = 1500 * time.Millisecond
	defer func() { nativeStallAfter = old }()
	cam := startFakeCamera(t)
	ing := startFakeIngest(t)
	p := startNative("rtsp://"+cam.addr+"/main", portOf(t, ing.addr), "cam-7", "T", nil)
	waitForN(t, 5*time.Second, func() bool { return ing.packets.Load() >= 5 })
	cam.sending.Store(false)
	done := make(chan error, 1)
	go func() { done <- p.Wait() }()
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "sent nothing") {
			t.Fatalf("ended with %v", err)
		}
	case <-time.After(8 * time.Second):
		p.Kill()
		t.Fatal("a quiet camera was not noticed")
	}
}

func TestNativeRelayRefusesATargetThePolicyRefuses(t *testing.T) {
	cam := startFakeCamera(t)
	ing := startFakeIngest(t)
	pol := newFixedPolicy(mustNets("192.168.1.0/24"), nil, nil) // 127.0.0.1 is never allowed
	p := startNative("rtsp://"+cam.addr+"/main", portOf(t, ing.addr), "cam-7", "T", pol.Dial)
	err := p.Wait()
	if err == nil || !strings.Contains(err.Error(), "loopback") {
		t.Fatalf("got %v", err)
	}
	if ing.packets.Load() != 0 {
		t.Fatal("something was relayed")
	}
}

func TestNativeRelayNeedsAVideoTrack(t *testing.T) {
	cam := startFakeCamera(t)
	cam.stream.Close()
	cam.stream = &gortsplib.ServerStream{Server: cam.server, Desc: &description.Session{Medias: []*description.Media{cam.audio}}}
	if err := cam.stream.Initialize(); err != nil {
		t.Fatal(err)
	}
	ing := startFakeIngest(t)
	p := startNative("rtsp://"+cam.addr+"/main", portOf(t, ing.addr), "cam-7", "T", nil)
	if err := p.Wait(); err == nil || !strings.Contains(err.Error(), "no video track") {
		t.Fatalf("got %v", err)
	}
}

func waitForN(t *testing.T, d time.Duration, cond func() bool) {
	t.Helper()
	end := time.Now().Add(d)
	for time.Now().Before(end) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("condition not met")
}

// ── the supervisor: native first, ffmpeg when native keeps failing ─────

type failingProc struct{ done chan struct{} }

func (p *failingProc) Wait() error         { <-p.done; return nil }
func (p *failingProc) Kill()               {}
func (p *failingProc) LastLines() []string { return []string{"camera: 400 bad request"} }

func TestAStreamFallsBackToFFmpegOnlyAfterNativeKeepsFailing(t *testing.T) {
	rec := &recorder{}
	a := testAgent(rec)
	a.d.liveAfter = time.Hour // a run never goes live
	var nativeRuns atomic.Int64
	a.d.native = func(string, int, string, string) publisher.Process {
		nativeRuns.Add(1)
		p := &failingProc{done: make(chan struct{})}
		close(p.done)
		return p
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer a.stopAll()
	a.apply(ctx, &Answer{Desired: []Desired{{CameraID: 7, StreamName: "cam-7", MainURL: "rtsp://a/main"}}})
	waitFor(t, func() bool { return rec.count() >= 1 })
	if n := nativeRuns.Load(); n != nativeFallbackAfter {
		t.Fatalf("native ran %d times before ffmpeg, want %d", n, nativeFallbackAfter)
	}
	if st := a.report().Publishers[0]; st.Relay != relayFFmpeg {
		t.Fatalf("relay %q", st.Relay)
	}
}

func TestWithoutFFmpegTheNativeRelayIsUsed(t *testing.T) {
	rec := &recorder{}
	a := testAgent(rec)
	a.d.ffmpeg = ""
	var nativeRuns atomic.Int64
	a.d.native = func(string, int, string, string) publisher.Process {
		nativeRuns.Add(1)
		return &fakeProc{done: make(chan struct{})}
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer a.stopAll()
	a.apply(ctx, &Answer{Desired: []Desired{{CameraID: 7, StreamName: "cam-7", MainURL: "rtsp://a/main"}}})
	waitFor(t, func() bool {
		rep := a.report().Publishers
		return len(rep) == 1 && rep[0].State == "live" && rep[0].Relay == relayNative
	})
	if rec.count() != 0 || nativeRuns.Load() != 1 {
		t.Fatalf("ffmpeg runs %d, native runs %d", rec.count(), nativeRuns.Load())
	}
}

// TestNativeRelayAgainstRealServers relays a real RTSP server's stream into
// another (two MediaMTX, say), for a manual check:
//
//	OPENNVR_NATIVE_E2E_SRC=rtsp://127.0.0.1:18554/cam OPENNVR_NATIVE_E2E_DST_PORT=28554 \
//	OPENNVR_NATIVE_E2E_PATH=cam-7 OPENNVR_NATIVE_E2E_SECONDS=15 go test -run AgainstReal ./internal/site/
func TestNativeRelayAgainstRealServers(t *testing.T) {
	src := os.Getenv("OPENNVR_NATIVE_E2E_SRC")
	if src == "" {
		t.Skip("set OPENNVR_NATIVE_E2E_SRC and OPENNVR_NATIVE_E2E_DST_PORT")
	}
	port, _ := strconv.Atoi(os.Getenv("OPENNVR_NATIVE_E2E_DST_PORT"))
	secs, _ := strconv.Atoi(os.Getenv("OPENNVR_NATIVE_E2E_SECONDS"))
	if secs == 0 {
		secs = 10
	}
	p := startNative(src, port, os.Getenv("OPENNVR_NATIVE_E2E_PATH"), "x", nil)
	done := make(chan error, 1)
	go func() { done <- p.Wait() }()
	select {
	case err := <-done:
		t.Fatalf("the relay ended early: %v", err)
	case <-time.After(time.Duration(secs) * time.Second):
	}
	p.Kill()
	<-done
}
