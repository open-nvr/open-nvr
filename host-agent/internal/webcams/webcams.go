// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package webcams finds the capture devices on this machine, and only those:
// the agent acts on a device it enumerated itself, never on a string core
// sent it. Enumerate is per OS (enum_*.go); the parsers are plain functions
// so they are tested against real ffmpeg output.
package webcams

// Device is one webcam.
type Device struct {
	// ID is as stable as the OS allows: the DirectShow device path on
	// Windows, the /dev/v4l/by-id name on Linux, the name on macOS.
	ID   string
	Name string
	// Input is ffmpeg's input arguments for this device, without -i's
	// framerate/size options (the publisher adds those).
	Input []string
}

// Resolve finds the device a camera asks for. The id first; when it is gone
// (Windows and Linux ids include the USB port, so moving a webcam to another
// port changes it), the same name — but only when exactly one present device
// has it, so two identical webcams are never mixed up. ok is false when the
// device is missing.
func Resolve(id string, name string, present []Device) (Device, bool) {
	for _, d := range present {
		if d.ID == id {
			return d, true
		}
	}
	if name == "" {
		return Device{}, false
	}
	var hit Device
	n := 0
	for _, d := range present {
		if d.Name == name {
			hit = d
			n++
		}
	}
	return hit, n == 1
}
