// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package webcams

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fixture(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestParseDshowKeepsVideoAndUsesThePnpPath(t *testing.T) {
	devs := ParseDshow(fixture(t, "dshow-two-cams.txt"))
	if len(devs) != 2 {
		t.Fatalf("want the 2 webcams, not the microphone; got %+v", devs)
	}
	if devs[0].Name != "HP HD Camera" || devs[1].Name != "USB Camera" {
		t.Fatalf("names: %+v", devs)
	}
	if !strings.HasPrefix(devs[0].ID, `dshow:@device_pnp_\\?\usb#vid_30c9`) {
		t.Fatalf("id should be the PnP path, got %q", devs[0].ID)
	}
	in := strings.Join(devs[1].Input, " ")
	if !strings.Contains(in, "-f dshow") || !strings.Contains(in, `video=@device_pnp_\\?\usb#vid_0c45`) {
		t.Fatalf("input: %q", in)
	}
}

func TestParseDshowWithOneCameraUnplugged(t *testing.T) {
	devs := ParseDshow(fixture(t, "dshow-hp-laptop.txt"))
	if len(devs) != 1 || devs[0].Name != "HP HD Camera" {
		t.Fatalf("got %+v", devs)
	}
}

func TestParseDshowOldSectionStyle(t *testing.T) {
	devs := ParseDshow(fixture(t, "dshow-old-style.txt"))
	if len(devs) != 1 || devs[0].Name != "Integrated Webcam" {
		t.Fatalf("got %+v", devs)
	}
	if !strings.Contains(devs[0].ID, "vid_0bda") {
		t.Fatalf("id: %q", devs[0].ID)
	}
}

func TestParseAVFoundationSkipsScreensAndAudio(t *testing.T) {
	devs := ParseAVFoundation(fixture(t, "avfoundation-macbook.txt"))
	if len(devs) != 2 {
		t.Fatalf("got %+v", devs)
	}
	if devs[1].ID != "avf:Logitech BRIO" || strings.Join(devs[1].Input, " ") != "-f avfoundation -framerate 30 -i 1:none" {
		t.Fatalf("got %+v", devs[1])
	}
}

func TestResolveFallsBackToAUniqueName(t *testing.T) {
	present := []Device{{ID: "a2", Name: "USB Camera"}, {ID: "b", Name: "HP"}}
	// Same webcam, other USB port: new id, same name.
	if d, ok := Resolve("a1", "USB Camera", present); !ok || d.ID != "a2" {
		t.Fatalf("got %+v %v", d, ok)
	}
	// Two identical webcams: never guess.
	twins := append(present, Device{ID: "a3", Name: "USB Camera"})
	if _, ok := Resolve("a1", "USB Camera", twins); ok {
		t.Fatal("must not pick one of two same-named webcams")
	}
	if _, ok := Resolve("zz", "", present); ok {
		t.Fatal("no id, no name: missing")
	}
}
