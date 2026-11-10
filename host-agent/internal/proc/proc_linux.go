// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build linux

// Package proc makes child processes behave: no ffmpeg left running after the
// agent is gone.
package proc

import (
	"os"
	"os/exec"
	"syscall"
)

// Prepare sets up a command before Start: the kernel kills the child when the
// agent dies, however it dies.
func Prepare(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{Pdeathsig: syscall.SIGKILL}
}

// Adopt is a no-op: Pdeathsig already ties the child to the agent.
func Adopt(*os.Process) {}
