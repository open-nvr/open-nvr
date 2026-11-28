// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package siteinstall

import (
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

type fakeSystemd struct {
	calls  [][]string
	failOn string
}

func (f *fakeSystemd) run(args ...string) ([]byte, error) {
	f.calls = append(f.calls, args)
	if f.failOn != "" && args[0] == f.failOn {
		return []byte("Failed to " + f.failOn), errors.New("exit status 1")
	}
	if args[0] == "is-active" {
		return []byte("active\n"), nil
	}
	return nil, nil
}

func newTest(t *testing.T) (*Installer, *fakeSystemd, string) {
	t.Helper()
	root := t.TempDir()
	sd := &fakeSystemd{}
	exe := filepath.Join(t.TempDir(), "opennvr-host")
	if err := os.WriteFile(exe, []byte("#!agent v1"), 0o755); err != nil {
		t.Fatal(err)
	}
	return &Installer{Root: root, System: sd.run}, sd, exe
}

func TestInstallPutsEverythingInPlaceAndStartsTheService(t *testing.T) {
	in, sd, exe := newTest(t)
	if err := in.Install(exe, []byte(`{"token":"site_1_x"}`)); err != nil {
		t.Fatal(err)
	}
	bin, _ := os.ReadFile(in.path(BinPath))
	cfg, _ := os.ReadFile(in.path(ConfigPath))
	unit, _ := os.ReadFile(in.path(UnitPath))
	if string(bin) != "#!agent v1" || string(cfg) != `{"token":"site_1_x"}` || string(unit) != Unit() {
		t.Fatalf("bin %q cfg %q unit %d bytes", bin, cfg, len(unit))
	}
	if runtime.GOOS != "windows" {
		for path, want := range map[string]os.FileMode{BinPath: 0o755, ConfigPath: 0o600, UnitPath: 0o644, "etc/opennvr-site": 0o700} {
			st, err := os.Stat(in.path(path))
			if err != nil || st.Mode().Perm() != want {
				t.Errorf("%s: mode %v, want %v (%v)", path, st.Mode().Perm(), want, err)
			}
		}
	}
	got := []string{}
	for _, c := range sd.calls {
		got = append(got, strings.Join(c, " "))
	}
	if strings.Join(got, "; ") != "daemon-reload; enable opennvr-site.service; restart opennvr-site.service" {
		t.Fatalf("systemctl calls %q", got)
	}
	if !in.Active() {
		t.Fatal("not active")
	}
}

func TestInstallingAgainReplacesTheAgentAndTheCredential(t *testing.T) {
	in, _, exe := newTest(t)
	if err := in.Install(exe, []byte("old")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(exe, []byte("#!agent v2"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := in.Install(exe, []byte("new")); err != nil {
		t.Fatal(err)
	}
	bin, _ := os.ReadFile(in.path(BinPath))
	cfg, _ := os.ReadFile(in.path(ConfigPath))
	if string(bin) != "#!agent v2" || string(cfg) != "new" {
		t.Fatalf("bin %q cfg %q", bin, cfg)
	}
	// Running the installed binary itself again copies nothing over itself.
	if err := in.Install(in.path(BinPath), []byte("again")); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(in.path(BinPath) + ".new"); !os.IsNotExist(err) {
		t.Fatal("a temp file was left")
	}
}

func TestAFailingSystemctlIsReported(t *testing.T) {
	in, sd, exe := newTest(t)
	sd.failOn = "restart"
	err := in.Install(exe, []byte("x"))
	if err == nil || !strings.Contains(err.Error(), "systemctl restart") || !strings.Contains(err.Error(), "Failed to restart") {
		t.Fatalf("got %v", err)
	}
}

func TestUninstallLeavesNothingBehind(t *testing.T) {
	in, sd, exe := newTest(t)
	if err := in.Install(exe, []byte("x")); err != nil {
		t.Fatal(err)
	}
	state := in.path(StateDir)
	if err := os.MkdirAll(state, 0o700); err != nil {
		t.Fatal(err)
	}
	_ = os.WriteFile(filepath.Join(state, "state.json"), []byte("{}"), 0o600)
	sd.calls = nil
	if err := in.Uninstall(); err != nil {
		t.Fatal(err)
	}
	for _, p := range []string{BinPath, ConfigPath, UnitPath, "opt/opennvr-site", "etc/opennvr-site", StateDir} {
		if _, err := os.Stat(in.path(p)); !os.IsNotExist(err) {
			t.Errorf("%s still there", p)
		}
	}
	if strings.Join(sd.calls[0], " ") != "disable --now opennvr-site.service" {
		t.Fatalf("calls %q", sd.calls)
	}
	// Twice is fine (nothing to remove).
	if err := in.Uninstall(); err != nil {
		t.Fatal(err)
	}
}

func TestThePackagedUnitIsTheInstalledOne(t *testing.T) {
	b, err := os.ReadFile(filepath.Join("..", "..", "packaging", "opennvr-site.service"))
	if err != nil {
		t.Fatal(err)
	}
	norm := func(s string) string { return strings.ReplaceAll(s, "\r\n", "\n") }
	if norm(string(b)) != norm(Unit()) {
		t.Fatal("host-agent/packaging/opennvr-site.service differs from internal/siteinstall/opennvr-site.service")
	}
}

func TestTheUnitStaysLeastPrivilegedAndCanListNetworks(t *testing.T) {
	u := Unit()
	for _, want := range []string{
		"DynamicUser=yes", "NoNewPrivileges=yes", "ProtectSystem=strict", "StateDirectory=opennvr-site",
		"LoadCredential=config.json:/etc/opennvr-site/config.json",
		"ExecStart=/opt/opennvr-site/bin/opennvr-host site -config ${CREDENTIALS_DIRECTORY}/config.json",
		// Go lists this machine's networks over netlink; without it the
		// agent's target policy sees no camera network and relays nothing.
		"RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK",
	} {
		if !strings.Contains(u, want) {
			t.Errorf("unit lacks %q", want)
		}
	}
}
