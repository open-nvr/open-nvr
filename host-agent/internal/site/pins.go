// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
)

// maxPins: the newest few keys the server proved, so a renewal that went
// back and forth still connects.
const maxPins = 4

// PinSet is the server keys an enrolled agent trusts. After enrolment the
// agent trusts ONLY these, not the machine's certificate store: a firewall
// that inspects HTTPS with its own CA (which the machine trusts) would
// otherwise read the site's credential and every camera password.
//
// When the server's key changes (a renewed certificate with a new key), the
// agent learns the new one only after the server proves it holds the site's
// credential hash (Client.repin). An agent enrolled before pins existed
// (an empty set) learns the first key that verifies by the normal chain,
// once, and is pinned from then on.
type PinSet struct {
	mu    sync.Mutex
	pins  []string
	onAdd func([]string)
	// roots: tests only (nil = the machine's store, for the one-time
	// learning of an empty set).
	roots *x509.CertPool
}

// NewPinSet; onAdd is called (outside the lock) with the new list whenever a
// key is learned, to save it.
func NewPinSet(pins []string, onAdd func([]string)) *PinSet {
	s := &PinSet{onAdd: onAdd}
	for _, p := range pins {
		s.addLocked(strings.TrimSpace(p))
	}
	return s
}

func (s *PinSet) addLocked(p string) bool {
	if !strings.HasPrefix(p, "sha256/") {
		return false
	}
	for _, have := range s.pins {
		if have == p {
			return false
		}
	}
	s.pins = append(s.pins, p)
	if len(s.pins) > maxPins {
		s.pins = s.pins[len(s.pins)-maxPins:]
	}
	return true
}

// Add trusts another key (newest last).
func (s *PinSet) Add(p string) {
	s.mu.Lock()
	added := s.addLocked(p)
	list := append([]string(nil), s.pins...)
	s.mu.Unlock()
	if added && s.onAdd != nil {
		s.onAdd(list)
	}
}

// Has: is this key trusted?
func (s *PinSet) Has(p string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, have := range s.pins {
		if have == p {
			return true
		}
	}
	return false
}

// List is a copy of the trusted keys.
func (s *PinSet) List() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]string(nil), s.pins...)
}

func (s *PinSet) empty() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.pins) == 0
}

// errNotPinned: the server presented a key this agent does not trust.
var errNotPinned = errors.New("the server's key is not the one this agent was enrolled with")

// PinnedTLSConfig trusts the server only by its key (PinSet). serverName
// matters only for the one-time learning of an empty set.
func PinnedTLSConfig(serverName string, s *PinSet, alpn string) *tls.Config {
	cfg := &tls.Config{ServerName: serverName, MinVersion: tls.VersionTLS12}
	if alpn != "" {
		cfg.NextProtos = []string{alpn}
	}
	cfg.InsecureSkipVerify = true // verified below, by key
	cfg.VerifyConnection = func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) == 0 {
			return errors.New("the server sent no certificate")
		}
		leaf := cs.PeerCertificates[0]
		pin := Pin(leaf)
		if s.Has(pin) {
			return nil
		}
		if s.empty() {
			inter := x509.NewCertPool()
			for _, c := range cs.PeerCertificates[1:] {
				inter.AddCert(c)
			}
			if _, err := leaf.Verify(x509.VerifyOptions{DNSName: serverName, Intermediates: inter, Roots: s.roots}); err == nil {
				s.Add(pin)
				return nil
			}
		}
		return &ErrUntrusted{ServerPin: pin, Cause: errNotPinned, Pinned: true}
	}
	return cfg
}

// repinMessage is what the server's repin proof covers.
func repinMessage(nonce, pin string) []byte {
	return []byte("opennvr-site-repin\n" + nonce + "\n" + pin)
}

// RepinProof is HMAC-SHA256 keyed by the hex SHA-256 of the site's secret
// (what the server stores as the credential hash), over the nonce and the
// key the agent saw. Hex. Mirrors services/sites.py repin_proof.
func RepinProof(secret, nonce, pin string) string {
	sum := sha256.Sum256([]byte(secret))
	mac := hmac.New(sha256.New, []byte(hex.EncodeToString(sum[:])))
	mac.Write(repinMessage(nonce, pin))
	return hex.EncodeToString(mac.Sum(nil))
}

// tokenSecret splits "site_<id>_<secret>".
func tokenSecret(token string) (id string, secret string, ok bool) {
	rest, found := strings.CutPrefix(token, "site_")
	if !found {
		return "", "", false
	}
	id, secret, ok = strings.Cut(rest, "_")
	return id, secret, ok && id != "" && secret != ""
}
