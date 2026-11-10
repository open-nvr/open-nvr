// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package agent is the main loop: find webcams, measure the host, sync with
// core, make the streams match what core wants. Every few seconds, for as
// long as the machine runs.
package agent

import (
	"bufio"
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/core"
	"github.com/open-nvr/open-nvr/host-agent/internal/ffmpeg"
	"github.com/open-nvr/open-nvr/host-agent/internal/metrics"
	"github.com/open-nvr/open-nvr/host-agent/internal/onvifdisc"
	"github.com/open-nvr/open-nvr/host-agent/internal/publisher"
	"github.com/open-nvr/open-nvr/host-agent/internal/webcams"
)

// ExitReplaced is the exit code for "a newer agent was installed next to me":
// the service manager restarts the agent, on the new binary.
const ExitReplaced = 75

// Options are the agent's settings (flags in cmd/opennvr-host).
type Options struct {
	Core    string // core on this host, e.g. http://127.0.0.1:8000
	RTSPS   string // MediaMTX RTSPS on this host, e.g. rtsps://127.0.0.1:8322
	KeyFile string
	Repo    string // the OpenNVR folder (for .env and ./recordings)
	FFmpeg  string // explicit ffmpeg path, or "" to look for one
	Logf    func(format string, args ...any)
}

// Run runs until ctx ends (exit 0) or the agent was replaced (ExitReplaced).
func Run(ctx context.Context, o Options) int {
	logf := o.Logf
	exe, _ := os.Executable()
	startStat := statOf(exe)
	hostname, _ := os.Hostname()
	recordings := recordingsDir(o.Repo)

	ff := ffmpeg.Find(o.FFmpeg)
	ffVersion := ffmpeg.Version(ff)
	if ff == "" {
		logf("ffmpeg not found: webcams cannot be streamed until it is installed")
	} else {
		logf("ffmpeg %s at %s", ffVersion, ff)
	}

	client := core.New(o.Core, "")
	mgr := publisher.NewManager(publisher.Config{FFmpeg: ff, RTSPS: o.RTSPS, Logf: logf}, client)
	defer mgr.StopAll()

	var (
		present     []webcams.Device
		lastEnum    time.Time
		lastFFCheck = time.Now()
		desired     []core.Desired
		failures    int
		wasOK       bool
	)
	backoff := []time.Duration{2 * time.Second, 5 * time.Second, 10 * time.Second, 30 * time.Second}

	// ONVIF discovery runs beside the loop: a WS-Discovery round takes a few
	// seconds and must not hold up a sync. A round every minute keeps a list
	// ready; core can ask for one now (Discover opened), and the loop syncs
	// as soon as it is done so the answer reaches the dialog at once.
	disc := newDiscoverer(ctx)

	for {
		wait := 5 * time.Second

		// The key is re-read every round: `start` may create or rotate it
		// while the agent runs.
		key, err := readKey(o.KeyFile)
		if err != nil {
			logf("no agent key yet (%v); waiting for `start` to create it", err)
			if !sleep(ctx, 30*time.Second) {
				return 0
			}
			continue
		}
		client.Key = key

		// ffmpeg installed while we run: pick it up without a restart.
		if ff == "" && time.Since(lastFFCheck) > time.Minute {
			lastFFCheck = time.Now()
			if ff = ffmpeg.Find(o.FFmpeg); ff != "" {
				ffVersion = ffmpeg.Version(ff)
				logf("ffmpeg found: %s", ff)
				mgr.StopAll()
				mgr = publisher.NewManager(publisher.Config{FFmpeg: ff, RTSPS: o.RTSPS, Logf: logf}, client)
			}
		}

		// Plugged or unplugged webcams show up within one enumeration.
		if time.Since(lastEnum) >= 10*time.Second {
			if devs, err := webcams.Enumerate(ff); err == nil {
				present = devs
			} else {
				logf("webcam enumeration failed: %v", err)
			}
			lastEnum = time.Now()
			mgr.Reconcile(desired, present)
		}

		report := core.Report{
			Version:    buildinfo.Build,
			Role:       "user",
			OS:         runtime.GOOS,
			Arch:       runtime.GOARCH,
			Hostname:   hostname,
			Ffmpeg:     core.Ffmpeg{Found: ff != "", Version: ffVersion},
			Webcams:    reportWebcams(present, mgr.InUse()),
			Publishers: mgr.States(),
			Metrics:    metrics.Collect(recordings),
			Onvif:      disc.latest(),
		}
		resp, err := client.Sync(ctx, report)
		switch {
		case ctx.Err() != nil:
			return 0
		case errors.Is(err, core.ErrRejected):
			if wasOK || failures == 0 {
				logf("core refused the agent key; run `start host-agent install` again if the key was replaced")
			}
			wasOK = false
			failures++
			wait = 30 * time.Second
		case err != nil:
			if wasOK || failures == 0 {
				logf("cannot reach OpenNVR at %s: %v", o.Core, err)
			}
			wasOK = false
			wait = backoff[min(failures, len(backoff)-1)]
			failures++
		default:
			if !wasOK {
				logf("connected to OpenNVR (core %s)", resp.CoreBuild)
			}
			wasOK, failures = true, 0
			if replaced(resp.CoreBuild, exe, startStat) {
				logf("core is %s and a new agent is installed; exiting so it starts", resp.CoreBuild)
				return ExitReplaced
			}
			if !sameDesired(desired, resp.Desired) {
				desired = resp.Desired
				lastEnum = time.Time{} // enumerate now, then reconcile
				wait = 0
			}
			if resp.DiscoverOnvif {
				disc.request()
			}
			if resp.PollAfterS >= 2 && resp.PollAfterS <= 60 {
				wait = min(wait, time.Duration(resp.PollAfterS)*time.Second)
				if wait == 0 {
					wait = 100 * time.Millisecond
				}
			}
		}
		// Wake early when a discovery round finishes, so its result is
		// reported now, not at the next poll.
		select {
		case <-ctx.Done():
			return 0
		case <-disc.done:
		case <-time.After(wait):
		}
	}
}

