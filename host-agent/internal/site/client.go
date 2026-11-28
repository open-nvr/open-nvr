// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/tls"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"sync"
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
	// MainFromSub: the main path is fed from the sub stream (sub_only).
	MainFromSub bool `json:"main_from_sub"`
	// ControlHost: where core's camera requests (ONVIF, PTZ) may go.
	ControlHost string `json:"control_host"`
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
	// Camera requests core needs sent on this site's LAN (wait only).
	RPC []RPC `json:"rpc"`
}

// PublisherState is one relayed stream, as reported.
type PublisherState struct {
	CameraID int     `json:"camera_id"`
	Stream   string  `json:"stream"`
	State    string  `json:"state"`
	Detail   string  `json:"detail,omitempty"`
	Since    float64 `json:"since,omitempty"`
	// Kbps: what the stream sends upstream (last 10 s).
	Kbps float64 `json:"kbps,omitempty"`
	// Relay: "native" (built in) or "ffmpeg" (the fallback).
	Relay string `json:"relay,omitempty"`
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

	seen *pinBox // the key of the last server that passed verification
	pins *PinSet // enrolled: trust only these keys (nil: enrolment, tests)
	logf func(string, ...any)

	repinMu   sync.Mutex
	repinLast time.Time
	repinErr  error
}

func serverHost(server string) string {
	if u, err := url.Parse(server); err == nil {
		return u.Hostname()
	}
	return server
}

// NewClient verifies the server per TLSConfig (pin / insecure): for
// enrolment, before the agent has a key of its own to trust.
func NewClient(server, token, pin string, insecure bool) *Client {
	seen := &pinBox{}
	cfg := recordPin(TLSConfig(serverHost(server), pin, insecure, ""), seen)
	return &Client{Server: server, Token: token, HTTP: newHTTP(cfg), seen: seen, logf: func(string, ...any) {}}
}

// NewPinnedClient trusts the server by its key only (PinSet), and learns a
// new key only when the server proves it (repin).
func NewPinnedClient(server, token string, pins *PinSet, logf func(string, ...any)) *Client {
	if logf == nil {
		logf = func(string, ...any) {}
	}
	seen := &pinBox{}
	cfg := recordPin(PinnedTLSConfig(serverHost(server), pins, ""), seen)
	return &Client{Server: server, Token: token, HTTP: newHTTP(cfg), seen: seen, pins: pins, logf: logf}
}

func newHTTP(cfg *tls.Config) *http.Client {
	tr := &http.Transport{
		TLSClientConfig:     cfg,
		Proxy:               http.ProxyFromEnvironment,
		TLSHandshakeTimeout: 15 * time.Second,
		IdleConnTimeout:     90 * time.Second,
	}
	// No overall timeout here: wait holds for up to ~25 s; each call sets its own.
	return &http.Client{Transport: tr}
}

// SeenPin is the key of the server the last successful call reached
// (enrolment saves it).
func (c *Client) SeenPin() string { return c.seen.get() }

func (c *Client) do(ctx context.Context, method, path string, body any, timeout time.Duration, out any) error {
	err := c.doOnce(ctx, method, path, body, timeout, out)
	var ue *ErrUntrusted
	if c.pins != nil && errors.As(err, &ue) && ue.Pinned {
		if rerr := c.Repin(ctx); rerr != nil {
			return rerr
		}
		return c.doOnce(ctx, method, path, body, timeout, out)
	}
	return err
}

// ErrKeyNotProven: the server's key changed and the server could not prove
// it holds this site's credential: something in between replaced it.
var ErrKeyNotProven = errors.New("the server's certificate changed and it could not prove it is the same server; " +
	"refusing it (a firewall that inspects HTTPS? exempt the OpenNVR server from it)")

// repinEvery: a failing proof is not asked for again sooner than this.
const repinEvery = 30 * time.Second

// Repin learns the server's new key after the server proves it is the same
// server: it answers HMAC(credential hash, nonce ‖ the key this agent sees)
// without the agent sending anything secret (no credential on this call).
// A man in the middle shows its own key and cannot make that proof.
func (c *Client) Repin(ctx context.Context) error {
	c.repinMu.Lock()
	defer c.repinMu.Unlock()
	if time.Since(c.repinLast) < repinEvery {
		if c.repinErr != nil {
			return c.repinErr
		}
		return nil
	}
	c.repinLast = time.Now()
	c.repinErr = c.repin(ctx)
	return c.repinErr
}

func (c *Client) repin(ctx context.Context) error {
	id, secret, ok := tokenSecret(c.Token)
	if !ok {
		return ErrKeyNotProven
	}
	siteID, err := strconv.Atoi(id)
	if err != nil {
		return ErrKeyNotProven
	}
	nb := make([]byte, 32)
	if _, err := rand.Read(nb); err != nil {
		return err
	}
	nonce := base64.RawURLEncoding.EncodeToString(nb)
	// One connection, no reuse: the key recorded is the key of the very
	// connection that carried the proof.
	seen := &pinBox{}
	cfg := recordPin(&tls.Config{ServerName: serverHost(c.Server), MinVersion: tls.VersionTLS12,
		InsecureSkipVerify: true}, seen) //nolint:gosec // verified by the proof below
	hc := &http.Client{Transport: &http.Transport{TLSClientConfig: cfg, Proxy: http.ProxyFromEnvironment,
		DisableKeepAlives: true, TLSHandshakeTimeout: 15 * time.Second}}
	bare := &Client{Server: c.Server, HTTP: hc, seen: seen}
	var out struct {
		Proof string `json:"proof"`
	}
	if err := bare.doOnce(ctx, http.MethodPost, "/repin", map[string]any{"site_id": siteID, "nonce": nonce}, 30*time.Second, &out); err != nil {
		return fmt.Errorf("asking the server to prove its new key: %w", err)
	}
	pin := seen.get()
	if pin == "" || !hmac.Equal([]byte(out.Proof), []byte(RepinProof(secret, nonce, pin))) {
		c.logf("%v (it showed %s)", ErrKeyNotProven, pin)
		return ErrKeyNotProven
	}
	c.pins.Add(pin)
	c.logf("the server's certificate key changed; it proved it is the same server, now trusting %s", pin)
	return nil
}

func (c *Client) doOnce(ctx context.Context, method, path string, body any, timeout time.Duration, out any) error {
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

// RPCResult returns the answer to one camera request.
func (c *Client) RPCResult(ctx context.Context, id string, r RPCResult) error {
	return c.do(ctx, http.MethodPost, "/rpc/"+url.PathEscape(id)+"/result", r, 30*time.Second, nil)
}

// PublishToken asks for a short publish token for one camera.
func (c *Client) PublishToken(ctx context.Context, cameraID int) (*Token, error) {
	var out Token
	err := c.do(ctx, http.MethodPost, fmt.Sprintf("/publish-token/%d", cameraID), nil, 30*time.Second, &out)
	return &out, err
}
