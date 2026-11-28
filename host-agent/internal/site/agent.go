// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"sync"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/onvifdisc"
	"github.com/open-nvr/open-nvr/host-agent/internal/publisher"
)

// Options are the site agent's settings (flags in cmd/opennvr-host).
type Options struct {
	Config  *Config
	FFmpeg  string // path to ffmpeg, "" = cannot relay
	Version string
	Logf    func(format string, args ...any)
	// Discover: run ONVIF discovery on the site's LAN and report it.
	Discover bool
	// ConfigPath: where Config came from; newly proven server keys are
	// saved there when it is writable ("" = not saved).
	ConfigPath string
}

// Agent is the running site mode.
type Agent struct {
	o      Options
	client *Client
	d      *deps

	mu      sync.Mutex
	streams map[string]*stream
	relay   *Relay
	rpc     *rpcRunner
	// Hosts of the cameras this site relays: core's camera requests may reach these.
	camHosts []string
	ingest   Ingest
	scan     *OnvifScan
	// pins: the server keys trusted (control and video alike); policy:
	// where on the LAN the agent may connect, whatever the server says.
	pins   *PinSet
	policy *TargetPolicy
	// Uplink: what each stream sends, the cap, what is wanted, what is paused.
	meter   *Meter
	capKbps int
	want    map[string]wantStream
	shed    map[string]shedStream
}

const (
	reportEvery   = 15 * time.Second
	discoverEvery = 2 * time.Minute
)

// Run runs until ctx ends.
func Run(ctx context.Context, o Options) error {
	if o.Logf == nil {
		o.Logf = func(string, ...any) {}
	}
	c := o.Config
	a := &Agent{
		o:       o,
		streams: map[string]*stream{},
		rpc:     newRPCRunner(o.Logf),
		meter:   NewMeter(),
		want:    map[string]wantStream{},
		shed:    map[string]shedStream{},
		policy:  NewTargetPolicy(c.AllowSubnets, c.AllowPorts, o.Logf),
	}
	a.pins = NewPinSet(c.TrustedPins(), a.savePins)
	if c.Insecure {
		a.client = NewClient(c.Server, c.Token, "", true)
	} else {
		a.client = NewPinnedClient(c.Server, c.Token, a.pins, o.Logf)
	}
	a.rpc.policy = a.policy
	a.d = &deps{
		ffmpeg:    o.FFmpeg,
		relayPort: a.relayPort,
		tokens:    a.client.PublishToken,
		logf:      o.Logf,
		run:       publisher.Start,
		backoff:   []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second, 60 * time.Second},
		liveAfter: 5 * time.Second,
		healthy:   60 * time.Second,
		checkURL:  a.policy.CheckStreamURL,
		native: func(lanURL string, port int, path, token string) publisher.Process {
			return startNative(lanURL, port, path, token, a.policy.Dial)
		},
	}
	defer a.stopAll()

	if o.Discover {
		go a.discoverLoop(ctx)
	}
	// Reports on their own clock, so a long poll never delays one.
	go a.reportLoop(ctx)
	go a.capLoop(ctx)

	etag := ""
	failures := 0
	for ctx.Err() == nil {
		var ans *Answer
		var err error
		asked := time.Now()
		waited := etag != ""
		if etag == "" {
			ans, err = a.client.Sync(ctx, a.report())
		} else {
			ans, err = a.client.Wait(ctx, etag)
		}
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			failures++
			wait := min(time.Duration(failures)*5*time.Second, 60*time.Second)
			if errors.Is(err, ErrRejected) {
				wait = 5 * time.Minute
			}
			if failures == 1 || errors.Is(err, ErrRejected) {
				o.Logf("cannot reach OpenNVR at %s: %v (retrying)", c.Server, err)
			}
			etag = "" // resync with a full report after an outage
			if !sleepCtx(ctx, wait) {
				return nil
			}
			continue
		}
		if failures > 0 {
			o.Logf("connected to OpenNVR at %s (site %d)", c.Server, ans.SiteID)
		}
		failures = 0
		if ans.ETag != etag {
			a.apply(ctx, ans)
			etag = ans.ETag
		}
		if len(ans.RPC) > 0 {
			a.rpc.handle(ctx, a.client, ans.RPC)
		} else if waited && ans.ETag == etag && time.Since(asked) < time.Second {
			// The server answered a long poll at once with nothing new: it
			// holds no more polls just now (too many sites, or another
			// agent with this site's credential). Poll gently instead.
			pause := time.Duration(ans.PollAfterS) * time.Second
			if pause <= 0 || pause > 30*time.Second {
				pause = 5 * time.Second
			}
			if !sleepCtx(ctx, pause) {
				return nil
			}
		}
	}
	return nil
}

