// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

//go:build !windows

package site

import "golang.org/x/sys/unix"

// diskFreeMB: free space for unprivileged use on dir's filesystem.
func diskFreeMB(dir string) (float64, bool) {
	var st unix.Statfs_t
	if err := unix.Statfs(dir, &st); err != nil {
		return 0, false
	}
	return float64(st.Bavail) * float64(st.Bsize) / (1 << 20), true
}
