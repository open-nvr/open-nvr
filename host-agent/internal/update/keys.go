// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

package update

import "strings"

// The public keys whose signatures this agent accepts for its own updates
// (base64 ed25519). Two: the release key CI signs with, and a standby key
// kept offline only. If the release key is lost or leaks, a build signed with
// the standby key that lists a new pair here replaces it on every site, and
// nothing has to be reinstalled. docs/SITE_AGENT.md "The signing keys" has the steps
// (`go run ./cmd/opennvr-sign keygen`).
//
// While this is empty, site agents do not update themselves at all.
var trustedKeysB64 = []string{}

// buildKeysB64: more trusted keys, comma separated, set at build time
// (-ldflags -X .../update.buildKeysB64=...): for a fork with its own release
// key, and for tests. The official builds set none.
var buildKeysB64 = ""

// TrustedKeys are the keys compiled into this agent.
func TrustedKeys() Keys {
	all := append([]string(nil), trustedKeysB64...)
	for _, k := range strings.Split(buildKeysB64, ",") {
		if strings.TrimSpace(k) != "" {
			all = append(all, k)
		}
	}
	k, err := ParseKeys(all...)
	if err != nil {
		panic(err) // a typo in this file: caught by the tests
	}
	return k
}
