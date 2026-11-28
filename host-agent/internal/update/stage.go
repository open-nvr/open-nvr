// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package update

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// Staged files, in the agent's state directory.
const (
	StagingDir   = "staging"
	StagedBinary = "opennvr-host.new"
	StagedMan    = "manifest.json"
	StagedSig    = "manifest.sig"
	// Ready is written last; the root updater's path unit waits for it.
	Ready = "ready"
)

// ErrNotFound: the server has no signed build to offer.
var ErrNotFound = errors.New("no signed agent build on the server")

// Fetcher gets the server's signed manifest and an agent build.
type Fetcher interface {
	UpdateManifest(ctx context.Context) (manifest, sig []byte, err error)
	DownloadAgent(ctx context.Context, platform string) ([]byte, error)
}

// Stager is the agent's half: it finds a newer signed build, downloads it,
// checks it, and leaves it for the root updater. It never replaces anything
// itself: the agent runs unprivileged and cannot.
type Stager struct {
	StateDir string // the agent's state directory
	Keys     Keys
	Self     Self
	Platform string // "linux-amd64"
	Fetch    Fetcher
	// FailedFile: where the updater notes a build it rolled back (its
	// source hash), so the agent does not stage it again.
	FailedFile string
	Logf       func(string, ...any)
}

// Check stages a newer build if there is one. It returns the staged
// build, or "" when there was nothing (new) to stage.
func (s *Stager) Check(ctx context.Context) (string, error) {
	data, sig, err := s.Fetch.UpdateManifest(ctx)
	if errors.Is(err, ErrNotFound) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	m, err := Verify(data, sig, s.Keys)
	if errors.Is(err, ErrNoKeys) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	if err := Acceptable(m, s.Self); err != nil {
		if errors.Is(err, ErrSame) {
			return "", nil
		}
		return "", err
	}
	if s.FailedFile != "" {
		if b, err := os.ReadFile(s.FailedFile); err == nil && strings.TrimSpace(string(b)) == m.Source {
			return "", nil // rolled back before: not again
		}
	}
	dir := filepath.Join(s.StateDir, StagingDir)
	if have, err := os.ReadFile(filepath.Join(dir, StagedMan)); err == nil && bytes.Equal(have, data) {
		if _, err := os.Stat(filepath.Join(dir, Ready)); err == nil {
			return "", nil // already waiting for the updater
		}
	}
	bin, err := s.Fetch.DownloadAgent(ctx, s.Platform)
	if err != nil {
		return "", fmt.Errorf("downloading the update: %w", err)
	}
	sum := sha256.Sum256(bin)
	if _, ok := m.FileFor(s.Platform, hex.EncodeToString(sum[:])); !ok {
		return "", errors.New("the downloaded agent is not one the signed manifest lists for this machine: refusing it")
	}
	if err := os.RemoveAll(dir); err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	for name, body := range map[string][]byte{StagedBinary: bin, StagedMan: data, StagedSig: sig} {
		if err := os.WriteFile(filepath.Join(dir, name), body, 0o600); err != nil {
			return "", err
		}
	}
	if err := os.WriteFile(filepath.Join(dir, Ready), []byte(m.Source+"\n"), 0o600); err != nil {
		return "", err
	}
	if s.Logf != nil {
		s.Logf("agent update %s checked and staged; the updater installs it", m.Build)
	}
	return m.Build, nil
}
