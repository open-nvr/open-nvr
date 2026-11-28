// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"encoding/base64"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// A setup code is what a setup link carries (server/services/site_setup.py):
//
//	https://<server>/site-setup#<id>.<secret>   the link itself
//	<server>#<id>.<secret>                      the code the setup page shows
//
// The agent never sends the secret until the server has proven it knows it.

// SetupCode is a parsed setup code.
type SetupCode struct {
	Server  string // https://host[:port]
	TokenID string
	Secret  string
}

// Token is "<id>.<secret>", what enrol takes.
func (c SetupCode) Token() string { return c.TokenID + "." + c.Secret }

// ParseSetupCode reads a setup link or code, as pasted (surrounding spaces,
// quotes and a trailing newline are fine).
func ParseSetupCode(raw string) (SetupCode, error) {
	s := strings.Trim(strings.TrimSpace(raw), `"'`)
	hostPart, frag, ok := strings.Cut(s, "#")
	if !ok {
		return SetupCode{}, errors.New("this is not a setup code: it has no '#'; copy the whole link or code")
	}
	if !strings.Contains(hostPart, "://") {
		hostPart = "https://" + hostPart
	}
	u, err := url.Parse(hostPart)
	if err != nil || u.Host == "" {
		return SetupCode{}, fmt.Errorf("this is not a setup code: no server in %q", hostPart)
	}
	if u.Scheme != "https" {
		return SetupCode{}, errors.New("a setup code's server must be https")
	}
	tid, secret, ok := strings.Cut(frag, ".")
	if !ok || len(tid) < 4 || len(tid) > 16 || len(secret) < 16 || len(secret) > 64 {
		return SetupCode{}, errors.New("this setup code is incomplete: copy it again, all of it")
	}
	return SetupCode{Server: "https://" + u.Host, TokenID: tid, Secret: secret}, nil
}

// SetupProofKey and SetupHelloProof mirror site_setup.proof_key and
// hello_proof on the server.
func SetupProofKey(secret string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte("opennvr-site-proof"))
	return hex.EncodeToString(mac.Sum(nil))
}

func SetupHelloProof(key, nonce, pin string) string {
	mac := hmac.New(sha256.New, []byte(key))
	mac.Write([]byte("opennvr-site-hello\n" + nonce + "\n" + pin))
	return hex.EncodeToString(mac.Sum(nil))
}

// ErrServerNotProven: the server could not prove it made the setup code.
var ErrServerNotProven = errors.New("the server could not prove it is the server that made this setup code. " +
	"Nothing was sent to it. Something between this machine and the server may be intercepting HTTPS " +
	"(a firewall that inspects it?), or the code was changed. Ask for a new setup link")

// SetupResult is a finished setup: the site's credential and the server key
// to trust from now on.
type SetupResult struct {
	*EnrolResult
	Server string
	Pin    string
}

// Setup proves the server (hello) and then enrols with the setup code over
// a connection pinned to the key it proved.
func Setup(ctx context.Context, code SetupCode, version, hostname string) (*SetupResult, error) {
	nb := make([]byte, 32)
	if _, err := rand.Read(nb); err != nil {
		return nil, err
	}
	nonce := base64.RawURLEncoding.EncodeToString(nb)

	// 1. hello: one connection, no verification yet (that is what the proof
	// is for), and only the link's id and a nonce on it.
	seen := &pinBox{}
	cfg := recordPin(&tls.Config{ServerName: serverHost(code.Server), MinVersion: tls.VersionTLS12,
		InsecureSkipVerify: true}, seen) //nolint:gosec // verified by the proof below
	hc := &http.Client{Transport: &http.Transport{TLSClientConfig: cfg, Proxy: http.ProxyFromEnvironment,
		DisableKeepAlives: true, TLSHandshakeTimeout: 15 * time.Second}}
	bare := &Client{Server: code.Server, HTTP: hc, seen: seen}
	var out struct {
		Proof string `json:"proof"`
	}
	if err := bare.doOnce(ctx, http.MethodPost, "/hello", map[string]string{"token_id": code.TokenID, "nonce": nonce},
		30*time.Second, &out); err != nil {
		return nil, fmt.Errorf("cannot reach %s: %w", code.Server, err)
	}
	pin := seen.get()
	want := SetupHelloProof(SetupProofKey(code.Secret), nonce, pin)
	if pin == "" || !hmac.Equal([]byte(out.Proof), []byte(want)) {
		return nil, ErrServerNotProven
	}

	// 2. enrol, trusting that key only.
	pinned := NewPinnedClient(code.Server, "", NewPinSet([]string{pin}, nil), nil)
	var res EnrolResult
	err := pinned.doOnce(ctx, http.MethodPost, "/enrol",
		map[string]string{"token": code.Token(), "version": version, "hostname": hostname}, 30*time.Second, &res)
	if errors.Is(err, ErrRejected) {
		return nil, errors.New("the setup link was not accepted: it was already used, revoked, or it expired. Ask for a new one")
	}
	if err != nil {
		return nil, err
	}
	return &SetupResult{EnrolResult: &res, Server: code.Server, Pin: pin}, nil
}
