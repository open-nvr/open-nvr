// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"crypto/tls"
	"crypto/x509"
	"net"
	"strconv"
	"testing"
)

func parseLeaf(c tls.Certificate) (*x509.Certificate, error) {
	return x509.ParseCertificate(c.Certificate[0])
}

func splitHostPort(t *testing.T, addr string) (string, int) {
	t.Helper()
	h, p, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatal(err)
	}
	n, _ := strconv.Atoi(p)
	return h, n
}

func itoaT(n int) string { return strconv.Itoa(n) }
