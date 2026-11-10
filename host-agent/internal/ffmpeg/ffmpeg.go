// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package ffmpeg finds the host's ffmpeg. The agent never ships one: it is the
// user's, installed by start.ps1 / start.sh (winget, apt, brew) or by hand.
package ffmpeg

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strings"

	"github.com/open-nvr/open-nvr/host-agent/internal/proc"
)

// Find returns the ffmpeg to use: an explicit path, then PATH, then the places
// installers put it without updating this process's PATH.
func Find(explicit string) string {
	if explicit != "" {
		if _, err := os.Stat(explicit); err == nil {
			return explicit
		}
	}
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		return p
	}
	var candidates []string
	switch runtime.GOOS {
	case "windows":
		// winget adds it to PATH only for new logon sessions.
		if la := os.Getenv("LOCALAPPDATA"); la != "" {
			matches, _ := filepath.Glob(filepath.Join(la, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin", "ffmpeg.exe"))
			candidates = append(candidates, matches...)
			candidates = append(candidates, filepath.Join(la, "Microsoft", "WinGet", "Links", "ffmpeg.exe"))
		}
		candidates = append(candidates, `C:\ProgramData\chocolatey\bin\ffmpeg.exe`)
	case "darwin":
		// A LaunchAgent's PATH has no Homebrew in it.
		candidates = append(candidates, "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg")
	default:
		candidates = append(candidates, "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg")
	}
	for _, c := range candidates {
		if _, err := os.Stat(c); err == nil {
			return c
		}
	}
	return ""
}

var versionRe = regexp.MustCompile(`ffmpeg version (\S+)`)

// Version is ffmpeg's own version string, or "".
func Version(path string) string {
	if path == "" {
		return ""
	}
	cmd := exec.Command(path, "-version")
	proc.Prepare(cmd)
	out, err := cmd.Output()
	if err != nil {
		return ""
	}
	if m := versionRe.FindStringSubmatch(string(out)); m != nil {
		return m[1]
	}
	return strings.TrimSpace(strings.SplitN(string(out), "\n", 2)[0])
}
