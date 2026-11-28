// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"
)

// ── target policy: the agent's own rule, whatever the server says ──────

func testPolicy() *TargetPolicy {
	return newFixedPolicy(mustNets("192.168.1.0/24", "10.20.0.0/16"), []string{"192.168.1.10"}, []string{"192.168.1.1"})
}

func TestPolicyAllowsACameraOnTheSitesOwnNetwork(t *testing.T) {
	p := testPolicy()
	for _, c := range []struct {
		ip   string
		port int
	}{{"192.168.1.64", 554}, {"192.168.1.64", 80}, {"10.20.3.4", 8000}, {"192.168.1.65", 37777}} {
		if err := p.Check(net.ParseIP(c.ip), c.port); err != nil {
			t.Errorf("%s:%d refused: %v", c.ip, c.port, err)
		}
	}
}

func TestPolicyRefusesWhatACompromisedServerWouldAimAt(t *testing.T) {
	p := testPolicy()
	for _, c := range []struct {
		ip, why string
		port    int
	}{
		{"192.168.1.1", "router", 80},
		{"192.168.1.10", "own address", 80},
		{"8.8.8.8", "camera network", 80},           // the internet
		{"192.168.7.20", "camera network", 554},     // another private network
		{"172.16.0.5", "camera network", 554},       // private but not connected
		{"192.168.1.64", "not a camera port", 22},   // SSH on a camera
		{"192.168.1.64", "not a camera port", 3389}, // RDP
		{"127.0.0.1", "loopback", 554},
		{"169.254.169.254", "link-local", 80}, // cloud metadata
		{"224.0.0.1", "multicast", 554},
	} {
		err := p.Check(net.ParseIP(c.ip), c.port)
		if err == nil || !strings.Contains(err.Error(), c.why) {
			t.Errorf("%s:%d: want a refusal mentioning %q, got %v", c.ip, c.port, c.why, err)
		}
		if err != nil && !IsTargetRefusal(err) {
			t.Errorf("%s: not a policy refusal: %v", c.ip, err)
		}
	}
}

func TestPolicyIsWidenedOnlyByTheLocalConfig(t *testing.T) {
	p := NewTargetPolicy([]string{"192.168.7.0/24", "nonsense"}, []int{9000}, nil)
	p.local, p.self, p.routers, p.frozen = mustNets("192.168.1.0/24"), set(nil), set(nil), true
	if err := p.Check(net.ParseIP("192.168.7.20"), 9000); err != nil {
		t.Fatalf("allow_subnets + allow_ports: %v", err)
	}
	if err := p.Check(net.ParseIP("192.168.1.64"), 9001); err == nil {
		t.Fatal("an unlisted port passed")
	}
	// A port the camera announced in its own ONVIF reply is allowed for it.
	p.SetObserved(map[string][]int{"192.168.1.64": {9001}})
	if err := p.Check(net.ParseIP("192.168.1.64"), 9001); err != nil {
		t.Fatalf("announced port: %v", err)
	}
	if err := p.Check(net.ParseIP("192.168.1.65"), 9001); err == nil {
		t.Fatal("one camera's announced port opened it on another")
	}
}

func TestOnlyAnExactLocalEntryLetsTheRouterOrThisMachineThrough(t *testing.T) {
	p := NewTargetPolicy([]string{"192.168.1.1", "192.168.20.0/24"}, nil, nil)
	p.local = mustNets("192.168.1.0/24")
	p.self, p.routers, p.frozen = set([]string{"192.168.1.10"}), set([]string{"192.168.1.1", "192.168.20.1"}), true
	if err := p.Check(net.ParseIP("192.168.1.1"), 554); err != nil {
		t.Fatalf("an NVR-router listed by its own address: %v", err)
	}
	if err := p.Check(net.ParseIP("192.168.20.1"), 80); err == nil {
		t.Fatal("a listed subnet opened its router")
	}
	if err := p.Check(net.ParseIP("192.168.1.10"), 554); err == nil {
		t.Fatal("this machine passed without being listed")
	}
}

func TestStreamURLsMustBeRTSPToAnAllowedCamera(t *testing.T) {
	p := testPolicy()
	ctx := context.Background()
	if err := p.CheckStreamURL(ctx, "rtsp://u:p@192.168.1.64:554/main"); err != nil {
		t.Fatalf("camera refused: %v", err)
	}
	if err := p.CheckStreamURL(ctx, "rtsps://192.168.1.64/main"); err != nil {
		t.Fatalf("rtsps on the default port refused: %v", err)
	}
	for _, u := range []string{
		"file:///etc/passwd", "concat:/etc/passwd", "http://192.168.1.64/x", "rtsp://192.168.1.1/admin",
		"rtsp://8.8.8.8/x", "rtsp://192.168.1.64:22/x", "rtsp://no-such-host.invalid/x",
	} {
		if err := p.CheckStreamURL(ctx, u); err == nil {
			t.Errorf("%s passed", u)
		}
	}
}

