// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"context"
	"crypto/tls"
	"fmt"
	"io"
	"net"
	"sync"
	"time"
)

// Relay turns plain RTSP from the local ffmpeg into RTSP over TLS to the
// server's 443, offering the ingest ALPN. ffmpeg cannot set ALPN or pin a
// certificate itself; this can, and it is the only thing that leaves the
// site's network carrying video.
//
// It listens on 127.0.0.1 only, on a free port, and every accepted
// connection gets its own TLS connection to the server.
type Relay struct {
	ln     net.Listener
	target string
	tls    *tls.Config
	logf   func(string, ...any)
	wg     sync.WaitGroup
}

// StartRelay listens on loopback and relays to host:port with tlsCfg
// (TLSConfig with the ingest ALPN).
func StartRelay(ctx context.Context, host string, port int, tlsCfg *tls.Config, logf func(string, ...any)) (*Relay, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, err
	}
	r := &Relay{ln: ln, target: net.JoinHostPort(host, fmt.Sprint(port)), tls: tlsCfg, logf: logf}
	go func() {
		<-ctx.Done()
		_ = ln.Close()
	}()
	go r.accept(ctx)
	return r, nil
}

// Port is the loopback port ffmpeg publishes to.
func (r *Relay) Port() int { return r.ln.Addr().(*net.TCPAddr).Port }

// Close stops accepting; open relays end when their ffmpeg does.
func (r *Relay) Close() { _ = r.ln.Close() }

func (r *Relay) accept(ctx context.Context) {
	for {
		c, err := r.ln.Accept()
		if err != nil {
			return
		}
		r.wg.Add(1)
		go func() {
			defer r.wg.Done()
			r.serve(ctx, c)
		}()
	}
}

func (r *Relay) serve(ctx context.Context, local net.Conn) {
	defer local.Close()
	d := &tls.Dialer{NetDialer: &net.Dialer{Timeout: 15 * time.Second, KeepAlive: 30 * time.Second}, Config: r.tls}
	dctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	conn, err := d.DialContext(dctx, "tcp", r.target)
	cancel()
	if err != nil {
		r.logf("relay: cannot reach %s: %v", r.target, err)
		return
	}
	remote := conn.(*tls.Conn)
	defer remote.Close()
	// Offered ALPN must come back: otherwise this landed on the web server
	// (an older server without the ingest route, or a proxy in front that
	// ends TLS itself), and RTSP sent there is wasted at best.
	if want := r.tls.NextProtos; len(want) > 0 && remote.ConnectionState().NegotiatedProtocol != want[0] {
		r.logf("relay: %s did not accept ALPN %q (got %q): the server has no camera ingest on 443, or a proxy in front ends TLS",
			r.target, want[0], remote.ConnectionState().NegotiatedProtocol)
		return
	}
	pipe(local, remote)
}

// pipe copies both ways until either side closes, then closes both.
func pipe(a, b net.Conn) {
	done := make(chan struct{}, 2)
	cp := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = cw.CloseWrite()
		}
		done <- struct{}{}
	}
	go cp(a, b)
	go cp(b, a)
	<-done
	_ = a.Close()
	_ = b.Close()
	<-done
}
