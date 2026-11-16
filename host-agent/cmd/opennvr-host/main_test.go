// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package main

import (
	"path/filepath"
	"testing"
)

// The LAN prober runs with a read-only filesystem and a read-only key
// mount; a lock file next to the key cannot be created there (0.1.12:
// "cannot take the agent lock: ... read-only file system", restarting for
// ever). The host agent keeps its lock next to the key.
func TestTheProberTakesNoLockAndTheAgentLocksNextToItsKey(t *testing.T) {
	if got := lockPathFor("/key/agent.key", true); got != "" {
		t.Fatalf("discovery-only must take no lock, got %q", got)
	}
	want := filepath.Join("/repo/data/host-agent-key", "agent.lock")
	if got := lockPathFor("/repo/data/host-agent-key/agent.key", false); got != want {
		t.Fatalf("agent lock = %q, want %q", got, want)
	}
}
