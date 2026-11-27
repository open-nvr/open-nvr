// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package nethints

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

var site = []Iface{
	{Name: "lo", Up: true, Loopback: true, Addrs: []string{"127.0.0.1", "::1"}},
	{Name: "eth1", Up: true, Addrs: []string{"10.10.0.1"}},
	{Name: "eth0", Up: true, Addrs: []string{"192.168.2.30", "fe80::1"}},
	{Name: "wg0", Up: true, Addrs: []string{"10.8.0.1"}},
	{Name: "tailscale0", Up: true, Addrs: []string{"100.83.173.49"}},
	{Name: "docker0", Up: true, Addrs: []string{"172.17.0.1"}},
	{Name: "br-1a2b", Up: true, Addrs: []string{"172.28.0.1"}},
	{Name: "veth9", Up: true, Addrs: []string{"169.254.1.1"}},
	{Name: "eth2", Up: false, Addrs: []string{"192.168.50.2"}},
}

func TestBuildPutsTheEgressLANFirstAndVPNsLast(t *testing.T) {
	got := Build(site, "192.168.2.30", "")
	want := []string{"192.168.2.30", "10.10.0.1", "10.8.0.1", "100.83.173.49"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestBuildWithoutADefaultRouteKeepsInterfaceOrder(t *testing.T) {
	got := Build(site, "", "")
	want := []string{"10.10.0.1", "192.168.2.30", "10.8.0.1", "100.83.173.49"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestBuildOnADualNICHostIsTheBoundAddressAlone(t *testing.T) {
	if got := Build(site, "192.168.2.30", "192.168.2.30"); !reflect.DeepEqual(got, []string{"192.168.2.30"}) {
		t.Fatalf("got %v", got)
	}
	for _, wildcard := range []string{"", "0.0.0.0", "::"} {
		if got := Build(site, "", wildcard); len(got) < 2 {
			t.Fatalf("bind %q treated as dual-NIC: %v", wildcard, got)
		}
	}
}

func TestBuildTreatsACGNATAddressAsAVPN(t *testing.T) {
	ifs := []Iface{
		{Name: "ens3", Up: true, Addrs: []string{"100.100.5.5"}},
		{Name: "ens4", Up: true, Addrs: []string{"192.168.1.4"}},
	}
	got := Build(ifs, "100.100.5.5", "")
	if !reflect.DeepEqual(got, []string{"192.168.1.4", "100.100.5.5"}) {
		t.Fatalf("got %v", got)
	}
}

func TestWriteOnlyWhenChangedAndNeverEmpty(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, FileName)

	if changed, err := Write(dir, []string{"192.168.2.30", "10.8.0.1"}); err != nil || !changed {
		t.Fatalf("first write: changed=%v err=%v", changed, err)
	}
	if b, _ := os.ReadFile(path); string(b) != "192.168.2.30 10.8.0.1\n" {
		t.Fatalf("content %q", b)
	}
	if changed, _ := Write(dir, []string{"192.168.2.30", "10.8.0.1"}); changed {
		t.Fatal("unchanged list rewritten")
	}
	if changed, _ := Write(dir, nil); changed {
		t.Fatal("an empty list replaced the file")
	}
	if b, _ := os.ReadFile(path); string(b) != "192.168.2.30 10.8.0.1\n" {
		t.Fatalf("empty write touched the file: %q", b)
	}
	if _, err := os.Stat(path + ".tmp"); !os.IsNotExist(err) {
		t.Fatal("tmp file left behind")
	}
}
