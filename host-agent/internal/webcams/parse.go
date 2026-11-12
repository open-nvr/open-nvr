// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package webcams

import (
	"regexp"
	"strings"
)

var (
	// [dshow @ 000002058c09ed40] "HP HD Camera" (video)
	dshowDevice = regexp.MustCompile(`\]\s+"([^"]+)"(?:\s+\((video|audio|none)\))?\s*$`)
	// [dshow @ ...]   Alternative name "@device_pnp_\\?\usb#..."
	dshowAlt = regexp.MustCompile(`Alternative name\s+"([^"]+)"`)
	// [AVFoundation indev @ 0x7f8] [0] FaceTime HD Camera
	avfDevice = regexp.MustCompile(`\]\s+\[(\d+)\]\s+(.+?)\s*$`)
)

// ParseDshow reads `ffmpeg -list_devices true -f dshow -i dummy`.
//
// Two output styles exist: current ffmpeg tags each device "(video)" /
// "(audio)"; older builds print a "DirectShow video devices" header before
// each section instead. Both are handled. A device's id is its alternative
// name (the PnP path), which ffmpeg also accepts as video=<id>; without one,
// the friendly name.
func ParseDshow(out string) []Device {
	var devs []Device
	section := ""
	var last *Device
	for _, line := range strings.Split(strings.ReplaceAll(out, "\r\n", "\n"), "\n") {
		lower := strings.ToLower(line)
		switch {
		case strings.Contains(lower, "directshow video devices"):
			section, last = "video", nil
			continue
		case strings.Contains(lower, "directshow audio devices"):
			section, last = "audio", nil
			continue
		}
		if m := dshowAlt.FindStringSubmatch(line); m != nil {
			if last != nil {
				last.ID = "dshow:" + m[1]
				last.Input = []string{"-f", "dshow", "-rtbufsize", "64M", "-i", "video=" + m[1]}
			}
			continue
		}
		m := dshowDevice.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		kind := m[2]
		if kind == "" {
			kind = section
		}
		if kind != "video" {
			last = nil
			continue
		}
		devs = append(devs, Device{
			ID:    "dshow:" + m[1],
			Name:  m[1],
			Input: []string{"-f", "dshow", "-rtbufsize", "64M", "-i", "video=" + m[1]},
		})
		last = &devs[len(devs)-1]
	}
	return devs
}

// ParseAVFoundation reads `ffmpeg -f avfoundation -list_devices true -i ""`:
// the video section only (audio devices follow "AVFoundation audio devices").
//
// The index is NOT stable (it shifts when a device is added), so the id is
// the name and the index is resolved at every enumeration. Screen-capture
// entries ("Capture screen 0") are skipped: they are not webcams.
func ParseAVFoundation(out string) []Device {
	var devs []Device
	inVideo := false
	for _, line := range strings.Split(strings.ReplaceAll(out, "\r\n", "\n"), "\n") {
		lower := strings.ToLower(line)
		switch {
		case strings.Contains(lower, "avfoundation video devices"):
			inVideo = true
			continue
		case strings.Contains(lower, "avfoundation audio devices"):
			inVideo = false
			continue
		}
		if !inVideo {
			continue
		}
		m := avfDevice.FindStringSubmatch(line)
		if m == nil || strings.HasPrefix(strings.ToLower(m[2]), "capture screen") {
			continue
		}
		devs = append(devs, Device{
			ID:    "avf:" + m[2],
			Name:  m[2],
			Input: []string{"-f", "avfoundation", "-framerate", "30", "-i", m[1] + ":none"},
		})
	}
	return devs
}
