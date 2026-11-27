// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"errors"
	"fmt"
	"strings"
)

// IngestALPN is the ALPN the server's 443 stream layer routes to camera
// ingest (nginx/stream.conf.template).
const IngestALPN = "opennvr-rtsps"

// Pin is the SPKI pin of a certificate: "sha256/<base64>".
func Pin(cert *x509.Certificate) string {
	sum := sha256.Sum256(cert.RawSubjectPublicKeyInfo)
	return "sha256/" + base64.StdEncoding.EncodeToString(sum[:])
}

// ErrUntrusted: the server's certificate neither verifies nor matches a pin.
// Its own pin is in the message, so a first enrolment can show what to check.
type ErrUntrusted struct {
	ServerPin string
	Cause     error
}

func (e *ErrUntrusted) Error() string {
	return fmt.Sprintf("the server's certificate is not trusted (%v); its pin is %s", e.Cause, e.ServerPin)
}

// TLSConfig verifies the server as `serverName`:
//   - no pin: the normal chain + host name check;
//   - a pin: the chain check OR the leaf's key matching the pin (a
//     self-signed server, or a CA the site does not know);
//   - insecure: nothing (tests).
//
// alpn, when set, is offered and required back: a camera stream that ended
// up anywhere but the ingest route must not be sent.
func TLSConfig(serverName, pin string, insecure bool, alpn string) *tls.Config {
	cfg := &tls.Config{ServerName: serverName, MinVersion: tls.VersionTLS12}
	if alpn != "" {
		cfg.NextProtos = []string{alpn}
	}
	if insecure {
		cfg.InsecureSkipVerify = true
		return cfg
	}
	pin = strings.TrimSpace(pin)
	// Verification is done here in every case so the error can carry the
	// server's own pin.
	cfg.InsecureSkipVerify = true
	cfg.VerifyConnection = func(cs tls.ConnectionState) error {
		if len(cs.PeerCertificates) == 0 {
			return errors.New("the server sent no certificate")
		}
		leaf := cs.PeerCertificates[0]
		inter := x509.NewCertPool()
		for _, c := range cs.PeerCertificates[1:] {
			inter.AddCert(c)
		}
		_, chainErr := leaf.Verify(x509.VerifyOptions{DNSName: serverName, Intermediates: inter})
		if chainErr == nil {
			return nil
		}
		if pin != "" && Pin(leaf) == pin {
			return nil
		}
		return &ErrUntrusted{ServerPin: Pin(leaf), Cause: chainErr}
	}
	return cfg
}
