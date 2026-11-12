// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package core is the agent's only way to talk to OpenNVR: two POSTs to core on
// localhost (server/routers/host_agent.py). Core never calls the agent.
package core

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

// ErrRejected is core's single answer to a wrong key, a proxied request or a
// camera the agent was not told to feed: 404, on purpose without a reason.
var ErrRejected = errors.New("core refused the request (wrong key, or not a camera this agent feeds)")

// Webcam is one capture device the agent found.
type Webcam struct {
	ID   string `json:"id"`
	Name string `json:"name"`
}

// Ffmpeg says whether the agent can capture at all.
type Ffmpeg struct {
	Found   bool   `json:"found"`
	Version string `json:"version,omitempty"`
}

// PublisherState is how one camera's stream is doing.
type PublisherState struct {
	CameraID int     `json:"camera_id"`
	State    string  `json:"state"` // starting | live | error | device_missing | waiting
	Detail   string  `json:"detail,omitempty"`
	Since    float64 `json:"since,omitempty"`
}

// Disk is one mounted volume.
type Disk struct {
	Mount      string `json:"mount"`
	TotalBytes uint64 `json:"total_bytes"`
	FreeBytes  uint64 `json:"free_bytes"`
}

// Temperature is one sensor.
type Temperature struct {
	Label   string  `json:"label"`
	Celsius float64 `json:"celsius"`
}

// Metrics are the host's own numbers, not a container's or Docker's VM's.
type Metrics struct {
	CPUPercent       *float64      `json:"cpu_percent,omitempty"`
	CPUCount         int           `json:"cpu_count,omitempty"`
	MemoryTotalBytes uint64        `json:"memory_total_bytes,omitempty"`
	MemoryUsedBytes  uint64        `json:"memory_used_bytes,omitempty"`
	LoadAvg          []float64     `json:"load_avg,omitempty"`
	Disks            []Disk        `json:"disks"`
	Temperatures     []Temperature `json:"temperatures"`
	RecordingsDisk   *Disk         `json:"recordings_disk,omitempty"`
}

// Report is everything the agent tells core on each sync.
type Report struct {
	Version    string           `json:"version"`
	Role       string           `json:"role"`
	OS         string           `json:"os"`
	Arch       string           `json:"arch"`
	Hostname   string           `json:"hostname,omitempty"`
	Ffmpeg     Ffmpeg           `json:"ffmpeg"`
	Webcams    []Webcam         `json:"webcams"`
	Publishers []PublisherState `json:"publishers"`
	Metrics    *Metrics         `json:"metrics,omitempty"`
}

// Desired is one camera the agent should feed, and from which webcam.
// DeviceName lets the agent find the same webcam after it moved to another
// USB port (its id changed); see webcams.Resolve.
type Desired struct {
	CameraID   int    `json:"camera_id"`
	DeviceID   string `json:"device_id"`
	DeviceName string `json:"device_name,omitempty"`
	Width      int    `json:"width"`
	Height     int    `json:"height"`
	FPS        int    `json:"fps"`
}

// SyncResponse is core's answer: what to do, when to call again, and which
// build core is (an agent built for another core gets replaced).
type SyncResponse struct {
	CoreBuild  string    `json:"core_build"`
	Desired    []Desired `json:"desired"`
	PollAfterS int       `json:"poll_after_s"`
}

// Client calls core with the agent key.
type Client struct {
	Base string // e.g. http://127.0.0.1:8000
	Key  string
	HTTP *http.Client
}

// New returns a client with sane timeouts.
func New(base, key string) *Client {
	return &Client{
		Base: strings.TrimRight(base, "/"),
		Key:  key,
		HTTP: &http.Client{Timeout: 15 * time.Second},
	}
}

func (c *Client) post(ctx context.Context, path string, body any, out any) error {
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.Base+path, rd)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Host-Agent-Key", c.Key)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return ErrRejected
	case resp.StatusCode < 200 || resp.StatusCode > 299:
		return fmt.Errorf("core answered %d: %s", resp.StatusCode, strings.TrimSpace(string(data)))
	}
	if out == nil {
		return nil
	}
	return json.Unmarshal(data, out)
}

// Sync reports and fetches the desired state.
func (c *Client) Sync(ctx context.Context, r Report) (SyncResponse, error) {
	var out SyncResponse
	err := c.post(ctx, "/api/v1/host-agent/sync", r, &out)
	return out, err
}

// PublishToken fetches a short publish-only token for one camera's path.
func (c *Client) PublishToken(ctx context.Context, cameraID int) (path, token string, err error) {
	var out struct {
		StreamName string `json:"stream_name"`
		Token      string `json:"token"`
	}
	if err = c.post(ctx, fmt.Sprintf("/api/v1/host-agent/publish-token/%d", cameraID), nil, &out); err != nil {
		return "", "", err
	}
	if out.StreamName == "" || out.Token == "" {
		return "", "", errors.New("core sent an empty publish token")
	}
	return out.StreamName, out.Token, nil
}
