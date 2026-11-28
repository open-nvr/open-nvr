// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package update is how a site agent updates itself, and why a compromised
// OpenNVR server cannot use that to run its own code on the site's machine.
//
// Every agent build CI makes comes with a manifest (manifest.json: version,
// build time, source hash, the SHA-256 of each file) and a signature over its
// exact bytes (manifest.sig), made with an ed25519 key that only the release
// workflow holds. The public keys are compiled into the agent (keys.go). The
// server only relays the two files: it cannot make a signature. An agent
// installs a build only if:
//
//   - the signature verifies with a key compiled into it;
//   - the build is newer than itself (built later, a version not lower, and
//     not below the manifest's own floor): an old signed build with a known
//     flaw cannot be pushed back onto it;
//   - its agent sources differ from its own (a new core commit that did not
//     touch the agent is not an update);
//   - the downloaded file's SHA-256 is the one the manifest lists for this
//     machine's platform.
//
// The agent (an unprivileged service) only downloads and checks; a root
// updater (`opennvr-host apply-update`, its own systemd unit) checks again
// with the keys of the agent that is installed, swaps the binary, restarts
// the service and puts the old one back if the new one does not reach the
// server within two minutes.
package update

import (
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// Format is the manifest format this agent understands.
const Format = 1

// File is one agent build in the manifest.
type File struct {
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

// Manifest describes one set of agent builds.
type Manifest struct {
	Format  int    `json:"format"`
	Build   string `json:"build"`
	Version string `json:"version"`
	Source  string `json:"source"`
	BuiltAt int64  `json:"built_at"`
	// MinVersion: agents below it must update (and a build below it is
	// never taken); a floor the release process raises after a fix.
	MinVersion string `json:"min_version,omitempty"`
	// Files by path under the build folder, e.g. "linux-amd64/opennvr-host".
	Files map[string]File `json:"files"`
}

// Signature is manifest.sig.
type Signature struct {
	KeyID string `json:"key_id"`
	Sig   string `json:"sig"` // base64 ed25519 over the manifest's exact bytes
}

// KeyID names a public key: the first 16 hex characters of its SHA-256.
func KeyID(pub ed25519.PublicKey) string {
	sum := sha256.Sum256(pub)
	return hex.EncodeToString(sum[:8])
}

// Keys are public keys by id.
type Keys map[string]ed25519.PublicKey

// ParseKeys reads base64 public keys.
func ParseKeys(b64 ...string) (Keys, error) {
	out := Keys{}
	for _, s := range b64 {
		raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(s))
		if err != nil || len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("not an ed25519 public key: %q", s)
		}
		pub := ed25519.PublicKey(raw)
		out[KeyID(pub)] = pub
	}
	return out, nil
}

var (
	ErrNoKeys     = errors.New("this agent has no update keys: it does not update itself")
	ErrBadSig     = errors.New("the update's signature does not verify: refusing it")
	ErrUnknownKey = errors.New("the update is signed with a key this agent does not trust: refusing it")
)

// Verify checks sig over data with keys and parses the manifest.
func Verify(data, sigJSON []byte, keys Keys) (*Manifest, error) {
	if len(keys) == 0 {
		return nil, ErrNoKeys
	}
	var sig Signature
	if err := json.Unmarshal(sigJSON, &sig); err != nil {
		return nil, ErrBadSig
	}
	pub, ok := keys[sig.KeyID]
	if !ok {
		return nil, ErrUnknownKey
	}
	raw, err := base64.StdEncoding.DecodeString(sig.Sig)
	if err != nil || !ed25519.Verify(pub, data, raw) {
		return nil, ErrBadSig
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("the update's manifest: %w", err)
	}
	if m.Format != Format {
		return nil, fmt.Errorf("the update's manifest is format %d; this agent reads %d", m.Format, Format)
	}
	return &m, nil
}

// Self is the running (or installed) agent.
type Self struct {
	Version string
	Source  string
	BuiltAt int64
}

// ErrSame: the manifest's agent is this agent (same sources): nothing to do.
var ErrSame = errors.New("already this build")

// Acceptable says whether m may replace self; ErrSame when it is the same
// agent. A dev (unstamped) agent never updates itself.
func Acceptable(m *Manifest, self Self) error {
	if self.Source == "" || self.BuiltAt == 0 {
		return errors.New("a development build does not update itself")
	}
	if m.Source == self.Source {
		return ErrSame
	}
	if m.BuiltAt <= self.BuiltAt {
		return fmt.Errorf("the update (built %d) is not newer than this agent (built %d): refusing it", m.BuiltAt, self.BuiltAt)
	}
	if CompareVersions(m.Version, self.Version) < 0 {
		return fmt.Errorf("the update is version %s, older than this agent's %s: refusing it", m.Version, self.Version)
	}
	if m.MinVersion != "" && CompareVersions(m.Version, m.MinVersion) < 0 {
		return fmt.Errorf("the update is version %s, below its own floor %s: refusing it", m.Version, m.MinVersion)
	}
	return nil
}

// FileFor picks the manifest entry whose hash is sum among this platform's
// files ("linux-amd64/..."). The download route serves one of them.
func (m *Manifest) FileFor(platform, sum string) (string, bool) {
	for path, f := range m.Files {
		if strings.HasPrefix(path, platform+"/") && strings.EqualFold(f.SHA256, sum) {
			return path, true
		}
	}
	return "", false
}

// CompareVersions compares dotted numeric versions ("0.1.12"); a missing
// part counts as 0, anything after a '-' is ignored.
func CompareVersions(a, b string) int {
	pa, pb := versionParts(a), versionParts(b)
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var x, y int
		if i < len(pa) {
			x = pa[i]
		}
		if i < len(pb) {
			y = pb[i]
		}
		if x != y {
			if x < y {
				return -1
			}
			return 1
		}
	}
	return 0
}

func versionParts(v string) []int {
	v, _, _ = strings.Cut(strings.TrimPrefix(strings.TrimSpace(v), "v"), "-")
	v, _, _ = strings.Cut(v, "+")
	var out []int
	for _, p := range strings.Split(v, ".") {
		n, _ := strconv.Atoi(p)
		out = append(out, n)
	}
	return out
}

// Sign signs data with priv (CI's release key): manifest.sig's bytes.
func Sign(data []byte, priv ed25519.PrivateKey) ([]byte, error) {
	pub, ok := priv.Public().(ed25519.PublicKey)
	if !ok {
		return nil, errors.New("not an ed25519 key")
	}
	return json.MarshalIndent(Signature{KeyID: KeyID(pub), Sig: base64.StdEncoding.EncodeToString(ed25519.Sign(priv, data))}, "", "  ")
}

// ParsePrivateKey reads the signing key: the base64 of a 32-byte ed25519
// seed (what `opennvr-sign keygen` prints).
func ParsePrivateKey(b64 string) (ed25519.PrivateKey, error) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	if err != nil || len(raw) != ed25519.SeedSize {
		return nil, errors.New("the signing key is not the base64 of a 32-byte ed25519 seed")
	}
	return ed25519.NewKeyFromSeed(raw), nil
}
