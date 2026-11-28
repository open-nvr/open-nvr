// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"encoding/base64"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/update"
)

// UpdateManifest is the server's signed agent manifest (GET
// /site-agent/update): relayed as the release made it, the server cannot
// sign one.
func (c *Client) UpdateManifest(ctx context.Context) ([]byte, []byte, error) {
	var out struct {
		Manifest  string `json:"manifest"`
		Signature string `json:"signature"`
	}
	err := c.do(ctx, http.MethodGet, "/update", nil, 30*time.Second, &out)
	if errors.Is(err, ErrRejected) {
		return nil, nil, update.ErrNotFound
	}
	if err != nil {
		return nil, nil, err
	}
	m, err1 := base64.StdEncoding.DecodeString(out.Manifest)
	s, err2 := base64.StdEncoding.DecodeString(out.Signature)
	if err1 != nil || err2 != nil {
		return nil, nil, errors.New("the server's update manifest is not readable")
	}
	return m, s, nil
}

// maxAgentDownload bounds an agent download (the builds are ~10 MB).
const maxAgentDownload = 100 << 20

// DownloadAgent downloads the agent build for platform from the server,
// over the same pinned connection as everything else.
func (c *Client) DownloadAgent(ctx context.Context, platform string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		c.Server+"/api/v1/site-agent/download/"+url.PathEscape(platform), nil)
	if err != nil {
		return nil, err
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download: HTTP %d", resp.StatusCode)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, maxAgentDownload+1))
	if err != nil {
		return nil, err
	}
	if len(b) > maxAgentDownload {
		return nil, errors.New("download: too large")
	}
	return b, nil
}

const (
	updateFirstCheck = 2 * time.Minute
	updateEvery      = 6 * time.Hour
	// LastSyncFile: touched after a sync with the server (at most every
	// lastSyncEvery), in the state directory: the root updater's sign that
	// a new agent works.
	LastSyncFile  = "last_sync"
	lastSyncEvery = 20 * time.Second
)

// updateLoop checks for a signed agent update now and then (Options.Updates).
func (a *Agent) updateLoop(ctx context.Context) {
	st := a.o.Updates
	st.Fetch = a.client
	if st.Logf == nil {
		st.Logf = a.o.Logf
	}
	wait := updateFirstCheck
	for sleepCtx(ctx, wait) {
		wait = updateEvery
		if _, err := st.Check(ctx); err != nil && ctx.Err() == nil {
			a.o.Logf("agent update: %v", err)
		}
	}
}

// touchSync records a successful sync for the updater.
func (a *Agent) touchSync() {
	if a.o.StatePath == "" || time.Since(a.lastTouch) < lastSyncEvery {
		return
	}
	a.lastTouch = time.Now()
	p := filepath.Join(filepath.Dir(a.o.StatePath), LastSyncFile)
	now := time.Now()
	if err := os.Chtimes(p, now, now); err != nil {
		_ = os.WriteFile(p, nil, 0o600)
	}
}
