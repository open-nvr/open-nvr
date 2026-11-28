// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package siteinstall installs the site agent as a service, so it starts
// again after a reboot without anyone setting that up by hand.
//
// On Linux (systemd), as root:
//
//	/opt/opennvr-site/bin/opennvr-host           the agent (root-owned, read-only to the service)
//	/etc/opennvr-site/config.json                the site's credential (0600 root, handed to the
//	                                             service as a read-only systemd credential)
//	/etc/systemd/system/opennvr-site.service     runs it as a throwaway user (DynamicUser)
//	/var/lib/opennvr-site/                       the only place the service may write
//
// Windows has its own installer (plan S4); on macOS the steps are printed.
package siteinstall

import (
	_ "embed"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

//go:embed opennvr-site.service
var unitFile string

// Unit is the systemd unit installed (also host-agent/packaging/).
func Unit() string { return unitFile }

// Paths of an install, relative to Root ("/" for real, a temp dir in tests).
const (
	BinPath    = "opt/opennvr-site/bin/opennvr-host"
	ConfigPath = "etc/opennvr-site/config.json"
	UnitPath   = "etc/systemd/system/opennvr-site.service"
	UnitName   = "opennvr-site.service"
	// StatePaths: DynamicUser keeps the state under /var/lib/private and
	// links /var/lib/opennvr-site to it.
	StateLink = "var/lib/opennvr-site"
	StateDir  = "var/lib/private/opennvr-site"
)

// Installer does the work; System runs systemctl (replaced in tests).
type Installer struct {
	Root   string
	System func(args ...string) ([]byte, error)
	Out    io.Writer
}

// New is the real installer.
func New(out io.Writer) *Installer {
	return &Installer{Root: "/", Out: out, System: func(args ...string) ([]byte, error) {
		return exec.Command("systemctl", args...).CombinedOutput()
	}}
}

// ErrUnsupported: no service install on this system (yet).
var ErrUnsupported = errors.New("installing the agent as a service is done by `opennvr-host setup` on Linux (systemd) only")

// Supported: Linux with systemd, as root.
func Supported() error {
	if runtime.GOOS != "linux" {
		return ErrUnsupported
	}
	if os.Geteuid() != 0 {
		return errors.New("installing the service needs root: run it with sudo")
	}
	if _, err := exec.LookPath("systemctl"); err != nil {
		return errors.New("this machine has no systemd (systemctl): start `opennvr-host site` some other way")
	}
	return nil
}

func (in *Installer) path(rel string) string { return filepath.Join(in.Root, filepath.FromSlash(rel)) }

func (in *Installer) logf(format string, args ...any) {
	if in.Out != nil {
		fmt.Fprintf(in.Out, format+"\n", args...)
	}
}

// Install puts the binary at exe, the config (its bytes) and the unit in
// place, and (re)starts the service. Safe to run again: it replaces the
// binary, the credential and the unit, and restarts.
func (in *Installer) Install(exe string, config []byte) error {
	if err := in.copyBinary(exe); err != nil {
		return fmt.Errorf("installing the agent: %w", err)
	}
	if err := writeFile(in.path(ConfigPath), config, 0o600, 0o700); err != nil {
		return fmt.Errorf("saving the config: %w", err)
	}
	if err := writeFile(in.path(UnitPath), []byte(unitFile), 0o644, 0o755); err != nil {
		return fmt.Errorf("installing the service: %w", err)
	}
	for _, args := range [][]string{{"daemon-reload"}, {"enable", UnitName}, {"restart", UnitName}} {
		if out, err := in.System(args...); err != nil {
			return fmt.Errorf("systemctl %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(string(out)))
		}
	}
	in.logf("Installed as a service: it starts again after a reboot. Logs: journalctl -u opennvr-site -f")
	return nil
}

// Active: is the service running now?
func (in *Installer) Active() bool {
	out, err := in.System("is-active", UnitName)
	return err == nil && strings.TrimSpace(string(out)) == "active"
}

// Uninstall stops the service and removes everything Install put there,
// the site's credential and the agent's state included.
func (in *Installer) Uninstall() error {
	_, _ = in.System("disable", "--now", UnitName)
	var errs []error
	for _, rel := range []string{UnitPath, BinPath, ConfigPath, StateLink} {
		if err := os.Remove(in.path(rel)); err != nil && !errors.Is(err, os.ErrNotExist) {
			errs = append(errs, err)
		}
	}
	for _, rel := range []string{"opt/opennvr-site", "etc/opennvr-site", StateDir} {
		if err := os.RemoveAll(in.path(rel)); err != nil {
			errs = append(errs, err)
		}
	}
	_, _ = in.System("daemon-reload")
	if len(errs) > 0 {
		return errors.Join(errs...)
	}
	in.logf("The OpenNVR site agent is removed from this machine. Revoke the site on the server too, if it is not going to be used again.")
	return nil
}

// copyBinary copies the running executable into place, atomically, unless
// it already is the installed one.
func (in *Installer) copyBinary(exe string) error {
	dst := in.path(BinPath)
	if same, _ := sameFile(exe, dst); same {
		return nil
	}
	src, err := os.Open(exe)
	if err != nil {
		return err
	}
	defer src.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	tmp := dst + ".new"
	f, err := os.OpenFile(tmp, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	if _, err := io.Copy(f, src); err != nil {
		f.Close()
		os.Remove(tmp)
		return err
	}
	if err := f.Close(); err != nil {
		os.Remove(tmp)
		return err
	}
	if err := os.Chmod(tmp, 0o755); err != nil {
		return err
	}
	return os.Rename(tmp, dst)
}

func sameFile(a, b string) (bool, error) {
	sa, err := os.Stat(a)
	if err != nil {
		return false, err
	}
	sb, err := os.Stat(b)
	if err != nil {
		return false, err
	}
	return os.SameFile(sa, sb), nil
}

func writeFile(path string, data []byte, mode, dirMode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), dirMode); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, mode); err != nil {
		return err
	}
	if err := os.Chmod(tmp, mode); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// ConfigFile is where an installed agent's config lives (to load and
// save it), under Root.
func (in *Installer) ConfigFile() string { return in.path(ConfigPath) }

// OtherAgents lists other `opennvr-host site` processes on this machine
// (Linux, /proc): an agent started by hand before the service would relay
// every camera a second time with an old credential.
func OtherAgents(self int) []string {
	if runtime.GOOS != "linux" {
		return nil
	}
	dirs, _ := os.ReadDir("/proc")
	var out []string
	for _, d := range dirs {
		pid := d.Name()
		if pid == fmt.Sprint(self) || strings.Trim(pid, "0123456789") != "" {
			continue
		}
		b, err := os.ReadFile("/proc/" + pid + "/cmdline")
		if err != nil {
			continue
		}
		args := strings.Split(strings.TrimRight(string(b), "\x00"), "\x00")
		// The installed service itself is not "another" agent.
		if len(args) >= 2 && filepath.Base(args[0]) == "opennvr-host" && args[1] == "site" && args[0] != "/"+BinPath {
			out = append(out, pid)
		}
	}
	return out
}
