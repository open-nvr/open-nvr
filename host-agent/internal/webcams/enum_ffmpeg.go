// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build windows || darwin

package webcams

import (
	"context"
	"os/exec"
	"runtime"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/proc"
)

// Enumerate lists the webcams through ffmpeg's own device listing (DirectShow
// on Windows, AVFoundation on macOS): the same names ffmpeg will then open.
func Enumerate(ffmpegPath string) ([]Device, error) {
	if ffmpegPath == "" {
		return nil, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	var cmd *exec.Cmd
	if runtime.GOOS == "windows" {
		cmd = exec.CommandContext(ctx, ffmpegPath, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy")
	} else {
		cmd = exec.CommandContext(ctx, ffmpegPath, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", "")
	}
	proc.Prepare(cmd)
	// The listing is printed on stderr and ffmpeg then exits non-zero (there
	// is no real input): the exit status means nothing here.
	out, _ := cmd.CombinedOutput()
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	if runtime.GOOS == "windows" {
		return ParseDshow(string(out)), nil
	}
	return ParseAVFoundation(string(out)), nil
}