func TestProcRouteGateways(t *testing.T) {
	tbl := "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n" +
		"eth0\t00000000\t0101A8C0\t0003\t0\t0\t100\t00000000\n" + // default via 192.168.1.1
		"eth0\t0001A8C0\t00000000\t0001\t0\t0\t100\t00FFFFFF\n" + // on-link, no gateway
		"wg0\t0014000A\t0100140A\t0003\t0\t0\t0\t0000FFFF\n" // 10.20.0.0/16 via 10.20.0.1
	var got []string
	for _, line := range strings.Split(tbl, "\n")[1:] {
		if ip := procRouteGateway(line); ip != nil {
			got = append(got, ip.String())
		}
	}
	if strings.Join(got, ",") != "192.168.1.1,10.20.0.1" {
		t.Fatalf("gateways %v", got)
	}
}

func TestRPCRefusesTheRouterEvenWhenTheServerListsIt(t *testing.T) {
	r := newRPCRunner(t.Logf)
	r.policy = testPolicy()
	r.setAllowed([]string{"192.168.1.1", "8.8.8.8"})
	for _, u := range []string{"http://192.168.1.1/", "http://8.8.8.8/"} {
		res := r.run(context.Background(), RPC{Method: "GET", URL: u})
		if !strings.Contains(res.Error, "refusing") {
			t.Fatalf("%s: %+v", u, res)
		}
	}
}

func TestAStreamTheAgentRefusesIsNeverStarted(t *testing.T) {
	rec := &recorder{}
	a := testAgent(rec)
	a.d.checkURL = testPolicy().CheckStreamURL
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	defer a.stopAll()
	a.apply(ctx, &Answer{Desired: []Desired{{CameraID: 5, StreamName: "cam-5", MainURL: "file:///etc/passwd"}}})
	waitFor(t, func() bool {
		rep := a.report().Publishers
		return len(rep) == 1 && rep[0].State == "error" && strings.Contains(rep[0].Detail, "not relayed")
	})
	if rec.count() != 0 {
		t.Fatal("ffmpeg was started for a refused URL")
	}
}

// ── pins: after enrolment the server is trusted by its key only ────────

// newCert makes a certificate for 127.0.0.1 with a fresh key.
func newCert(t *testing.T) (tls.Certificate, *x509.Certificate) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: "test"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")}, IsCA: true, BasicConstraintsValid: true,
		KeyUsage: x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	leaf, _ := x509.ParseCertificate(der)
	return tls.Certificate{Certificate: [][]byte{der}, PrivateKey: key, Leaf: leaf}, leaf
}

func tlsServerWith(t *testing.T, cert tls.Certificate, h http.Handler) *httptest.Server {
	srv := httptest.NewUnstartedServer(h)
	srv.TLS = &tls.Config{Certificates: []tls.Certificate{cert}}
	srv.StartTLS()
	t.Cleanup(srv.Close)
	return srv
}

func TestPinnedTrustsItsKeyAndNotAChainTheMachineTrusts(t *testing.T) {
	enrolled, enrolledLeaf := newCert(t)
	other, otherLeaf := newCert(t)
	srvA := tlsServerWith(t, enrolled, http.NotFoundHandler())
	srvB := tlsServerWith(t, other, http.NotFoundHandler())
	roots := x509.NewCertPool()
	roots.AddCert(enrolledLeaf)
	roots.AddCert(otherLeaf) // e.g. an inspecting firewall's CA the machine trusts

	ps := NewPinSet([]string{Pin(enrolledLeaf)}, nil)
	ps.roots = roots
	if c, err := tls.Dial("tcp", srvA.Listener.Addr().String(), PinnedTLSConfig("127.0.0.1", ps, "")); err != nil {
		t.Fatalf("enrolled key refused: %v", err)
	} else {
		c.Close()
	}
	_, err := tls.Dial("tcp", srvB.Listener.Addr().String(), PinnedTLSConfig("127.0.0.1", ps, ""))
	var ue *ErrUntrusted
	if !errors.As(err, &ue) || !ue.Pinned {
		t.Fatalf("a chain-valid server with another key was trusted: %v", err)
	}
}

