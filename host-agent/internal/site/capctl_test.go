// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"io"
	"net"
	"testing"
	"time"
)

func TestRTSPPathIsTakenFromTheFirstRequest(t *testing.T) {
	for in, want := range map[string]string{
		"OPTIONS rtsp://127.0.0.1:40123/cam-7?jwt=abc RTSP/1.0\r\nCSeq: 1\r\n": "cam-7",
		"ANNOUNCE rtsp://127.0.0.1:1/cam-7-sub RTSP/1.0\r\n":                   "cam-7-sub",
		"garbage": "",
	} {
		if got := rtspPath([]byte(in)); got != want {
			t.Fatalf("%q: got %q want %q", in, got, want)
		}
	}
}

func TestMeterSamplesKbpsPerPath(t *testing.T) {
	m := NewMeter()
	t0 := m.at
	m.Add("cam-7", 125_000) // 1 Mbit
	rates := m.Sample(t0.Add(time.Second))
	if r := rates["cam-7"]; r < 999 || r > 1001 {
		t.Fatalf("rate %v", r)
	}
	if m.Rate("cam-7") != rates["cam-7"] || m.Rate("other") != 0 {
		t.Fatal("Rate disagrees with the sample")
	}
}

func TestTheRelayCountsWhatEachPathSends(t *testing.T) {
	addr, pin := selfSignedTLSServer(t, []string{IngestALPN}, func(c net.Conn) {
		defer c.Close()
		_, _ = io.Copy(io.Discard, c)
	})
	host, port := splitHostPort(t, addr)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	r, err := StartRelay(ctx, host, port, TLSConfig(host, pin, false, IngestALPN), t.Logf)
	if err != nil {
		t.Fatal(err)
	}
	r.Meter = NewMeter()
	c, err := net.Dial("tcp", net.JoinHostPort("127.0.0.1", itoaT(r.Port())))
	if err != nil {
		t.Fatal(err)
	}
	first := "OPTIONS rtsp://127.0.0.1:1/cam-9?jwt=x RTSP/1.0\r\n\r\n"
	_, _ = c.Write([]byte(first))
	_, _ = c.Write(make([]byte, 10_000))
	c.Close()
	waitFor(t, func() bool {
		r.Meter.mu.Lock()
		defer r.Meter.mu.Unlock()
		return r.Meter.bytes["cam-9"] == uint64(len(first)+10_000)
	})
}

// capAgent: three cameras running (mains + one sub), rates set by hand.
func capAgent(t *testing.T, capKbps int) (*Agent, *recorder, context.CancelFunc) {
	t.Helper()
	rec := &recorder{}
	a := testAgent(rec)
	ctx, cancel := context.WithCancel(context.Background())
	a.apply(ctx, &Answer{UplinkCap: capKbps, Desired: []Desired{
		{CameraID: 1, StreamName: "cam-1", SubStreamName: "cam-1-sub", MainURL: "rtsp://a/1", SubURL: "rtsp://a/1s"},
		{CameraID: 2, StreamName: "cam-2", MainURL: "rtsp://a/2"},
		{CameraID: 3, StreamName: "cam-3", MainURL: "rtsp://a/3s", MainFromSub: true},
	}})
	waitFor(t, func() bool { return rec.count() == 4 })
	a.meter.kbps = map[string]float64{"cam-1": 3000, "cam-1-sub": 500, "cam-2": 3000, "cam-3": 500}
	return a, rec, func() { a.stopAll(); cancel() }
}

func TestOverTheCapMainsArePausedNewestFirstAndSubsNever(t *testing.T) {
	a, _, stop := capAgent(t, 4500) // total 7000
	defer stop()
	now := time.Now()
	a.enforceCap(context.Background(), now)
	a.mu.Lock()
	_, shed2 := a.shed["2/main"]
	_, shed1 := a.shed["1/main"]
	_, run1s := a.streams["1/sub"]
	_, run3 := a.streams["3/main"] // main fed from the sub: kept like a sub
	a.mu.Unlock()
	if !shed2 || shed1 || !run1s || !run3 {
		t.Fatalf("shed2=%v shed1=%v sub1 running=%v cam3 running=%v", shed2, shed1, run1s, run3)
	}
	// Reported as paused for the cap.
	found := false
	for _, p := range a.report().Publishers {
		if p.CameraID == 2 && p.State == "uplink_cap" {
			found = true
		}
	}
	if !found {
		t.Fatal("the paused stream is not reported")
	}

	// Still over (1 main 3000 + subs 1000 = 4000 < 4500): nothing else paused.
	a.meter.kbps = map[string]float64{"cam-1": 3000, "cam-1-sub": 500, "cam-3": 500}
	a.enforceCap(context.Background(), now.Add(10*time.Second))
	a.mu.Lock()
	n := len(a.shed)
	a.mu.Unlock()
	if n != 1 {
		t.Fatalf("shed %d streams", n)
	}
}

func TestPausedStreamsComeBackWithRoomAfterTheHold(t *testing.T) {
	a, rec, stop := capAgent(t, 4500)
	defer stop()
	now := time.Now()
	a.enforceCap(context.Background(), now)
	before := rec.count()
	// Camera 1's main went quiet: 1000 used, cam-2 needs 3000, 4000 <= 4275.
	a.meter.kbps = map[string]float64{"cam-1": 0, "cam-1-sub": 500, "cam-3": 500}
	a.enforceCap(context.Background(), now.Add(30*time.Second)) // within the hold
	if rec.count() != before {
		t.Fatal("resumed within the hold")
	}
	a.enforceCap(context.Background(), now.Add(2*time.Minute))
	waitFor(t, func() bool { return rec.count() == before+1 })
	a.mu.Lock()
	defer a.mu.Unlock()
	if len(a.shed) != 0 {
		t.Fatal("still paused")
	}
}

func TestNoCapBringsEverythingBack(t *testing.T) {
	a, rec, stop := capAgent(t, 4500)
	defer stop()
	a.enforceCap(context.Background(), time.Now())
	before := rec.count()
	a.mu.Lock()
	a.capKbps = 0
	a.mu.Unlock()
	a.enforceCap(context.Background(), time.Now())
	waitFor(t, func() bool { return rec.count() == before+1 })
}
