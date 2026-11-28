// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package main

import (
	"fmt"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/site"
	"github.com/open-nvr/open-nvr/host-agent/internal/siteinstall"
	"github.com/open-nvr/open-nvr/host-agent/internal/update"
)

// platform is this machine's name in the server's downloads ("linux-arm64").
func platform() string { return runtime.GOOS + "-" + runtime.GOARCH }

func self() update.Self {
	return update.Self{Version: buildinfo.Version(), Source: buildinfo.Source, BuiltAt: buildinfo.BuiltAtUnix()}
}

// siteUpdates is the agent's update check, for the Linux service only: it
// stages into its state directory, which the root updater watches.
func siteUpdates(statePath string) *update.Stager {
	if runtime.GOOS != "linux" || statePath == "" {
		return nil
	}
	return &update.Stager{
		StateDir:   filepath.Dir(statePath),
		Keys:       update.TrustedKeys(),
		Self:       self(),
		Platform:   platform(),
		FailedFile: "/" + siteinstall.UpdateFailed,
	}
}

// applyUpdateCmd: `opennvr-host apply-update`, run as root by
// opennvr-site-update.service when the agent has staged a signed update.
func applyUpdateCmd(args []string) int {
	if len(args) != 0 {
		fmt.Fprintln(os.Stderr, "usage: opennvr-host apply-update   (run by opennvr-site-update.service)")
		return 2
	}
	if err := siteinstall.Supported(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	a := &update.Applier{
		StagingDir: "/" + siteinstall.StateDir + "/" + update.StagingDir,
		BinPath:    "/" + siteinstall.BinPath,
		FailedFile: "/" + siteinstall.UpdateFailed,
		LastSync:   "/" + siteinstall.StateDir + "/" + site.LastSyncFile,
		Keys:       update.TrustedKeys(),
		Self:       self(),
		Platform:   platform(),
		Restart: func() error {
			out, err := exec.Command("systemctl", "restart", siteinstall.UnitName).CombinedOutput()
			if err != nil {
				return fmt.Errorf("%v: %s", err, strings.TrimSpace(string(out)))
			}
			return nil
		},
		Healthy: 2 * time.Minute,
		Logf:    log.Printf,
	}
	if err := a.Apply(); err != nil {
		log.Printf("agent update: %v", err)
		return 1
	}
	return 0
}
