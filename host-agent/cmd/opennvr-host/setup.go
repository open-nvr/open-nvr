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
	"runtime"
	"strings"
	"time"

	"github.com/open-nvr/open-nvr/host-agent/internal/buildinfo"
	"github.com/open-nvr/open-nvr/host-agent/internal/onvifdisc"
	"github.com/open-nvr/open-nvr/host-agent/internal/site"
	"github.com/open-nvr/open-nvr/host-agent/internal/siteinstall"
)

// setupCmd: `opennvr-host setup` asks for the setup code (from the setup
// link OpenNVR's Sites page made) and enrols this machine with it. Run with
// sudo on Linux, it also installs the agent as a service and starts it: one
// command, and the site is relaying. The code is read from the terminal, or
// from standard input, never from the command line: a command line is
// visible to every user of the machine and kept in shell history.
func setupCmd(args []string) int {
	fs := flag.NewFlagSet("setup", flag.ExitOnError)
	cfgPath := fs.String("config", site.DefaultPath(), "where to save the site's config (when not installed as a service)")
	noService := fs.Bool("no-service", false, "only enrol; do not install the service")
	fs.Usage = func() {
		fmt.Fprintln(os.Stderr, "usage: sudo opennvr-host setup   (then paste the setup code)")
		fs.PrintDefaults()
	}
	_ = fs.Parse(args)
	if fs.NArg() != 0 {
		fmt.Fprintln(os.Stderr, "the setup code is not given on the command line: run `opennvr-host setup` and paste it when asked")
		return 2
	}
	// Decide before asking for the code: the code is single use.
	service := !*noService && siteinstall.Supported() == nil
	if !*noService && runtime.GOOS == "linux" && !service {
		fmt.Fprintf(os.Stderr, "Note: %v. Setting up without the service.\n", siteinstall.Supported())
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
	if !service {
		if err := cfg.Save(*cfgPath); err != nil {
			fmt.Fprintln(os.Stderr, "set up, but the config could not be saved:", err)
			return 1
		}
		fmt.Printf("Set up as site %d (%s) on %s. Config: %s\nStart relaying with:  opennvr-host site -config %s\n",
			res.SiteID, res.Name, res.Server, *cfgPath, *cfgPath)
		if runtime.GOOS == "linux" {
			fmt.Printf("To run it as a service instead (it starts again after a reboot):  sudo %s install-service -config %s\n",
				os.Args[0], *cfgPath)
		}
		return 0
	}
	if rc := installService(cfg); rc != 0 {
		fmt.Fprintf(os.Stderr, "The site is enrolled; its config is in /%s.\n", siteinstall.ConfigPath)
		return rc
	}
	report(res.Server, res.SiteID, res.Name)
	return 0
}

// installServiceCmd: `sudo opennvr-host install-service [-config F]` installs
// an already enrolled agent (`enrol`, or `setup -no-service`) as a service.
func installServiceCmd(args []string) int {
	fs := flag.NewFlagSet("install-service", flag.ExitOnError)
	cfgPath := fs.String("config", site.DefaultPath(), "the site's config, written by enrol or setup")
	_ = fs.Parse(args)
	if err := siteinstall.Supported(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	cfg, err := site.Load(*cfgPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if rc := installService(cfg); rc != 0 {
		return rc
	}
	report(cfg.Server, cfg.SiteID, "")
	if *cfgPath != "/"+siteinstall.ConfigPath {
		fmt.Printf("The service uses its own copy of the config (/%s); %s is no longer needed.\n",
			siteinstall.ConfigPath, *cfgPath)
	}
	return 0
}

// uninstallCmd: `sudo opennvr-host uninstall` removes the service, the
// installed agent, its config and its state.
func uninstallCmd(args []string) int {
	fs := flag.NewFlagSet("uninstall", flag.ExitOnError)
	yes := fs.Bool("yes", false, "do not ask")
	_ = fs.Parse(args)
	if err := siteinstall.Supported(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if !*yes {
		fmt.Fprint(os.Stderr, "Remove the OpenNVR site agent, its service and this site's credential from this machine? [y/N] ")
		line, _ := bufio.NewReader(os.Stdin).ReadString('\n')
		if a := strings.ToLower(strings.TrimSpace(line)); a != "y" && a != "yes" {
			fmt.Fprintln(os.Stderr, "Nothing removed.")
			return 1
		}
	}
	if err := siteinstall.New(os.Stdout).Uninstall(); err != nil {
		fmt.Fprintln(os.Stderr, "uninstall:", err)
		return 1
	}
	return 0
}

func installService(cfg *site.Config) int {
	data, err := cfg.Marshal()
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	exe, err := os.Executable()
	if err != nil {
		fmt.Fprintln(os.Stderr, "cannot find this program's own file:", err)
		return 1
	}
	in := siteinstall.New(os.Stdout)
	if err := in.Install(exe, data); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	for i := 0; i < 20 && !in.Active(); i++ {
		time.Sleep(500 * time.Millisecond)
	}
	if !in.Active() {
		fmt.Fprintln(os.Stderr, "The service did not start. See: journalctl -u opennvr-site -n 50")
		return 1
	}
	if others := siteinstall.OtherAgents(os.Getpid()); len(others) > 0 {
		fmt.Fprintf(os.Stderr, "Another `opennvr-host site` is running here (process %s), probably started by hand before: stop it, the service does its work now.\n",
			strings.Join(others, ", "))
	}
	return 0
}

// report prints what the person at the site needs to know, in plain words.
func report(server string, siteID int, name string) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	found := len(onvifdisc.Probe(ctx, 3*time.Second))
	who := fmt.Sprintf("site %d", siteID)
	if name != "" {
		who = fmt.Sprintf("%q", name)
	}
	fmt.Printf("\nDone. This machine relays %s to %s, and keeps doing so after a reboot.\n", who, server)
	switch found {
	case 0:
		fmt.Println("No camera answered on this network yet (cameras without ONVIF are added by address in OpenNVR).")
	case 1:
		fmt.Println("1 camera answered on this network.")
	default:
		fmt.Printf("%d cameras answered on this network.\n", found)
	}
	fmt.Println("Finish in OpenNVR: Sites, then add the cameras.")
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
