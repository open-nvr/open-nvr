// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build !windows

// Package singleton keeps one agent per site: two would fight over the same
// webcams, and MediaMTX lets only one publisher into a path. The lock is an
// OS file lock, released by the OS however the agent exits.
package singleton

import (
	"errors"
	"os"

	"golang.org/x/sys/unix"
)

// ErrHeld means another agent holds the lock.
var ErrHeld = errors.New("another OpenNVR host agent is already running")

// Acquire takes the lock at path; keep the file open for the process's life.
func Acquire(path string) (*os.File, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	if err := unix.Flock(int(f.Fd()), unix.LOCK_EX|unix.LOCK_NB); err != nil {
		f.Close()
		return nil, ErrHeld
	}
	return f, nil
}
