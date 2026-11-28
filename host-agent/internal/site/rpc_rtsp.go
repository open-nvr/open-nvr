// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package site

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"net"
	"strconv"
	"time"
)

// An "rtsp" RPC is one RTSP request core needs sent to a device on the
// site's LAN (DESCRIBE, to find a camera's stream path without ONVIF) and
// the response's head. As for HTTP, core keeps the protocol (it builds the
// request and its Digest answer); the agent sends the bytes to an allowed
// device only and reads back the status line and headers (and an SDP body
// up to rtspMaxBody).

const (
	rtspMaxRequest = 8 << 10
	rtspMaxHead    = 32 << 10
	rtspMaxBody    = 64 << 10
	rtspTimeout    = 6 * time.Second
)

func (r *rpcRunner) runRTSP(ctx context.Context, rpc RPC) RPCResult {
	host, portS, err := net.SplitHostPort(rpc.Target)
	if err != nil {
		return RPCResult{Error: "bad target"}
	}
	port, err := strconv.Atoi(portS)
	if err != nil || port <= 0 || port > 65535 {
		return RPCResult{Error: "bad target port"}
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return RPCResult{Error: "an RTSP target must be an address"}
	}
	if !r.isAllowed(ip.String()) {
		return RPCResult{Error: fmt.Sprintf("%s is not a camera of this site", ip)}
	}
	if err := r.check(ip, port); err != nil {
		return RPCResult{Error: "refusing that address: " + err.Error()}
	}
	req, err := base64.StdEncoding.DecodeString(rpc.BodyB64)
	if err != nil || len(req) == 0 || len(req) > rtspMaxRequest {
		return RPCResult{Error: "bad request"}
	}
	if !bytes.HasSuffix(req, []byte("\r\n\r\n")) || !bytes.Contains(bytes.SplitN(req, []byte("\r\n"), 2)[0], []byte(" RTSP/1.0")) {
		return RPCResult{Error: "not an RTSP request"}
	}
	dctx, cancel := context.WithTimeout(ctx, rtspTimeout)
	defer cancel()
	c, err := (&net.Dialer{}).DialContext(dctx, "tcp", net.JoinHostPort(ip.String(), portS))
	if err != nil {
		return RPCResult{Error: err.Error()}
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(rtspTimeout))
	if _, err := c.Write(req); err != nil {
		return RPCResult{Error: err.Error()}
	}
	br := bufio.NewReader(io.LimitReader(c, rtspMaxHead+rtspMaxBody))
	var head bytes.Buffer
	length := 0
	for {
		line, err := br.ReadString('\n')
		head.WriteString(line)
		if err != nil {
			if head.Len() == 0 {
				return RPCResult{Error: err.Error()}
			}
			break
		}
		if head.Len() > rtspMaxHead {
			return RPCResult{Error: "the answer is too large"}
		}
		if line == "\r\n" || line == "\n" {
			break
		}
		if k, v, ok := cutHeader(line); ok && k == "content-length" {
			length, _ = strconv.Atoi(v)
		}
	}
	if length > 0 && length <= rtspMaxBody {
		body := make([]byte, length)
		if n, _ := io.ReadFull(br, body); n > 0 {
			head.Write(body[:n])
		}
	}
	return RPCResult{Status: 200, BodyB64: base64.StdEncoding.EncodeToString(head.Bytes())}
}

func cutHeader(line string) (string, string, bool) {
	i := bytes.IndexByte([]byte(line), ':')
	if i <= 0 {
		return "", "", false
	}
	return string(bytes.ToLower(bytes.TrimSpace([]byte(line[:i])))), string(bytes.TrimSpace([]byte(line[i+1:]))), true
}
