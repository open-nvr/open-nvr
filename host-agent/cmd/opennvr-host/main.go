// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// opennvr-host is OpenNVR's agent on the host machine. It streams USB webcams
// into their OpenNVR cameras and reports the machine's own CPU, memory, disks
// and temperatures. It opens no port: it calls core on localhost every few
// seconds and does only what core's answer describes, with devices it found
// itself. Installed and kept current by start.ps1 / start.sh (host-agent).
//
//	opennvr-host            run (the service/task runs this)
//	opennvr-host version    print the build
//	opennvr-host enrol <server> <code>   site mode: enrol this site once
//	opennvr-host site       site mode: relay this site's cameras
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/agent"
	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/logx"
	"github.com/open-nvr/open-nvr/host-agent/internal/singleton"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println(buildinfo.Build)
		return
	}
	// Site mode (issue #925, cloud ingest): this machine is at a remote site
	// and relays its cameras to an OpenNVR on the internet.
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "setup":
			os.Exit(setupCmd(os.Args[2:]))
		case "install-service":
			os.Exit(installServiceCmd(os.Args[2:]))
		case "uninstall":
			os.Exit(uninstallCmd(os.Args[2:]))
		case "enrol":
			os.Exit(enrolCmd(os.Args[2:]))
		case "site":
			os.Exit(siteCmd(os.Args[2:]))
		}
	}

	// The binary lives in <OpenNVR folder>/bin, so the folder is one up.
	exe, _ := os.Executable()
	defRepo := filepath.Dir(filepath.Dir(exe))

	repo := flag.String("repo", defRepo, "the OpenNVR folder")
	coreURL := flag.String("core", "http://127.0.0.1:8000", "OpenNVR core on this machine")
	rtsps := flag.String("rtsps", "rtsps://127.0.0.1:8322", "MediaMTX RTSPS on this machine")
	keyFile := flag.String("key-file", "", "agent key (default <repo>/data/host-agent-key/agent.key)")
	logFile := flag.String("log-file", "", "log file (default next to the key; '-' = stderr only)")
	ffmpegPath := flag.String("ffmpeg", "", "ffmpeg to use (default: look for it)")
	discoveryOnly := flag.Bool("discovery-only", false, "LAN prober: ONVIF discovery and nothing else (the Linux container)")
	flag.Parse()

	keyDir := filepath.Join(*repo, "data", "host-agent-key")
	if *keyFile == "" {
		*keyFile = filepath.Join(keyDir, "agent.key")
	}
	if *logFile == "" {
		*logFile = filepath.Join(keyDir, "agent.log")
	}
	if *logFile == "-" {
		*logFile = ""
	}
	_ = os.MkdirAll(filepath.Dir(*keyFile), 0o700)

	log := logx.Open(*logFile, os.Stderr)
	defer log.Close()

	// During an upgrade the new agent starts while the old one is still on
	// its way out (it exits once it sees core's new build). Wait for the lock
	// rather than giving up at once, or the upgrade leaves no agent running.
	//
	// The LAN prober takes no lock: it is a container with a read-only
	// filesystem and a read-only key mount, and Docker's container name is
	// already what keeps it to one. Asking for the lock there failed with
	// "read-only file system" and the prober restarted for ever (0.1.12).
	if lockPath := lockPathFor(*keyFile, *discoveryOnly); lockPath != "" {
		var lock *os.File
		var err error
		for waited := 0; ; waited++ {
			lock, err = singleton.Acquire(lockPath)
			if err == nil {
				break
			}
			if !errors.Is(err, singleton.ErrHeld) {
				log.Printf("cannot take the agent lock: %v", err)
				os.Exit(1)
			}
			if waited == 0 {
				log.Printf("%v; waiting up to a minute for it to stop", err)
			}
			if waited >= 60 {
				log.Printf("%v; this one exits", err)
				os.Exit(0)
			}
			time.Sleep(time.Second)
		}
		defer lock.Close()
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	log.Printf("OpenNVR host agent %s starting (OpenNVR folder %s)", buildinfo.Build, *repo)
	code := agent.Run(ctx, agent.Options{
		Core:          *coreURL,
		RTSPS:         *rtsps,
		KeyFile:       *keyFile,
		Repo:          *repo,
		FFmpeg:        *ffmpegPath,
		DiscoveryOnly: *discoveryOnly,
		Logf:          log.Printf,
	})
	log.Printf("OpenNVR host agent stopped")
	if code != 0 {
		log.Close()
		os.Exit(code)
	}
}

// lockPathFor is where the one-agent-per-machine lock lives: next to the
// key. The LAN prober ("" here) takes none; see main.
func lockPathFor(keyFile string, discoveryOnly bool) string {
	if discoveryOnly {
		return ""
	}
	return filepath.Join(filepath.Dir(keyFile), "agent.lock")
}
