// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package update

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

const (
	maxManifest = 1 << 20
	maxBinary   = 200 << 20
)

// Applier is the root updater's half (`opennvr-host apply-update`, run by
// its own systemd unit when the agent has staged a build). Everything in the
// staging directory was written by the unprivileged agent, so nothing there
// is trusted: it checks the signature again with the keys of the agent that
// is installed (this binary), the rules again against itself, and the hash
// of the very bytes it then installs. Files are read without following
// links.
type Applier struct {
	StagingDir string // the agent's staging directory
	BinPath    string // the installed agent
	FailedFile string // its own state: the source of a build it rolled back
	LastSync   string // touched by the agent after each sync with the server
	Keys       Keys
	Self       Self
	Platform   string
	Restart    func() error
	// Healthy: how long the new agent has to reach the server.
	Healthy time.Duration
	Logf    func(string, ...any)
	now     func() time.Time
	sleep   func(time.Duration)
}

// Apply installs a staged build, or does nothing when none is staged.
// Whatever happens, the staging directory is emptied (the path unit would
// otherwise start the updater again and again).
func (a *Applier) Apply() error {
	if a.now == nil {
		a.now = time.Now
	}
	if a.sleep == nil {
		a.sleep = time.Sleep
	}
	if _, err := os.Lstat(filepath.Join(a.StagingDir, Ready)); err != nil {
		return nil
	}
	defer os.RemoveAll(a.StagingDir)

	data, err := readRegular(filepath.Join(a.StagingDir, StagedMan), maxManifest)
	if err != nil {
		return err
	}
	sig, err := readRegular(filepath.Join(a.StagingDir, StagedSig), maxManifest)
	if err != nil {
		return err
	}
	m, err := Verify(data, sig, a.Keys)
	if err != nil {
		return err
	}
	if err := Acceptable(m, a.Self); err != nil {
		if errors.Is(err, ErrSame) {
			return nil
		}
		return err
	}
	bin, err := readRegular(filepath.Join(a.StagingDir, StagedBinary), maxBinary)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(bin)
	if _, ok := m.FileFor(a.Platform, hex.EncodeToString(sum[:])); !ok {
		return errors.New("the staged agent is not one the signed manifest lists for this machine: refusing it")
	}

	prev := a.BinPath + ".prev"
	if err := copyFile(a.BinPath, prev); err != nil {
		return fmt.Errorf("keeping the current agent: %w", err)
	}
	if err := writeAtomic(a.BinPath, bin, 0o755); err != nil {
		return fmt.Errorf("installing the update: %w", err)
	}
	started := a.now()
	if err := a.Restart(); err == nil && a.reachedServer(started) {
		a.logf("agent updated to %s", m.Build)
		_ = os.Remove(a.FailedFile)
		return nil
	}
	// Back to the agent that worked.
	old, err := os.ReadFile(prev)
	if err == nil {
		err = writeAtomic(a.BinPath, old, 0o755)
	}
	if err != nil {
		return fmt.Errorf("the update to %s did not reach the server, and putting the old agent back failed: %w", m.Build, err)
	}
	_ = a.Restart()
	_ = writeAtomic(a.FailedFile, []byte(m.Source+"\n"), 0o644)
	return fmt.Errorf("the update to %s did not reach the server within %s: the previous agent is back", m.Build, a.Healthy)
}

func (a *Applier) reachedServer(since time.Time) bool {
	deadline := since.Add(a.Healthy)
	for a.now().Before(deadline) {
		if st, err := os.Stat(a.LastSync); err == nil && st.ModTime().After(since) {
			return true
		}
		a.sleep(2 * time.Second)
	}
	return false
}

func (a *Applier) logf(format string, args ...any) {
	if a.Logf != nil {
		a.Logf(format, args...)
	}
}

// readRegular reads a regular file (never through a link) of at most max bytes.
func readRegular(path string, max int64) ([]byte, error) {
	st, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if !st.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file: refusing it", path)
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, max+1))
	if err != nil {
		return nil, err
	}
	if int64(len(b)) > max {
		return nil, fmt.Errorf("%s is too large", path)
	}
	return b, nil
}

func copyFile(src, dst string) error {
	b, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	return writeAtomic(dst, b, 0o755)
}

func writeAtomic(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp := path + ".tmp"
	_ = os.Remove(tmp)
	if err := os.WriteFile(tmp, data, mode); err != nil {
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}
