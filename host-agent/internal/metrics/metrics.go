// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package metrics measures the machine itself. Inside Docker Desktop, core can
// only see Docker's Linux VM (its RAM, its CPUs); this is the host's view:
// every disk, the real memory, temperatures where the OS exposes them.
package metrics

import (
	"runtime"
	"sort"
	"strings"

	"github.com/shirou/gopsutil/v4/cpu"
	"github.com/shirou/gopsutil/v4/disk"
	"github.com/shirou/gopsutil/v4/load"
	"github.com/shirou/gopsutil/v4/mem"
	"github.com/shirou/gopsutil/v4/sensors"

	"github.com/open-nvr/open-nvr/host-agent/internal/core"
)

// Pseudo and container filesystems are not disks anyone runs out of.
var skipFS = map[string]bool{
	"tmpfs": true, "devtmpfs": true, "overlay": true, "squashfs": true, "proc": true,
	"sysfs": true, "cgroup": true, "cgroup2": true, "devfs": true, "autofs": true,
	"nsfs": true, "tracefs": true, "debugfs": true, "fuse.lxcfs": true, "ramfs": true,
	"efivarfs": true, "securityfs": true, "pstore": true, "bpf": true, "configfs": true,
}

// Collect samples the host. CPU is the average since the previous call (the
// first call reports nothing for it). recordingsDir is the host folder the
// recordings are written to; "" skips it.
func Collect(recordingsDir string) *core.Metrics {
	m := &core.Metrics{Disks: []core.Disk{}, Temperatures: []core.Temperature{}}
	if pct, err := cpu.Percent(0, false); err == nil && len(pct) == 1 {
		v := pct[0]
		m.CPUPercent = &v
	}
	if n, err := cpu.Counts(true); err == nil {
		m.CPUCount = n
	}
	if vm, err := mem.VirtualMemory(); err == nil {
		m.MemoryTotalBytes = vm.Total
		// Total minus available, as core's psutil reading does.
		m.MemoryUsedBytes = vm.Total - vm.Available
	}
	if runtime.GOOS != "windows" {
		if l, err := load.Avg(); err == nil {
			m.LoadAvg = []float64{l.Load1, l.Load5, l.Load15}
		}
	}
	if parts, err := disk.Partitions(false); err == nil {
		seen := map[string]bool{}
		for _, p := range parts {
			if skipFS[strings.ToLower(p.Fstype)] || seen[p.Mountpoint] {
				continue
			}
			if strings.HasPrefix(p.Mountpoint, "/snap/") || strings.HasPrefix(p.Mountpoint, "/boot/efi") {
				continue
			}
			u, err := disk.Usage(p.Mountpoint)
			if err != nil || u.Total == 0 {
				continue
			}
			seen[p.Mountpoint] = true
			m.Disks = append(m.Disks, core.Disk{Mount: p.Mountpoint, TotalBytes: u.Total, FreeBytes: u.Free})
			if len(m.Disks) >= 32 {
				break
			}
		}
	}
	if recordingsDir != "" {
		if u, err := disk.Usage(recordingsDir); err == nil && u.Total > 0 {
			m.RecordingsDisk = &core.Disk{Mount: recordingsDir, TotalBytes: u.Total, FreeBytes: u.Free}
		}
	}
	// Temperatures: Linux (hwmon) and macOS work as a normal user; Windows
	// only exposes them to an administrator, so it usually reports none.
	if temps, _ := sensors.SensorsTemperatures(); len(temps) > 0 {
		for _, t := range temps {
			if t.Temperature <= 0 || t.Temperature > 150 {
				continue
			}
			m.Temperatures = append(m.Temperatures, core.Temperature{Label: t.SensorKey, Celsius: t.Temperature})
		}
		sort.Slice(m.Temperatures, func(i, j int) bool { return m.Temperatures[i].Celsius > m.Temperatures[j].Celsius })
		if len(m.Temperatures) > 16 {
			m.Temperatures = m.Temperatures[:16]
		}
	}
	return m
}
