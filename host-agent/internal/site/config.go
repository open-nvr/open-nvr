// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package site is the host agent's site mode (issue #925, cloud ingest): it
// runs at a customer site, next to cameras that sit behind NAT, and relays
// them to an OpenNVR on the internet. It dials out only. Control is HTTPS to
// /api/v1/site-agent; video is RTSP over TLS to port 443 with ALPN
// "opennvr-rtsps", which nginx there ends and hands to MediaMTX. Each camera
// is relayed as it is (-c copy): nothing is re-encoded at the site.
package site

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"
)

// Config is what `enrol` writes and `site` reads. It holds the site's
// credential, so it is written 0600 (on Windows the file inherits its
// folder's ACL: keep it under ProgramData or the service account's profile).
type Config struct {
	Server string `json:"server"`  // https://nvr.example.com
	SiteID int    `json:"site_id"` // for the logs
	Token  string `json:"token"`   // site_<id>_<secret>
	// Pin: "sha256/<base64 SPKI>" of the server's certificate. With a pin the
	// server is trusted if its chain verifies OR its key matches the pin, so a
	// self-signed server works and a later Let's Encrypt one (same key,
	// --reuse-key) does too.
	Pin string `json:"pin,omitempty"`
	// Insecure skips server verification altogether. For tests only.
	Insecure bool `json:"insecure_skip_verify,omitempty"`
}

// DefaultPath is where the config lives unless -config says otherwise.
func DefaultPath() string {
	if dir, err := os.UserConfigDir(); err == nil && dir != "" {
		return filepath.Join(dir, "opennvr-site", "config.json")
	}
	return "opennvr-site.json"
}

// NormaliseServer checks a server URL and returns it as https://host[:port].
func NormaliseServer(raw string) (string, error) {
	s := strings.TrimSpace(raw)
	if !strings.Contains(s, "://") {
		s = "https://" + s
	}
	u, err := url.Parse(s)
	if err != nil || u.Host == "" {
		return "", fmt.Errorf("not a server address: %q", raw)
	}
	if u.Scheme != "https" {
		return "", errors.New("the server must be reached over https")
	}
	return "https://" + u.Host, nil
}

// Hostname is the server's host name, without a port.
func (c *Config) Hostname() string {
	u, err := url.Parse(c.Server)
	if err != nil {
		return ""
	}
	return u.Hostname()
}

// Load reads the config written by `enrol`.
func Load(path string) (*Config, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var c Config
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, fmt.Errorf("%s: %w", path, err)
	}
	if c.Server == "" || !strings.HasPrefix(c.Token, "site_") {
		return nil, fmt.Errorf("%s: no enrolled site (run `opennvr-host enrol` first)", path)
	}
	return &c, nil
}

// Save writes the config atomically, readable by its owner only.
func (c *Config) Save(path string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	b, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return os.Chmod(path, 0o600)
}
