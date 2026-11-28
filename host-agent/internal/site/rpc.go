// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bytes"
	"context"
	"crypto/tls"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"
)

// RPC is one HTTP request core needs sent to a camera on this site's LAN
// (ONVIF, PTZ, a vendor settings API). Core keeps all the protocol logic;
// the agent only sends the bytes and returns the answer.
type RPC struct {
	ID       string              `json:"id"`
	Method   string              `json:"method"`
	URL      string              `json:"url"`
	Headers  map[string][]string `json:"headers"`
	BodyB64  string              `json:"body_b64"`
	TimeoutS float64             `json:"timeout_s"`
}

// RPCResult is the camera's answer, or why there is none.
type RPCResult struct {
	Status  int                 `json:"status,omitempty"`
	Headers map[string][]string `json:"headers,omitempty"`
	BodyB64 string              `json:"body_b64"`
	Error   string              `json:"error,omitempty"`
}

const (
	rpcMaxTimeout  = 20 * time.Second
	rpcMaxResponse = 4 << 20
	rpcMaxParallel = 8
)

// rpcRunner sends core's requests, but only to this site's own cameras:
// the hosts of the cameras it relays and the devices its ONVIF discovery
// found, and of those only what the agent's own TargetPolicy allows. The
// host list comes from the server, so it alone would not stop a compromised
// server naming the router or a NAS as a "camera"; the policy does.
type rpcRunner struct {
	mu      sync.Mutex
	allowed map[string]bool
	client  *http.Client
	sem     chan struct{}
	logf    func(string, ...any)
	// policy is the agent's own rule (TargetPolicy): even a host core lists
	// is reached only if it passes. nil: tests that predate it.
	policy *TargetPolicy
	// allowLoopback: tests only (their camera is an httptest server).
	allowLoopback bool
}

func newRPCRunner(logf func(string, ...any)) *rpcRunner {
	r := &rpcRunner{allowed: map[string]bool{}, sem: make(chan struct{}, rpcMaxParallel), logf: logf}
	dialer := &net.Dialer{Timeout: 10 * time.Second}
	tr := &http.Transport{
		Proxy: nil, // a camera on the LAN, never through a proxy
		// Cameras present self-signed certificates on the LAN; core does
		// not verify them either.
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, //nolint:gosec
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			c, err := dialer.DialContext(ctx, network, addr)
			if err != nil {
				return nil, err
			}
			// The address actually dialled, after any name resolution: a
			// name that resolves elsewhere now (DNS rebinding) is caught here.
			if a, ok := c.RemoteAddr().(*net.TCPAddr); ok {
				if err := r.check(a.IP, a.Port); err != nil {
					c.Close()
					return nil, err
				}
			}
			return c, nil
		},
		MaxIdleConnsPerHost: 2,
		IdleConnTimeout:     30 * time.Second,
	}
	r.client = &http.Client{
		Transport: tr,
		// Never follow a redirect: it could point anywhere.
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	return r
}

// check: may a camera request go to ip:port?
func (r *rpcRunner) check(ip net.IP, port int) error {
	if r.allowLoopback && ip.IsLoopback() {
		return nil
	}
	if forbiddenIP(ip) {
		return fmt.Errorf("refusing to reach %s", ip)
	}
	if r.policy != nil {
		return r.policy.Check(ip, port)
	}
	return nil
}

func forbiddenIP(ip net.IP) bool {
	return ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() ||
		ip.IsMulticast() || ip.IsUnspecified()
}

// setAllowed replaces the set of hosts requests may go to.
func (r *rpcRunner) setAllowed(hosts []string) {
	m := make(map[string]bool, len(hosts))
	for _, h := range hosts {
		if h = strings.ToLower(strings.TrimSpace(h)); h != "" {
			m[h] = true
		}
	}
	r.mu.Lock()
	r.allowed = m
	r.mu.Unlock()
}

func (r *rpcRunner) isAllowed(host string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.allowed[strings.ToLower(host)]
}

// run sends one request and returns the camera's answer; it never panics
// and always returns a result to post back.
func (r *rpcRunner) run(ctx context.Context, rpc RPC) RPCResult {
	u, err := url.Parse(rpc.URL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" {
		return RPCResult{Error: "not an http(s) URL"}
	}
	if !r.isAllowed(u.Hostname()) {
		return RPCResult{Error: fmt.Sprintf("%s is not a camera of this site", u.Hostname())}
	}
	if ip := net.ParseIP(u.Hostname()); ip != nil {
		port := 80
		if u.Scheme == "https" {
			port = 443
		}
		if p, err := strconv.Atoi(u.Port()); err == nil {
			port = p
		}
		if err := r.check(ip, port); err != nil {
			return RPCResult{Error: "refusing that address: " + err.Error()}
		}
	}
	body, err := base64.StdEncoding.DecodeString(rpc.BodyB64)
	if err != nil {
		return RPCResult{Error: "bad body"}
	}
	timeout := time.Duration(rpc.TimeoutS * float64(time.Second))
	if timeout <= 0 || timeout > rpcMaxTimeout {
		timeout = rpcMaxTimeout
	}
	rctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(rctx, rpc.Method, u.String(), bytes.NewReader(body))
	if err != nil {
		return RPCResult{Error: err.Error()}
	}
	for k, vs := range rpc.Headers {
		switch strings.ToLower(k) {
		// Go sets these itself; Accept-Encoding left to Go means it also
		// decompresses, so core gets the body as the camera meant it.
		case "host", "content-length", "connection", "transfer-encoding", "accept-encoding":
			continue
		}
		for _, v := range vs {
			req.Header.Add(k, v)
		}
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return RPCResult{Error: redactURL(err.Error())}
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, rpcMaxResponse+1))
	if err != nil {
		return RPCResult{Error: "reading the camera's answer: " + err.Error()}
	}
	if len(data) > rpcMaxResponse {
		return RPCResult{Error: "the camera's answer is too large to relay"}
	}
	return RPCResult{Status: resp.StatusCode, Headers: resp.Header, BodyB64: base64.StdEncoding.EncodeToString(data)}
}

// handle runs each request (a few at a time) and posts its result.
func (r *rpcRunner) handle(ctx context.Context, c *Client, rpcs []RPC) {
	for _, rpc := range rpcs {
		rpc := rpc
		select {
		case r.sem <- struct{}{}:
		case <-ctx.Done():
			return
		}
		go func() {
			defer func() { <-r.sem }()
			res := r.run(ctx, rpc)
			if err := c.RPCResult(ctx, rpc.ID, res); err != nil && !errors.Is(err, context.Canceled) {
				r.logf("could not return camera request %s: %v", rpc.ID, err)
			}
		}()
	}
}