func TestAnAgentWithoutPinsLearnsTheFirstVerifiedKeyOnce(t *testing.T) {
	first, firstLeaf := newCert(t)
	second, secondLeaf := newCert(t)
	srvA := tlsServerWith(t, first, http.NotFoundHandler())
	srvB := tlsServerWith(t, second, http.NotFoundHandler())
	roots := x509.NewCertPool()
	roots.AddCert(firstLeaf)
	roots.AddCert(secondLeaf)
	var saved []string
	ps := NewPinSet(nil, func(p []string) { saved = p })
	ps.roots = roots
	c, err := tls.Dial("tcp", srvA.Listener.Addr().String(), PinnedTLSConfig("127.0.0.1", ps, ""))
	if err != nil {
		t.Fatalf("first verified key: %v", err)
	}
	c.Close()
	if len(saved) != 1 || saved[0] != Pin(firstLeaf) {
		t.Fatalf("learned %v", saved)
	}
	if _, err := tls.Dial("tcp", srvB.Listener.Addr().String(), PinnedTLSConfig("127.0.0.1", ps, "")); err == nil {
		t.Fatal("a second key was learned without proof")
	}
}

// repinServer answers like core: /repin with HMAC(credential hash, nonce ‖
// its own key), and the agent routes only with the credential.
func repinServer(t *testing.T, cert tls.Certificate, leaf *x509.Certificate, secret string, authSeen *[]string, mu *sync.Mutex) *httptest.Server {
	return tlsServerWith(t, cert, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		*authSeen = append(*authSeen, r.URL.Path+" "+r.Header.Get("Authorization"))
		mu.Unlock()
		switch r.URL.Path {
		case "/api/v1/site-agent/repin":
			var body struct {
				SiteID int    `json:"site_id"`
				Nonce  string `json:"nonce"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			proof := "00"
			if secret != "" && body.SiteID == 4 {
				proof = RepinProof(secret, body.Nonce, Pin(leaf))
			}
			_ = json.NewEncoder(w).Encode(map[string]string{"proof": proof})
		case "/api/v1/site-agent/sync":
			_ = json.NewEncoder(w).Encode(Answer{SiteID: 4, ETag: "e"})
		default:
			http.NotFound(w, r)
		}
	}))
}

func TestANewServerKeyIsTrustedOnlyAfterTheServerProvesIt(t *testing.T) {
	oldCert, oldLeaf := newCert(t)
	_ = oldCert
	newC, newLeaf := newCert(t)
	var seen []string
	var mu sync.Mutex
	srv := repinServer(t, newC, newLeaf, "s3cret", &seen, &mu)
	var saved []string
	ps := NewPinSet([]string{Pin(oldLeaf)}, func(p []string) { saved = p })
	c := NewPinnedClient(srv.URL, "site_4_s3cret", ps, t.Logf)
	if _, err := c.Sync(context.Background(), Report{}); err != nil {
		t.Fatalf("sync after a proven key change: %v", err)
	}
	if !ps.Has(Pin(newLeaf)) || len(saved) != 2 {
		t.Fatalf("new key not trusted/saved: %v", saved)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 || seen[0] != "/api/v1/site-agent/repin " {
		t.Fatalf("requests %q: the repin call must carry no credential", seen)
	}
}

func TestAManInTheMiddleGetsNoCredentialAndIsRefused(t *testing.T) {
	_, enrolledLeaf := newCert(t)
	mitm, mitmLeaf := newCert(t)
	var seen []string
	var mu sync.Mutex
	srv := repinServer(t, mitm, mitmLeaf, "", &seen, &mu) // does not know the secret
	ps := NewPinSet([]string{Pin(enrolledLeaf)}, nil)
	c := NewPinnedClient(srv.URL, "site_4_s3cret", ps, t.Logf)
	if _, err := c.Sync(context.Background(), Report{}); !errors.Is(err, ErrKeyNotProven) {
		t.Fatalf("want ErrKeyNotProven, got %v", err)
	}
	if ps.Has(Pin(mitmLeaf)) {
		t.Fatal("the impostor's key was trusted")
	}
	mu.Lock()
	defer mu.Unlock()
	for _, s := range seen {
		if strings.Contains(s, "s3cret") || strings.Contains(s, "Bearer") {
			t.Fatalf("the credential reached the impostor: %q", s)
		}
	}
}

func TestEnrolRecordsTheKeyItMet(t *testing.T) {
	cert, leaf := newCert(t)
	srv := tlsServerWith(t, cert, http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(EnrolResult{SiteID: 4, Token: "site_4_x"})
	}))
	c := NewClient(srv.URL, "", Pin(leaf), false)
	if _, err := c.Enrol(context.Background(), "ABCDE-FGHJK", "t", "h"); err != nil {
		t.Fatal(err)
	}
	if c.SeenPin() != Pin(leaf) {
		t.Fatalf("seen %q", c.SeenPin())
	}
}

// The same vector is in server/tests/test_sites.py: agent and server must
// compute one proof.
func TestRepinProofMatchesTheServers(t *testing.T) {
	const want = "d6faf2169657c81d6db1db0eca51a418d94cde01ad2d99ef7b38295ab46726ce"
	if got := RepinProof("s3cret", "nonce-0123456789ab", "sha256/AAA="); got != want {
		t.Fatalf("proof %s", got)
	}
}
