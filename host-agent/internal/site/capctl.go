// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"fmt"
	"sort"
	"time"
)

// wantStream is one stream the server wants relayed.
type wantStream struct {
	cameraID  int
	kind      string // main | sub
	url, path string
	// priority 0 is kept longest under an uplink cap: sub streams, and a
	// main path fed from the sub stream (live policy sub_only). 1: main.
	priority int
}

// shedStream is a wanted stream paused to stay under the uplink cap.
type shedStream struct {
	want wantStream
	kbps float64 // what it cost when it was paused
	at   time.Time
}

const (
	capEvery = 10 * time.Second
	// A paused stream is not tried again sooner than this, so the cap does
	// not flap a stream on and off.
	resumeHold = 60 * time.Second
	// What an unmeasured stream is assumed to cost when deciding to resume it.
	assumedKbps = 2000.0
)

func (a *Agent) capLoop(ctx context.Context) {
	for sleepCtx(ctx, capEvery) {
		a.meter.Sample(time.Now())
		a.enforceCap(ctx, time.Now())
	}
}

// enforceCap keeps the relayed streams under the site's uplink cap
// (uplink_cap_kbps, set on the Sites page): over it, main streams are paused
// (newest camera first) until the total fits; with room again, the paused
// ones come back one at a time. Sub streams are never paused: they are what
// keeps every camera visible. No cap: nothing is paused.
func (a *Agent) enforceCap(ctx context.Context, now time.Time) {
	a.mu.Lock()
	limit := float64(a.capKbps)
	var stopping []*stream
	var notes []string
	switch {
	case limit <= 0:
		for k, sh := range a.shed {
			if _, ok := a.want[k]; ok {
				a.streams[k] = newStream(ctx, k, sh.want.cameraID, sh.want.kind, sh.want.url, sh.want.path, a.d)
			}
			delete(a.shed, k)
		}
	default:
		total := 0.0
		for _, s := range a.streams {
			total += a.meter.Rate(s.path)
		}
		if total > limit*1.05 {
			var mains []*stream
			for k, s := range a.streams {
				if a.want[k].priority == 1 {
					mains = append(mains, s)
				}
			}
			sort.Slice(mains, func(i, j int) bool { return mains[i].cameraID > mains[j].cameraID })
			for _, s := range mains {
				if total <= limit {
					break
				}
				r := a.meter.Rate(s.path)
				if r <= 0 {
					continue // not sending: pausing it frees nothing
				}
				a.shed[s.key] = shedStream{want: a.want[s.key], kbps: r, at: now}
				delete(a.streams, s.key)
				stopping = append(stopping, s)
				total -= r
				notes = append(notes, fmt.Sprintf("paused camera %d %s (%.0f kbps) for the %d kbps uplink cap", s.cameraID, s.kind, r, a.capKbps))
			}
		} else if total < limit*0.9 && len(a.shed) > 0 {
			keys := make([]string, 0, len(a.shed))
			for k := range a.shed {
				keys = append(keys, k)
			}
			sort.Slice(keys, func(i, j int) bool {
				x, y := a.shed[keys[i]].want, a.shed[keys[j]].want
				if x.priority != y.priority {
					return x.priority < y.priority
				}
				return x.cameraID < y.cameraID
			})
			for _, k := range keys {
				sh := a.shed[k]
				if now.Sub(sh.at) < resumeHold {
					continue
				}
				est := sh.kbps
				if est <= 0 {
					est = assumedKbps
				}
				if total+est <= limit*0.95 {
					a.streams[k] = newStream(ctx, k, sh.want.cameraID, sh.want.kind, sh.want.url, sh.want.path, a.d)
					delete(a.shed, k)
					notes = append(notes, fmt.Sprintf("resumed camera %d %s", sh.want.cameraID, sh.want.kind))
				}
				break // one at a time: measure again before the next
			}
		}
	}
	a.mu.Unlock()
	for _, s := range stopping {
		s.stop()
	}
	for _, n := range notes {
		a.o.Logf("%s", n)
	}
}

// shedStates reports the paused streams (a.mu held).
func (a *Agent) shedStatesLocked() []PublisherState {
	out := make([]PublisherState, 0, len(a.shed))
	for _, sh := range a.shed {
		out = append(out, PublisherState{
			CameraID: sh.want.cameraID, Stream: sh.want.kind, State: "uplink_cap",
			Detail: fmt.Sprintf("paused: the site's %d kbps uplink cap is reached", a.capKbps),
			Since:  float64(sh.at.UnixNano()) / 1e9,
		})
	}
	return out
}
