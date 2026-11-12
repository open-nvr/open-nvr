// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build windows

// Package proc makes child processes behave: no console window flashing up on
// the user's desktop, and no ffmpeg left running after the agent is gone.
package proc

import (
	"os"
	"os/exec"
	"sync"
	"syscall"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Prepare sets up a command before Start: no console window.
func Prepare(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		HideWindow:    true,
		CreationFlags: windows.CREATE_NO_WINDOW,
	}
}

var (
	jobOnce sync.Once
	job     windows.Handle
)

// One job object for the agent's lifetime, set to kill everything in it when
// its last handle closes — which happens when the agent exits, however it
// exits. A killed agent therefore never leaves an orphan ffmpeg holding the
// webcam.
func killOnCloseJob() windows.Handle {
	jobOnce.Do(func() {
		h, err := windows.CreateJobObject(nil, nil)
		if err != nil {
			return
		}
		info := windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION{
			BasicLimitInformation: windows.JOBOBJECT_BASIC_LIMIT_INFORMATION{
				LimitFlags: windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
			},
		}
		if _, err := windows.SetInformationJobObject(
			h, windows.JobObjectExtendedLimitInformation,
			uintptr(unsafe.Pointer(&info)), uint32(unsafe.Sizeof(info)),
		); err != nil {
			windows.CloseHandle(h)
			return
		}
		job = h
	})
	return job
}

// Adopt ties a started child to the agent's lifetime.
func Adopt(p *os.Process) {
	j := killOnCloseJob()
	if j == 0 || p == nil {
		return
	}
	h, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(p.Pid))
	if err != nil {
		return
	}
	defer windows.CloseHandle(h)
	_ = windows.AssignProcessToJobObject(j, h)
}
