// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Command opennvr-sign makes and signs the site agent's update manifest.
//
//	opennvr-sign keygen
//	    A new key pair. Prints the private key (the release workflow's
//	    AGENT_SIGNING_KEY secret, or the offline standby key) and the public
//	    key to add to internal/update/keys.go. Run it on a trusted machine;
//	    the private key is printed once and stored nowhere.
//
//	opennvr-sign sign -key FILE -dir OUT -build B -version V -source S -built-at T
//	    Writes OUT/manifest.json (every agent build under OUT with its
//	    SHA-256) and OUT/manifest.sig. Run by build.sh in the core image
//	    build, with the key as a BuildKit secret.
package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"github.com/open-nvr/open-nvr/host-agent/internal/update"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "keygen":
		os.Exit(keygen())
	case "sign":
		os.Exit(sign(os.Args[2:]))
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: opennvr-sign keygen | opennvr-sign sign -key FILE -dir OUT -build B -version V -source S -built-at T")
	os.Exit(2)
}

func keygen() int {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	fmt.Printf("private key (keep secret; GitHub secret AGENT_SIGNING_KEY, or the offline standby):\n  %s\n",
		base64.StdEncoding.EncodeToString(priv.Seed()))
	fmt.Printf("public key (add to host-agent/internal/update/keys.go):\n  %s\nkey id: %s\n",
		base64.StdEncoding.EncodeToString(pub), update.KeyID(pub))
	return 0
}

func sign(args []string) int {
	fl := flag.NewFlagSet("sign", flag.ExitOnError)
	keyFile := fl.String("key", "", "file with the base64 signing key")
	dir := fl.String("dir", "", "the build folder (build.sh's OUT)")
	build := fl.String("build", "", "the build string")
	version := fl.String("version", "", "the version")
	source := fl.String("source", "", "the agent source hash")
	builtAt := fl.Int64("built-at", 0, "the build time, Unix seconds")
	minVersion := fl.String("min-version", "", "the oldest version that may keep running without updating")
	_ = fl.Parse(args)
	if *keyFile == "" || *dir == "" || *build == "" || *version == "" || *source == "" || *builtAt == 0 {
		usage()
	}
	keyB64, err := os.ReadFile(*keyFile)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	priv, err := update.ParsePrivateKey(string(keyB64))
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	files, err := hashBuilds(*dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	m := update.Manifest{Format: update.Format, Build: *build, Version: *version, Source: *source,
		BuiltAt: *builtAt, MinVersion: *minVersion, Files: files}
	data, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	sig, err := update.Sign(data, priv)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if err := os.WriteFile(filepath.Join(*dir, "manifest.json"), data, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	if err := os.WriteFile(filepath.Join(*dir, "manifest.sig"), sig, 0o644); err != nil {
		fmt.Fprintln(os.Stderr, err)
		return 1
	}
	fmt.Printf("signed %d agent builds (%s), key %s\n", len(files), *build, update.KeyID(priv.Public().(ed25519.PublicKey)))
	return 0
}

// hashBuilds: every opennvr-host[.exe] under dir, by path relative to it.
func hashBuilds(dir string) (map[string]update.File, error) {
	out := map[string]update.File{}
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || (d.Name() != "opennvr-host" && d.Name() != "opennvr-host.exe") {
			return nil
		}
		f, err := os.Open(path)
		if err != nil {
			return err
		}
		defer f.Close()
		h := sha256.New()
		n, err := io.Copy(h, f)
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(dir, path)
		if err != nil {
			return err
		}
		out[strings.ReplaceAll(rel, string(filepath.Separator), "/")] = update.File{SHA256: hex.EncodeToString(h.Sum(nil)), Size: n}
		return nil
	})
	if err == nil && len(out) == 0 {
		err = fmt.Errorf("no agent builds under %s", dir)
	}
	return out, err
}
