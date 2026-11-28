// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package update

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func keypair(t *testing.T) (ed25519.PrivateKey, Keys) {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	k, err := ParseKeys(base64.StdEncoding.EncodeToString(pub))
	if err != nil {
		t.Fatal(err)
	}
	return priv, k
}

var self = Self{Version: "0.1.12", Source: "aaaa", BuiltAt: 1000}

func signed(t *testing.T, priv ed25519.PrivateKey, m Manifest) ([]byte, []byte) {
	t.Helper()
	data, _ := json.Marshal(m)
	sig, err := Sign(data, priv)
	if err != nil {
		t.Fatal(err)
	}
	return data, sig
}

func sumOf(b []byte) string { s := sha256.Sum256(b); return hex.EncodeToString(s[:]) }

func newer(bin []byte) Manifest {
	return Manifest{Format: Format, Build: "0.1.13+bbb", Version: "0.1.13", Source: "bbbb", BuiltAt: 2000,
		Files: map[string]File{"linux-amd64/opennvr-host": {SHA256: sumOf(bin), Size: int64(len(bin))}}}
}

func TestTheReleaseKeyIsTheOnlyWayIn(t *testing.T) {
	priv, keys := keypair(t)
	other, _ := keypair(t)
	data, sig := signed(t, priv, newer([]byte("v2")))
	if _, err := Verify(data, sig, keys); err != nil {
		t.Fatalf("valid: %v", err)
	}
	if _, err := Verify(data, sig, Keys{}); !errors.Is(err, ErrNoKeys) {
		t.Fatalf("no keys: %v", err)
	}
	_, osig := signed(t, other, newer([]byte("v2")))
	if _, err := Verify(data, osig, keys); !errors.Is(err, ErrUnknownKey) {
		t.Fatalf("another key: %v", err)
	}
	tampered := []byte(strings.Replace(string(data), "0.1.13", "9.9.99", 1))
	if _, err := Verify(tampered, sig, keys); !errors.Is(err, ErrBadSig) {
		t.Fatalf("tampered: %v", err)
	}
}

func TestOnlyANewerDifferentBuildIsAcceptable(t *testing.T) {
	m := newer(nil)
	if err := Acceptable(&m, self); err != nil {
		t.Fatalf("newer: %v", err)
	}
	same := m
	same.Source = self.Source
	if err := Acceptable(&same, self); !errors.Is(err, ErrSame) {
		t.Fatalf("same sources: %v", err)
	}
	for name, mod := range map[string]func(*Manifest){
		"built earlier":     func(x *Manifest) { x.BuiltAt = 999 },
		"lower version":     func(x *Manifest) { x.Version = "0.1.11" },
		"below its floor":   func(x *Manifest) { x.MinVersion = "0.2.0" },
		"built at the same": func(x *Manifest) { x.BuiltAt = self.BuiltAt },
	} {
		x := newer(nil)
		mod(&x)
		if err := Acceptable(&x, self); err == nil || errors.Is(err, ErrSame) {
			t.Errorf("%s: accepted (%v)", name, err)
		}
	}
	if err := Acceptable(&m, Self{Version: "0.1.12"}); err == nil {
		t.Fatal("a development build updated itself")
	}
}

func TestCompareVersions(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want int
	}{{"0.1.12", "0.1.12", 0}, {"0.1.13", "0.1.12", 1}, {"0.1.9", "0.1.12", -1}, {"0.2", "0.1.99", 1}, {"v1.0.0", "1.0", 0}, {"0.1.12+abc", "0.1.12", 0}} {
		if got := CompareVersions(c.a, c.b); got != c.want {
			t.Errorf("%s vs %s: %d", c.a, c.b, got)
		}
	}
}

type fakeFetch struct {
	data, sig, bin []byte
	downloads      int
}

func (f *fakeFetch) UpdateManifest(context.Context) ([]byte, []byte, error) {
	if f.data == nil {
		return nil, nil, ErrNotFound
	}
	return f.data, f.sig, nil
}

func (f *fakeFetch) DownloadAgent(context.Context, string) ([]byte, error) {
	f.downloads++
	return f.bin, nil
}

