// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

// Package buildinfo carries the build the agent was cut from. The core image
// stamps it with -ldflags, from the same version and commit core reports, so
// an agent can tell when it was built for another core (see core.SyncResponse).
package buildinfo

// Build is "<version>+<commit>", or "<version>" without a commit. "dev" for an
// unstamped build, which never asks to be replaced.
var Build = "dev"
