// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/ffmpeg"
	"github.com/open-nvr/open-nvr/host-agent/internal/logx"
	"github.com/open-nvr/open-nvr/host-agent/internal/singleton"
	"github.com/open-nvr/open-nvr/host-agent/internal/site"
)

// enrolCmd: `opennvr-host enrol [-config F] [-pin sha256/...] <server> <code>`
// trades the one-time code from the server's Sites page for the site's
// credential and saves it.
func enrolCmd(args []string) int {
	fs := flag.NewFlagSet("enrol", flag.ExitOnError)
	cfgPath := fs.String("config", site.DefaultPath(), "where to save the site's config")
	pin := fs.String("pin", "", "the server certificate's pin (sha256/...), as the Sites page shows it, for a server whose certificate is self-signed")
	insecure := fs.Bool("insecure-skip-verify", false, "do not verify the server at all (testing only)")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: opennvr-host enrol [-config FILE] [-pin sha256/...] <server> <code>")
		fs.PrintDefaults()
	}
	_ = fs.Parse(args)
	if fs.NArg() != 2 {
		fs.Usage()
		return 2
	}
	server, err := site.NormaliseServer(fs.Arg(0))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	host, _ := os.Hostname()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	c := site.NewClient(server, "", *pin, *insecure)
	res, err := c.Enrol(ctx, fs.Arg(1), buildinfo.Build, host)
	if err != nil {
		var ue *site.ErrUntrusted
		if errors.As(err, &ue) {
			fmt.Fprintf(os.Stderr, "%s's certificate is not trusted by this machine.\n"+
				"Its pin is:\n\n    %s\n\n"+
				"If the Sites page shows the same pin, run again with:\n\n"+
				"    opennvr-host enrol -pin %s %s %s\n", server, ue.ServerPin, ue.ServerPin, server, fs.Arg(1))
			return 1
		}
		fmt.Fprintln(os.Stderr, "enrolment failed:", err)
		return 1
	}
	cfg := &site.Config{Server: server, SiteID: res.SiteID, Token: res.Token, Insecure: *insecure}
	if !*insecure {
		// From now on the agent trusts this key only, not the machine's
		// certificate store (a firewall inspecting HTTPS would otherwise see
		// the site's credential and its camera passwords).
		cfg.Pins = []string{c.SeenPin()}
	}
	if err := cfg.Save(*cfgPath); err != nil {
		fmt.Fprintln(os.Stderr, "enrolled, but the config could not be saved:", err)
		return 1
	}
	fmt.Printf("Enrolled as site %d (%s). Config: %s\nStart relaying with:  opennvr-host site -config %s\n",
		res.SiteID, res.Name, *cfgPath, *cfgPath)
	return 0
}

// siteCmd: `opennvr-host site [-config F]` relays this site's cameras.
func siteCmd(args []string) int {
	fs := flag.NewFlagSet("site", flag.ExitOnError)
	cfgPath := fs.String("config", site.DefaultPath(), "the site's config, written by `enrol`")
	ffmpegPath := fs.String("ffmpeg", "", "ffmpeg to use as the fallback relay (default: look for it; not required)")
	logFile := fs.String("log-file", "", "log file ('' = stderr only)")
	noDiscovery := fs.Bool("no-discovery", false, "do not look for ONVIF cameras on this network")
	_ = fs.Parse(args)

	log := logx.Open(*logFile, os.Stderr)
	defer log.Close()
	cfg, err := site.Load(*cfgPath)
	if err != nil {
		log.Printf("%v", err)
		return 1
	}
	// One agent per config: two would relay every camera twice and fight
	// over its paths.
	lock, err := siteLock(cfg)
	if err != nil {
		log.Printf("%v", err)
		return 1
	}
	defer lock.Close()
	ff := ffmpeg.Find(*ffmpegPath)
	if ff == "" {
		log.Printf("cameras are relayed by the built-in relay (ffmpeg, the fallback for a camera it cannot handle, is not installed)")
	} else {
		log.Printf("cameras are relayed by the built-in relay; ffmpeg %s at %s is the fallback", ffmpeg.Version(ff), ff)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	log.Printf("OpenNVR site agent %s for site %d at %s", buildinfo.Build, cfg.SiteID, cfg.Server)
	_ = site.Run(ctx, site.Options{
		Config: cfg, FFmpeg: ff, Version: buildinfo.Build, Logf: log.Printf, Discover: !*noDiscovery,
		ConfigPath: *cfgPath,
		StatePath:  statePath(),
	})
	log.Printf("site agent stopped")
	return 0
}

// siteLock keeps one agent per site on this machine: two would relay every
// camera twice and fight over its paths. It is keyed by the site (server and
// site id), not by the config file: the service reads its config from a
// systemd credential path while an agent started by hand reads another copy.
//
// On Linux it is an abstract unix socket: shared by every user and every
// mount namespace (the service runs as a throwaway user with a private /tmp),
// and gone the moment the process ends. Elsewhere a file lock in the temp
// folder.
func siteLock(cfg *site.Config) (io.Closer, error) {
	sum := sha256.Sum256([]byte(cfg.Server + "#" + strconv.Itoa(cfg.SiteID)))
	key := "opennvr-site-" + hex.EncodeToString(sum[:8])
	held := fmt.Errorf("another site agent for site %d is already running on this machine", cfg.SiteID)
	if runtime.GOOS == "linux" {
		ln, err := net.Listen("unix", "@"+key)
		if err != nil {
			if errors.Is(err, syscall.EADDRINUSE) {
				return nil, held
			}
			return nil, fmt.Errorf("cannot take the site agent's lock: %w", err)
		}
		return ln, nil
	}
	f, err := singleton.Acquire(filepath.Join(os.TempDir(), key+".lock"))
	if errors.Is(err, singleton.ErrHeld) {
		return nil, held
	}
	if err != nil {
		return nil, fmt.Errorf("cannot take the site agent's lock: %w", err)
	}
	return f, nil
}

// statePath: under systemd the service's state directory ($STATE_DIRECTORY,
// the unit's StateDirectory=), where the agent keeps what it learns: its
// config there is a read-only credential.
func statePath() string {
	dir, _, _ := strings.Cut(os.Getenv("STATE_DIRECTORY"), ":")
	if dir == "" {
		return ""
	}
	return filepath.Join(dir, "state.json")
}
