// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/onvifdisc"
)

// ErrRejected: the server answered 404, which on the site-agent routes means
// the credential (or the code, for enrol) is not accepted: revoked, replaced
// or wrong. The agent keeps trying slowly; an admin re-enrols it.
var ErrRejected = errors.New("the server does not accept this site's credential (revoked or replaced?)")

// Ingest is where video goes: host:port, TLS, with this ALPN.
type Ingest struct {
	Host string `json:"host"`
	Port int    `json:"port"`
	ALPN string `json:"alpn"`
}

// Desired is one camera the server wants relayed.
type Desired struct {
	CameraID      int    `json:"camera_id"`
	Name          string `json:"name"`
	StreamName    string `json:"stream_name"`
	SubStreamName string `json:"sub_stream_name"`
	MainURL       string `json:"main_url"` // carries the camera's credentials
	SubURL        string `json:"sub_url"`
	Mode          string `json:"mode"`
}

// Answer is what sync and wait return.
type Answer struct {
	SiteID     int       `json:"site_id"`
	Desired    []Desired `json:"desired"`
	ETag       string    `json:"etag"`
	LivePolicy string    `json:"live_policy"`
	UplinkCap  int       `json:"uplink_cap_kbps"`
	Ingest     Ingest    `json:"ingest"`
	PollAfterS int       `json:"poll_after_s"`
	WaitMaxS   int       `json:"wait_max_s"`
}

// PublisherState is one relayed stream, as reported.
type PublisherState struct {
	CameraID int     `json:"camera_id"`
	Stream   string  `json:"stream"`
	State    string  `json:"state"`
	Detail   string  `json:"detail,omitempty"`
	Since    float64 `json:"since,omitempty"`
}

// OnvifScan is the last discovery round on the site's LAN.
type OnvifScan struct {
	ScannedAt float64            `json:"scanned_at"`
	Devices   []onvifdisc.Device `json:"devices"`
}

// Report is what the agent sends on sync.
type Report struct {
	Version    string           `json:"version"`
	OS         string           `json:"os"`
	Arch       string           `json:"arch"`
	Hostname   string           `json:"hostname,omitempty"`
	Publishers []PublisherState `json:"publishers"`
	Onvif      *OnvifScan       `json:"onvif,omitempty"`
}

// Token is a short publish token for one camera's paths.
type Token struct {
	CameraID      int    `json:"camera_id"`
	StreamName    string `json:"stream_name"`
	SubStreamName string `json:"sub_stream_name"`
	Token         string `json:"token"`
}

// EnrolResult is what enrol returns, once.
type EnrolResult struct {
	SiteID int    `json:"site_id"`
	Name   string `json:"name"`
	Token  string `json:"token"`
	Ingest Ingest `json:"ingest"`
}

// Client talks to /api/v1/site-agent on the server.
type Client struct {
	Server string // https://host[:port]
	Token  string
	HTTP   *http.Client
}

// NewClient verifies the server per TLSConfig (pin / insecure).
func NewClient(server, token, pin string, insecure bool) *Client {
	host := server
	if u, err := url.Parse(server); err == nil {
		host = u.Hostname()
	}
	tr := &http.Transport{
		TLSClientConfig:     TLSConfig(host, pin, insecure, ""),
		Proxy:               http.ProxyFromEnvironment,
		TLSHandshakeTimeout: 15 * time.Second,
		IdleConnTimeout:     90 * time.Second,
	}
	// No overall timeout here: wait holds for up to ~25 s; each call sets its own.
	return &Client{Server: server, Token: token, HTTP: &http.Client{Transport: tr}}
}

func (c *Client) do(ctx context.Context, method, path string, body any, timeout time.Duration, out any) error {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	var rd io.Reader
	if body != nil {
		b, err := json.Marshal(body)
		if err != nil {
			return err
		}
		rd = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Server+"/api/v1/site-agent"+path, rd)
	if err != nil {
		return err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	switch {
	case resp.StatusCode == http.StatusNotFound:
		return ErrRejected
	case resp.StatusCode == http.StatusTooManyRequests:
		return errors.New("the server asks to slow down (429)")
	case resp.StatusCode/100 != 2:
		return fmt.Errorf("%s %s: HTTP %d", method, path, resp.StatusCode)
	}
	if out != nil {
		return json.Unmarshal(data, out)
	}
	return nil
}

// Enrol trades a one-time code for the site's credential.
func (c *Client) Enrol(ctx context.Context, code, version, hostname string) (*EnrolResult, error) {
	var out EnrolResult
	err := c.do(ctx, http.MethodPost, "/enrol",
		map[string]string{"code": code, "version": version, "hostname": hostname}, 30*time.Second, &out)
	if errors.Is(err, ErrRejected) {
		return nil, errors.New("the code was not accepted: it is wrong, already used, or older than 15 minutes")
	}
	return &out, err
}

// Sync sends the report and returns what to relay.
func (c *Client) Sync(ctx context.Context, r Report) (*Answer, error) {
	var out Answer
	err := c.do(ctx, http.MethodPost, "/sync", r, 30*time.Second, &out)
	return &out, err
}

// Wait long-polls: it returns when the desired state differs from etag, or
// after the server's wait (25 s) with the same state.
func (c *Client) Wait(ctx context.Context, etag string) (*Answer, error) {
	var out Answer
	err := c.do(ctx, http.MethodGet, "/wait?etag="+url.QueryEscape(etag), nil, 60*time.Second, &out)
	return &out, err
}

// PublishToken asks for a short publish token for one camera.
func (c *Client) PublishToken(ctx context.Context, cameraID int) (*Token, error) {
	var out Token
	err := c.do(ctx, http.MethodPost, fmt.Sprintf("/publish-token/%d", cameraID), nil, 30*time.Second, &out)
	return &out, err
}