func stager(t *testing.T, keys Keys, f *fakeFetch) *Stager {
	return &Stager{StateDir: t.TempDir(), Keys: keys, Self: self, Platform: "linux-amd64", Fetch: f,
		FailedFile: filepath.Join(t.TempDir(), "failed")}
}

func TestTheAgentStagesASignedNewerBuildOnce(t *testing.T) {
	priv, keys := keypair(t)
	bin := []byte("agent v2")
	data, sig := signed(t, priv, newer(bin))
	f := &fakeFetch{data: data, sig: sig, bin: bin}
	s := stager(t, keys, f)
	got, err := s.Check(context.Background())
	if err != nil || got != "0.1.13+bbb" {
		t.Fatalf("staged %q %v", got, err)
	}
	dir := filepath.Join(s.StateDir, StagingDir)
	if b, _ := os.ReadFile(filepath.Join(dir, StagedBinary)); string(b) != "agent v2" {
		t.Fatalf("staged binary %q", b)
	}
	if _, err := os.Stat(filepath.Join(dir, Ready)); err != nil {
		t.Fatal("not marked ready")
	}
	if again, _ := s.Check(context.Background()); again != "" || f.downloads != 1 {
		t.Fatalf("staged again (%q), downloads %d", again, f.downloads)
	}
}

func TestTheAgentStagesNothingItCannotTrust(t *testing.T) {
	priv, keys := keypair(t)
	bin := []byte("agent v2")
	data, sig := signed(t, priv, newer(bin))
	cases := map[string]*fakeFetch{
		"another file":   {data: data, sig: sig, bin: []byte("evil")},
		"a bad sig":      {data: data, sig: []byte(`{"key_id":"x","sig":"AAAA"}`), bin: bin},
		"nothing signed": {},
	}
	for name, f := range cases {
		s := stager(t, keys, f)
		got, _ := s.Check(context.Background())
		if got != "" {
			t.Errorf("%s: staged %q", name, got)
		}
		if _, err := os.Stat(filepath.Join(s.StateDir, StagingDir, Ready)); err == nil {
			t.Errorf("%s: marked ready", name)
		}
	}
	// A build the updater rolled back is not staged again.
	s := stager(t, keys, &fakeFetch{data: data, sig: sig, bin: bin})
	_ = os.WriteFile(s.FailedFile, []byte("bbbb\n"), 0o644)
	if got, _ := s.Check(context.Background()); got != "" {
		t.Fatalf("a rolled-back build was staged again")
	}
}

// ── the root updater ───────────────────────────────────────────────────

type fakeClock struct{ t time.Time }

func (c *fakeClock) now() time.Time        { return c.t }
func (c *fakeClock) sleep(d time.Duration) { c.t = c.t.Add(d) }

func applier(t *testing.T, keys Keys, staging string) (*Applier, *fakeClock, *int) {
	dir := t.TempDir()
	bin := filepath.Join(dir, "bin", "opennvr-host")
	_ = os.MkdirAll(filepath.Dir(bin), 0o755)
	_ = os.WriteFile(bin, []byte("agent v1"), 0o755)
	clock := &fakeClock{t: time.Unix(5000, 0)}
	restarts := 0
	a := &Applier{StagingDir: staging, BinPath: bin, FailedFile: filepath.Join(dir, "failed"),
		LastSync: filepath.Join(dir, "last_sync"), Keys: keys, Self: self, Platform: "linux-amd64",
		Healthy: 2 * time.Minute, now: clock.now, sleep: clock.sleep}
	a.Restart = func() error { restarts++; return nil }
	return a, clock, &restarts
}

func stageFor(t *testing.T, priv ed25519.PrivateKey, keys Keys, bin []byte) string {
	data, sig := signed(t, priv, newer(bin))
	s := stager(t, keys, &fakeFetch{data: data, sig: sig, bin: bin})
	if got, err := s.Check(context.Background()); err != nil || got == "" {
		t.Fatalf("stage: %q %v", got, err)
	}
	return filepath.Join(s.StateDir, StagingDir)
}

