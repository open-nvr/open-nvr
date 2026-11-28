// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package main

import (
	"bufio"
	"context"
	"flag"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/site"
)

// setupCmd: `opennvr-host setup [-config F]` asks for the setup code (from
// the setup link OpenNVR's Sites page made) and enrols this machine with it.
// The code is read from the terminal, or from standard input, never from the
// command line: a command line is visible to every user of the machine and
// kept in shell history.
func setupCmd(args []string) int {
	fs := flag.NewFlagSet("setup", flag.ExitOnError)
	cfgPath := fs.String("config", site.DefaultPath(), "where to save the site's config")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: opennvr-host setup [-config FILE]   (then paste the setup code)")
		fs.PrintDefaults()
	}
	_ = fs.Parse(args)
	if fs.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "the setup code is not given on the command line: run `opennvr-host setup` and paste it when asked")
		return 2
	}
	raw, err := readSetupCode(os.Stdin, os.Stderr)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	code, err := site.ParseSetupCode(raw)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 2
	}
	host, _ := os.Hostname()
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	fmt.Fprintf(os.Stderr, "Checking %s ...\n", code.Server)
	res, err := site.Setup(ctx, code, buildinfo.Build, host)
	if err != nil {
		fmt.Fprintln(os.Stderr, "Setup failed:", err)
		return 1
	}
	cfg := &site.Config{Server: res.Server, SiteID: res.SiteID, Token: res.Token, Pins: []string{res.Pin}}
	if err := cfg.Save(*cfgPath); err != nil {
		fmt.Fprintln(os.Stderr, "set up, but the config could not be saved:", err)
		return 1
	}
	fmt.Printf("Set up as site %d (%s) on %s. Config: %s\nStart relaying with:  opennvr-host site -config %s\n",
		res.SiteID, res.Name, res.Server, *cfgPath, *cfgPath)
	return 0
}

// readSetupCode asks for the code and reads one line.
func readSetupCode(in io.Reader, prompt io.Writer) (string, error) {
	fmt.Fprint(prompt, "Paste the setup code (or the setup link) and press Enter: ")
	line, err := bufio.NewReader(in).ReadString('\n')
	if err != nil && line == "" {
		return "", fmt.Errorf("no setup code was given")
	}
	return strings.TrimSpace(line), nil
}