// discoverer runs WS-Discovery rounds in the background.
type discoverer struct {
	mu     sync.Mutex
	scan   *core.OnvifScan
	wakeup chan struct{}
	done   chan struct{}
}

const (
	discoverEvery = time.Minute
	discoverWait  = 3 * time.Second
)

func newDiscoverer(ctx context.Context) *discoverer {
	d := &discoverer{wakeup: make(chan struct{}, 1), done: make(chan struct{}, 1)}
	go func() {
		for {
			devs := onvifdisc.Probe(ctx, discoverWait)
			d.mu.Lock()
			d.scan = &core.OnvifScan{ScannedAt: float64(time.Now().UnixNano()) / 1e9, Devices: devs}
			d.mu.Unlock()
			select {
			case d.done <- struct{}{}:
			default:
			}
			select {
			case <-ctx.Done():
				return
			case <-d.wakeup:
			case <-time.After(discoverEvery):
			}
		}
	}()
	return d
}

func (d *discoverer) request() {
	select {
	case d.wakeup <- struct{}{}:
	default:
	}
}

func (d *discoverer) latest() *core.OnvifScan {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.scan
}

// reportWebcams is the enumeration plus the webcams being streamed, which
// some capture stacks stop listing while they are open.
func reportWebcams(present, inUse []webcams.Device) []core.Webcam {
	seen := map[string]bool{}
	out := []core.Webcam{}
	for _, list := range [][]webcams.Device{present, inUse} {
		for _, d := range list {
			if seen[d.ID] {
				continue
			}
			seen[d.ID] = true
			out = append(out, core.Webcam{ID: d.ID, Name: d.Name})
		}
	}
	return out
}

func sameDesired(a, b []core.Desired) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

type fileStat struct {
	size int64
	mod  time.Time
	ok   bool
}

func statOf(path string) fileStat {
	st, err := os.Stat(path)
	if err != nil {
		return fileStat{}
	}
	return fileStat{size: st.Size(), mod: st.ModTime(), ok: true}
}

// replaced: core is another build than this agent, AND a different binary
// now sits where this one was started from (`start` installs it there). Both,
// so a core rebuilt without its agent never makes the agent restart in a loop.
func replaced(coreBuild, exe string, started fileStat) bool {
	if buildinfo.Build == "dev" || coreBuild == "" || coreBuild == buildinfo.Build {
		return false
	}
	now := statOf(exe)
	return started.ok && now.ok && (now.size != started.size || !now.mod.Equal(started.mod))
}

func readKey(path string) (string, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	key := strings.TrimSpace(string(b))
	if !strings.HasPrefix(key, "ohk_") {
		return "", errors.New("the key file does not hold an agent key")
	}
	return key, nil
}

// recordingsDir is the host folder recordings go to: RECORDINGS_PATH in the
// site's .env (relative to the OpenNVR folder, as compose reads it), else
// ./recordings.
func recordingsDir(repo string) string {
	if repo == "" {
		return ""
	}
	p := "./recordings"
	if f, err := os.Open(filepath.Join(repo, ".env")); err == nil {
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			line := strings.TrimSpace(sc.Text())
			if v, ok := strings.CutPrefix(line, "RECORDINGS_PATH="); ok {
				if v = strings.Trim(strings.TrimSpace(v), `"'`); v != "" {
					p = v
				}
			}
		}
		f.Close()
	}
	if !filepath.IsAbs(p) {
		p = filepath.Join(repo, p)
	}
	return p
}

func sleep(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}