func TestTheUpdaterInstallsAGoodBuildAndKeepsThePrevious(t *testing.T) {
	priv, keys := keypair(t)
	staging := stageFor(t, priv, keys, []byte("agent v2"))
	a, clock, restarts := applier(t, keys, staging)
	// The new agent reaches the server: it touches last_sync after the restart.
	a.Restart = func() error {
		*restarts++
		_ = os.WriteFile(a.LastSync, nil, 0o600)
		_ = os.Chtimes(a.LastSync, clock.t.Add(time.Second), clock.t.Add(time.Second))
		return nil
	}
	if err := a.Apply(); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(a.BinPath); string(b) != "agent v2" {
		t.Fatalf("installed %q", b)
	}
	if b, _ := os.ReadFile(a.BinPath + ".prev"); string(b) != "agent v1" {
		t.Fatalf("previous %q", b)
	}
	if _, err := os.Stat(staging); !os.IsNotExist(err) || *restarts != 1 {
		t.Fatalf("staging left (%v), restarts %d", err, *restarts)
	}
}

func TestTheUpdaterPutsTheOldAgentBackWhenTheNewOneDoesNotConnect(t *testing.T) {
	priv, keys := keypair(t)
	staging := stageFor(t, priv, keys, []byte("agent v2"))
	a, _, restarts := applier(t, keys, staging)
	err := a.Apply()
	if err == nil || !strings.Contains(err.Error(), "previous agent is back") {
		t.Fatalf("got %v", err)
	}
	if b, _ := os.ReadFile(a.BinPath); string(b) != "agent v1" {
		t.Fatalf("installed %q after a failed update", b)
	}
	if b, _ := os.ReadFile(a.FailedFile); strings.TrimSpace(string(b)) != "bbbb" {
		t.Fatalf("failed marker %q", b)
	}
	if *restarts != 2 {
		t.Fatalf("restarts %d", *restarts)
	}
}

func TestTheUpdaterTrustsNothingTheAgentStaged(t *testing.T) {
	priv, keys := keypair(t)
	_, otherKeys := keypair(t)
	// The installed agent trusts other keys than the ones the stage was
	// signed with: refused.
	staging := stageFor(t, priv, keys, []byte("agent v2"))
	a, _, restarts := applier(t, otherKeys, staging)
	if err := a.Apply(); err == nil {
		t.Fatal("applied a build the installed agent's keys do not sign")
	}
	// The binary swapped after staging: refused.
	staging = stageFor(t, priv, keys, []byte("agent v2"))
	_ = os.WriteFile(filepath.Join(staging, StagedBinary), []byte("evil"), 0o600)
	a, _, restarts = applier(t, keys, staging)
	if err := a.Apply(); err == nil || *restarts != 0 {
		t.Fatalf("applied a swapped binary (%v)", err)
	}
	if b, _ := os.ReadFile(a.BinPath); string(b) != "agent v1" {
		t.Fatalf("installed %q", b)
	}
	if _, err := os.Stat(staging); !os.IsNotExist(err) {
		t.Fatal("a refused stage was left (the path unit would loop)")
	}
	// A link planted in the staging directory is not followed.
	if runtime.GOOS != "windows" {
		staging = stageFor(t, priv, keys, []byte("agent v2"))
		target := filepath.Join(t.TempDir(), "secret")
		_ = os.WriteFile(target, []byte("agent v2"), 0o600)
		_ = os.Remove(filepath.Join(staging, StagedBinary))
		if err := os.Symlink(target, filepath.Join(staging, StagedBinary)); err != nil {
			t.Fatal(err)
		}
		a, _, _ = applier(t, keys, staging)
		if err := a.Apply(); err == nil || !strings.Contains(err.Error(), "not a regular file") {
			t.Fatalf("followed a link: %v", err)
		}
	}
	// Nothing staged: nothing done.
	a, _, restarts = applier(t, keys, filepath.Join(t.TempDir(), "staging"))
	if err := a.Apply(); err != nil || *restarts != 0 {
		t.Fatalf("nothing staged: %v, restarts %d", err, *restarts)
	}
}

func TestTheCompiledKeysParse(t *testing.T) {
	_ = TrustedKeys() // panics on a malformed key in keys.go
}
