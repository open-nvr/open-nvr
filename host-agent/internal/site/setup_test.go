// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
)

// The same vectors are in server/tests/test_sites.py: agent and server must
// derive the same keys and proofs.
func TestSetupDerivationsMatchTheServers(t *testing.T) {
	key := SetupProofKey("abcdefghijklmnopqrstuvwxyz")
	if key != "0073dced0b2b7dc0c88e80f5805becf5783a9ba8ec488629ff2a169b30ec7b50" {
		t.Fatalf("proof key %s", key)
	}
	if p := SetupHelloProof(key, "nonce-0123456789ab", "sha256/AAA="); p != "57ab763d66a28479da86d02d3ff73be1064196cf37bd22dfc256a204735aadf4" {
		t.Fatalf("hello proof %s", p)
	}
}

func TestParseSetupCode(t *testing.T) {
	for in, want := range map[string]SetupCode{
		"https://nvr.example.com/site-setup#AbCd_123.abcdefghijklmnopqrstuvwxyz":  {"https://nvr.example.com", "AbCd_123", "abcdefghijklmnopqrstuvwxyz"},
		"  nvr.example.com#AbCd_123.abcdefghijklmnopqrstuvwxyz\r\n":               {"https://nvr.example.com", "AbCd_123", "abcdefghijklmnopqrstuvwxyz"},
		`"nvr.example.com:8443#AbCd_123.abcdefghijklmnopqrstuvwxyz"`:              {"https://nvr.example.com:8443", "AbCd_123", "abcdefghijklmnopqrstuvwxyz"},
		"https://nvr.example.com:8443/site-setup#AbCd.abcdefghijklmnopqrstuvwxyz": {"https://nvr.example.com:8443", "AbCd", "abcdefghijklmnopqrstuvwxyz"},
	} {
		got, err := ParseSetupCode(in)
		if err != nil || got != want {
			t.Errorf("%q: %+v %v", in, got, err)
		}
	}
	for _, bad := range []string{"", "nvr.example.com", "nvr.example.com#short", "http://nvr.example.com#AbCd.abcdefghijklmnopqrstuvwxyz", "#AbCd.abcdefghijklmnopqrstuvwxyz"} {
		if _, err := ParseSetupCode(bad); err == nil {
			t.Errorf("%q parsed", bad)
		}
	}
}

// setupServer answers like core: hello with the proof for its own key (if it
// knows the secret), enrol with the token.
func setupServer(t *testing.T, knowsSecret bool, seen *[]string, mu *sync.Mutex) (string, string) {
	cert, leaf := newCert(t)
	srv := tlsServerWith(t, cert, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		mu.Lock()
		*seen = append(*seen, r.URL.Path+" "+string(body))
		mu.Unlock()
		switch r.URL.Path {
		case "/api/v1/site-agent/hello":
			var b struct{ TokenID, Nonce string }
			var raw map[string]string
			_ = json.Unmarshal(body, &raw)
			b.TokenID, b.Nonce = raw["token_id"], raw["nonce"]
			proof := strings.Repeat("0", 64)
			if knowsSecret && b.TokenID == "AbCd_123" {
				proof = SetupHelloProof(SetupProofKey("abcdefghijklmnopqrstuvwxyz"), b.Nonce, Pin(leaf))
			}
			_ = json.NewEncoder(w).Encode(map[string]string{"proof": proof})
		case "/api/v1/site-agent/enrol":
			_ = json.NewEncoder(w).Encode(EnrolResult{SiteID: 9, Name: "Shop 9", Token: "site_9_cred"})
		default:
			http.NotFound(w, r)
		}
	}))
	return srv.URL, Pin(leaf)
}

func TestSetupEnrolsAfterTheServerProvesItAndPinsThatKey(t *testing.T) {
	var seen []string
	var mu sync.Mutex
	url, pin := setupServer(t, true, &seen, &mu)
	code := SetupCode{Server: url, TokenID: "AbCd_123", Secret: "abcdefghijklmnopqrstuvwxyz"}
	res, err := Setup(context.Background(), code, "t", "shop-pi")
	if err != nil {
		t.Fatal(err)
	}
	if res.SiteID != 9 || res.Token != "site_9_cred" || res.Pin != pin {
		t.Fatalf("result %+v", res)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) != 2 || strings.Contains(seen[0], "abcdefghijklmnopqrstuvwxyz") || !strings.Contains(seen[1], "AbCd_123.abcdefghijklmnopqrstuvwxyz") {
		t.Fatalf("requests %q: the secret must go only to enrol, after hello", seen)
	}
}

func TestSetupSendsNothingSecretToAServerThatCannotProveIt(t *testing.T) {
	var seen []string
	var mu sync.Mutex
	url, _ := setupServer(t, false, &seen, &mu) // an impostor: does not know the secret
	code := SetupCode{Server: url, TokenID: "AbCd_123", Secret: "abcdefghijklmnopqrstuvwxyz"}
	if _, err := Setup(context.Background(), code, "t", "h"); !errors.Is(err, ErrServerNotProven) {
		t.Fatalf("want ErrServerNotProven, got %v", err)
	}
	mu.Lock()
	defer mu.Unlock()
	for _, s := range seen {
		if strings.Contains(s, "abcdefghijklmnopqrstuvwxyz") || strings.Contains(s, "/enrol") {
			t.Fatalf("the impostor got %q", s)
		}
	}
}
