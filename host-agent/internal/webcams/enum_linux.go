// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build linux

package webcams

import (
	"os"
	"path/filepath"
	"sort"
	"strings"
	"unsafe"

	"golang.org/x/sys/unix"
)

// struct v4l2_capability (linux/videodev2.h).
type v4l2Capability struct {
	Driver       [16]byte
	Card         [32]byte
	BusInfo      [32]byte
	Version      uint32
	Capabilities uint32
	DeviceCaps   uint32
	Reserved     [3]uint32
}

const (
	vidiocQuerycap    = 0x80685600 // _IOR('V', 0, struct v4l2_capability)
	capVideoCapture   = 0x00000001
	capVideoCaptureMp = 0x00001000
	capDeviceCaps     = 0x80000000
	capStreaming      = 0x04000000
	byIDDir           = "/dev/v4l/by-id"
	byPathDir         = "/dev/v4l/by-path"
	sysfsVideo4Linux  = "/sys/class/video4linux"
)

func cstr(b []byte) string {
	if i := strings.IndexByte(string(b), 0); i >= 0 {
		b = b[:i]
	}
	return strings.TrimSpace(string(b))
}

// queryCap asks the driver what a node is. A USB webcam exposes two nodes
// (capture and metadata); only the capture one can stream, and only the
// device_caps field tells them apart.
func queryCap(node string) (*v4l2Capability, error) {
	f, err := os.OpenFile(node, os.O_RDWR|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var c v4l2Capability
	if _, _, e := unix.Syscall(unix.SYS_IOCTL, f.Fd(), vidiocQuerycap, uintptr(unsafe.Pointer(&c))); e != 0 {
		return nil, e
	}
	return &c, nil
}

func isCapture(c *v4l2Capability) bool {
	caps := c.Capabilities
	if caps&capDeviceCaps != 0 {
		caps = c.DeviceCaps
	}
	return caps&(capVideoCapture|capVideoCaptureMp) != 0 && caps&capStreaming != 0
}

// stableLinks maps /dev/videoN to its udev by-id (or else by-path) name.
func stableLinks() map[string]string {
	out := map[string]string{}
	for _, dir := range []string{byPathDir, byIDDir} { // by-id wins: read last
		entries, _ := os.ReadDir(dir)
		for _, e := range entries {
			target, err := filepath.EvalSymlinks(filepath.Join(dir, e.Name()))
			if err == nil {
				out[target] = e.Name()
			}
		}
	}
	return out
}

// Enumerate lists the V4L2 capture nodes. ffmpeg is not needed to list them.
func Enumerate(string) ([]Device, error) {
	nodes, _ := filepath.Glob("/dev/video*")
	sort.Strings(nodes)
	links := stableLinks()
	var devs []Device
	for _, node := range nodes {
		c, err := queryCap(node)
		if err != nil || !isCapture(c) {
			continue
		}
		name := cstr(c.Card[:])
		if name == "" {
			if b, err := os.ReadFile(filepath.Join(sysfsVideo4Linux, filepath.Base(node), "name")); err == nil {
				name = strings.TrimSpace(string(b))
			}
		}
		id := links[node]
		if id == "" {
			// No udev link (a container, a minimal system): driver + bus is
			// stable for a webcam that stays in its port.
			id = cstr(c.Driver[:]) + "@" + cstr(c.BusInfo[:])
		}
		devs = append(devs, Device{
			ID:    "v4l2:" + id,
			Name:  name,
			Input: []string{"-f", "v4l2", "-i", node},
		})
	}
	return devs, nil
}
