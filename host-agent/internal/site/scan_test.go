// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bufio"
	"context"
	"encoding/base64"
	"net"
	"strings"
	"testing"
)

func TestScanTargetsAreTheMachinesOwnSubnetsAtMostASlash24(t *testing.T) {
	p := newFixedPolicy(mustNets("192.168.1.0/24", "10.20.0.0/16"), []string{"10.20.3.4"}, nil)
	ips, subnets, note := p.scanTargets()
	if strings.Join(subnets, ",") != "192.168.1.0/24,10.20.3.0/24" {
		t.Fatalf("subnets %v", subnets)
	}
	if len(ips) != 2*254 { // .1 to .254 of each
		t.Fatalf("%d targets", len(ips))
	}
	for _, ip := range ips {
		s := ip.String()
		if strings.HasSuffix(s, ".0") || strings.HasSuffix(s, ".255") {
			t.Fatalf("network or broadcast address %s", s)
		}
	}
	if note == "" {
		t.Fatal("no note that the /16 was cut to a /24")
	}
}

// fakeTCP answers each connection with reply (after reading one line).
func fakeTCP(t *testing.T, reply string) (net.IP, int) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				br := bufio.NewReader(c)
				for {
					line, err := br.ReadString('\n')
					if err != nil || line == "\r\n" {
						break
					}
				}
				_, _ = c.Write([]byte(reply))
			}()
		}
	}()
	a := ln.Addr().(*net.TCPAddr)
	return a.IP, a.Port
}

func TestAnRTSPAnswerIsACameraSignatureAndOtherServicesAreNot(t *testing.T) {
	ip, port := fakeTCP(t, "RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
	rtsp, open := probePort(context.Background(), ip, port, true)
	if !open || !rtsp {
		t.Fatalf("RTSP server: rtsp=%v open=%v", rtsp, open)
	}
	ip, port = fakeTCP(t, "HTTP/1.1 400 Bad Request\r\n\r\n")
	rtsp, open = probePort(context.Background(), ip, port, true)
	if !open || rtsp {
		t.Fatalf("HTTP server on an RTSP port: rtsp=%v open=%v", rtsp, open)
	}
}

func TestTheRTSPRequestGoesToAnAllowedDeviceAndItsAnswerComesBack(t *testing.T) {
	ip, port := fakeTCP(t, "RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\nWWW-Authenticate: Digest realm=\"cam\", nonce=\"n1\"\r\n\r\n")
	r := newRPCRunner(t.Logf)
	r.allowLoopback = true
	r.setAllowed([]string{ip.String()})
	req := base64.StdEncoding.EncodeToString([]byte("DESCRIBE rtsp://192.168.1.64:554/x RTSP/1.0\r\nCSeq: 1\r\n\r\n"))
	res := r.run(context.Background(), RPC{Kind: "rtsp", Target: net.JoinHostPort(ip.String(), itoaT(port)), BodyB64: req})
	body, _ := base64.StdEncoding.DecodeString(res.BodyB64)
	if res.Error != "" || !strings.Contains(string(body), "401 Unauthorized") || !strings.Contains(string(body), `nonce="n1"`) {
		t.Fatalf("result %+v body %q", res, body)
	}
	// Not an allowed device; not an RTSP request; a name instead of an address.
	for _, bad := range []RPC{
		{Kind: "rtsp", Target: "192.168.9.9:554", BodyB64: req},
		{Kind: "rtsp", Target: net.JoinHostPort(ip.String(), itoaT(port)), BodyB64: base64.StdEncoding.EncodeToString([]byte("GET / HTTP/1.1\r\n\r\n"))},
		{Kind: "rtsp", Target: "camera.local:554", BodyB64: req},
		{Kind: "telnet", Target: "192.168.1.64:23"},
	} {
		if res := r.run(context.Background(), bad); res.Error == "" {
			t.Errorf("%+v was sent", bad)
		}
	}
}