// savePins writes newly proven server keys into the config, if it can (a
// systemd credential is read-only: the key is then proven again after a
// restart, which costs one extra request).
func (a *Agent) savePins(pins []string) {
	if a.o.ConfigPath == "" {
		return
	}
	cfg := *a.o.Config
	cfg.Pins = pins
	cfg.Pin = ""
	if err := cfg.Save(a.o.ConfigPath); err != nil {
		a.o.Logf("trusting the server's key for this run; could not save it to %s: %v", a.o.ConfigPath, err)
	}
}

func sleepCtx(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func (a *Agent) relayPort() int {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.relay == nil {
		return 0
	}
	return a.relay.Port()
}

// apply makes the running streams match the server's answer.
func (a *Agent) apply(ctx context.Context, ans *Answer) {
	a.mu.Lock()
	// The relay follows the ingest target (a site renamed, Let's Encrypt
	// switched on): a new one for new connections; running ffmpegs finish
	// on the old one and reconnect to the new one.
	if ans.Ingest.Host != "" && (a.relay == nil || ans.Ingest != a.ingest) {
		port := ans.Ingest.Port
		if port == 0 {
			port = 443
		}
		alpn := ans.Ingest.ALPN
		if alpn == "" {
			alpn = IngestALPN
		}
		// Video is verified by the same keys as control: an ingest host the
		// server names gets nothing unless it holds the server's own key.
		cfg := PinnedTLSConfig(ans.Ingest.Host, a.pins, alpn)
		if a.o.Config.Insecure {
			cfg = TLSConfig(ans.Ingest.Host, "", true, alpn)
		}
		if r, err := StartRelay(ctx, ans.Ingest.Host, port, cfg, a.o.Logf); err == nil {
			if a.relay != nil {
				a.relay.Close()
			}
			r.Meter = a.meter
			a.relay, a.ingest = r, ans.Ingest
			a.o.Logf("video goes to %s:%d (ALPN %s)", ans.Ingest.Host, port, alpn)
		} else {
			a.o.Logf("cannot start the local relay: %v", err)
		}
	}
	want := map[string]wantStream{}
	for _, d := range ans.Desired {
		if d.MainURL != "" {
			prio := 1
			if d.MainFromSub {
				prio = 0 // the camera's only stream: kept like a sub
			}
			want[streamKey(d.CameraID, "main")] = wantStream{d.CameraID, "main", d.MainURL, d.StreamName, prio}
		}
		if d.SubURL != "" && d.SubStreamName != "" {
			want[streamKey(d.CameraID, "sub")] = wantStream{d.CameraID, "sub", d.SubURL, d.SubStreamName, 0}
		}
	}
	a.want, a.capKbps = want, ans.UplinkCap
	// A paused stream that is no longer wanted, or wanted differently, is
	// forgotten (a changed one starts again below).
	for k, sh := range a.shed {
		if w, ok := want[k]; !ok || w != sh.want {
			delete(a.shed, k)
		}
	}
	a.camHosts = a.camHosts[:0]
	for _, d := range ans.Desired {
		if d.ControlHost != "" {
			a.camHosts = append(a.camHosts, d.ControlHost)
		}
		for _, raw := range []string{d.MainURL, d.SubURL} {
			if u, err := url.Parse(raw); err == nil && u.Hostname() != "" {
				a.camHosts = append(a.camHosts, u.Hostname())
			}
		}
	}
	a.refreshAllowedLocked()
	var stopping []*stream
	for k, s := range a.streams {
		w, ok := want[k]
		if !ok || w.url != s.lanURL || w.path != s.path {
			stopping = append(stopping, s)
			delete(a.streams, k)
		}
	}
	for k, w := range want {
		if _, paused := a.shed[k]; paused {
			continue // under the uplink cap; enforceCap brings it back
		}
		if _, ok := a.streams[k]; !ok {
			a.streams[k] = newStream(ctx, k, w.cameraID, w.kind, w.url, w.path, a.d)
		}
	}
	n := len(a.streams)
	a.mu.Unlock()
	for _, s := range stopping {
		s.stop()
	}
	a.o.Logf("relaying %d stream(s) for %d camera(s)", n, len(ans.Desired))
}

func streamKey(cameraID int, kind string) string { return fmt.Sprintf("%d/%s", cameraID, kind) }

// refreshAllowedLocked: core's camera requests may reach the hosts of the
// cameras this site relays and the devices its discovery found, nothing else.
func (a *Agent) refreshAllowedLocked() {
	hosts := append([]string(nil), a.camHosts...)
	if a.scan != nil {
		for _, d := range a.scan.Devices {
			hosts = append(hosts, d.IP)
		}
	}
	a.rpc.setAllowed(hosts)
}

func (a *Agent) stopAll() {
	a.mu.Lock()
	all := make([]*stream, 0, len(a.streams))
	for k, s := range a.streams {
		all = append(all, s)
		delete(a.streams, k)
	}
	if a.relay != nil {
		a.relay.Close()
	}
	a.mu.Unlock()
	for _, s := range all {
		s.stop()
	}
}

func (a *Agent) report() Report {
	host, _ := os.Hostname()
	a.mu.Lock()
	states := make([]PublisherState, 0, len(a.streams))
	for _, s := range a.streams {
		st := s.state()
		st.Kbps = a.meter.Rate(s.path)
		states = append(states, st)
	}
	states = append(states, a.shedStatesLocked()...)
	scan := a.scan
	a.mu.Unlock()
	sort.Slice(states, func(i, j int) bool {
		if states[i].CameraID != states[j].CameraID {
			return states[i].CameraID < states[j].CameraID
		}
		return states[i].Stream < states[j].Stream
	})
	return Report{
		Version: a.o.Version, OS: runtime.GOOS, Arch: runtime.GOARCH, Hostname: host,
		Publishers: states, Onvif: scan,
	}
}

func (a *Agent) reportLoop(ctx context.Context) {
	for sleepCtx(ctx, reportEvery) {
		if _, err := a.client.Sync(ctx, a.report()); err != nil && ctx.Err() == nil {
			// The main loop logs connection trouble; nothing more here.
			continue
		}
	}
}

func (a *Agent) discoverLoop(ctx context.Context) {
	for {
		devs := onvifdisc.Probe(ctx, 3*time.Second)
		if a.policy != nil {
			a.policy.SetObserved(announcedPorts(devs))
		}
		a.mu.Lock()
		a.scan = &OnvifScan{ScannedAt: nowF(), Devices: devs}
		a.refreshAllowedLocked()
		a.mu.Unlock()
		if !sleepCtx(ctx, discoverEvery) {
			return
		}
	}
}

var urlCredRe = regexp.MustCompile(`(rtsps?://)[^/@\s]+@`)

// redactURL hides credentials inside camera URLs in ffmpeg's messages.
func redactURL(s string) string { return urlCredRe.ReplaceAllString(s, "${1}***@") }

// announcedPorts: the ports each discovered camera named in its own ONVIF
// reply (its service addresses), which the target policy then allows for
// that camera.
func announcedPorts(devs []onvifdisc.Device) map[string][]int {
	out := map[string][]int{}
	for _, d := range devs {
		if d.Port > 0 {
			out[d.IP] = append(out[d.IP], d.Port)
		}
		for _, x := range d.XAddrs {
			u, err := url.Parse(x)
			if err != nil || u.Hostname() != d.IP {
				continue
			}
			port := 80
			if u.Scheme == "https" {
				port = 443
			}
			if p, err := strconv.Atoi(u.Port()); err == nil {
				port = p
			}
			out[d.IP] = append(out[d.IP], port)
		}
	}
	return out
}
