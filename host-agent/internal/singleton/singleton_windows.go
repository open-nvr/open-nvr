// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build windows

// Package singleton keeps one agent per site: two would fight over the same
// webcams, and MediaMTX lets only one publisher into a path. The lock is an
// OS file lock, released by the OS however the agent exits.
package singleton

import (
	"errors"
	"os"

	"golang.org/x/sys/windows"
)

// ErrHeld means another agent holds the lock.
var ErrHeld = errors.New("another OpenNVR host agent is already running")

// Acquire takes the lock at path; keep the file open for the process's life.
func Acquire(path string) (*os.File, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, err
	}
	ol := new(windows.Overlapped)
	err = windows.LockFileEx(windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY, 0, 1, 0, ol)
	if err != nil {
		f.Close()
		return nil, ErrHeld
	}
	return f, nil
}
