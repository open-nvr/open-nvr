// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build darwin

// Package proc makes child processes behave. macOS has no parent-death signal;
// the agent stops its children on a clean exit, and ffmpeg ends on its own
// when its RTSP session drops after a crash.
package proc

import (
	"os"
	"os/exec"
)

// Prepare sets up a command before Start.
func Prepare(*exec.Cmd) {}

// Adopt is a no-op on macOS (see the package comment).
func Adopt(*os.Process) {}
