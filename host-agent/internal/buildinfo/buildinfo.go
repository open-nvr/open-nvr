// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package buildinfo carries the build the agent was cut from. The core image
// stamps it with -ldflags, from the same version and commit core reports, so
// an agent can tell when it was built for another core (see core.SyncResponse).
package buildinfo

import (
	"strconv"
	"strings"
)

// Build is "<version>+<commit>", or "<version>" without a commit. "dev" for an
// unstamped build, which never asks to be replaced.
var Build = "dev"

// Source is a hash of the agent's own sources (build.sh). A site agent
// updates itself only when a signed build has OTHER sources: a new core
// commit that did not touch the agent is not a new agent.
var Source = ""

// BuiltAt is when the build was made (Unix seconds, as a string for
// -ldflags -X). A site agent never takes an update older than itself.
var BuiltAt = ""

// Version is Build without the "+<commit>" part.
func Version() string {
	v, _, _ := strings.Cut(Build, "+")
	return v
}

// BuiltAtUnix is BuiltAt as a number (0 when unstamped).
func BuiltAtUnix() int64 {
	n, _ := strconv.ParseInt(BuiltAt, 10, 64)
	return n
}
