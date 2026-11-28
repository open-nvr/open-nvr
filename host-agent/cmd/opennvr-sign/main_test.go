// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"os"
	"path/filepath"
	"testing"

	"github.com/open-nvr/open-nvr/host-agent/internal/update"
)

func TestSignWritesAManifestTheAgentVerifies(t *testing.T) {
	out := t.TempDir()
	for rel, body := range map[string]string{
		"linux-amd64/opennvr-host":               "linux",
		"windows-amd64/opennvr-host.exe":         "gui",
		"windows-amd64/console/opennvr-host.exe": "console",
		"BUILD":                                  "0.1.13+abc\n", // not a build
	} {
		p := filepath.Join(out, filepath.FromSlash(rel))
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p, []byte(body), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	pub, priv, _ := ed25519.GenerateKey(rand.Reader)
	keyFile := filepath.Join(t.TempDir(), "key")
	_ = os.WriteFile(keyFile, []byte(base64.StdEncoding.EncodeToString(priv.Seed())+"\n"), 0o600)
	if rc := sign([]string{"-key", keyFile, "-dir", out, "-build", "0.1.13+abc", "-version", "0.1.13",
		"-source", "src1", "-built-at", "1700000000"}); rc != 0 {
		t.Fatalf("sign: %d", rc)
	}
	data, _ := os.ReadFile(filepath.Join(out, "manifest.json"))
	sig, _ := os.ReadFile(filepath.Join(out, "manifest.sig"))
	keys, _ := update.ParseKeys(base64.StdEncoding.EncodeToString(pub))
	m, err := update.Verify(data, sig, keys)
	if err != nil {
		t.Fatal(err)
	}
	if m.Build != "0.1.13+abc" || m.Source != "src1" || m.BuiltAt != 1700000000 || len(m.Files) != 3 {
		t.Fatalf("manifest %+v", m)
	}
	if _, ok := m.Files["windows-amd64/console/opennvr-host.exe"]; !ok {
		t.Fatalf("files %v", m.Files)
	}
}
