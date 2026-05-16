# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
#
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""Single source of truth for the placeholder-secret detector.

Imported by:

* ``server/core/config.py`` — the runtime Pydantic validator that rejects
  weak/placeholder secrets at startup (V-002).
* ``Makefile`` ``check-secrets`` target — the CI-friendly linter that
  flags an ``.env`` file before the operator ever starts the server.

This file has **no dependencies** beyond the Python standard library so
that the Makefile can import it without triggering the rest of the
application's import graph (which would in turn require a fully populated
``.env`` just to discover the fragment list — see M0 followup H-3).
"""

# Substrings that indicate a value is still a placeholder shipped in
# env.example or in a quickstart copy. Match is case-insensitive and
# substring-based so that variants ("change-this-XYZ", "your-secret-here-...",
# etc.) all get rejected.
#
# Aligned with Zenodo paper (DOI 10.5281/zenodo.17261761) §3.1 (default
# credentials, ETSI EN 303 645 unique-credential requirement) and §4.1
# Principle "Secure-by-Design" defaults.
#
# Each fragment must be at least 6 characters: shorter fragments (e.g.
# "todo", "fixme") can appear by chance inside a legitimately random
# urlsafe-base64 secret. ``secrets.token_urlsafe(48)`` contains the
# substring "todo" roughly 1-in-17,000 of the time, which would cause
# false positives at fleet scale and would erode operator trust in the
# validator. Six characters is the lowest length where the per-secret
# false-positive rate stays below ~1-in-10-million for the 64-char
# alphabet we generate from.
PLACEHOLDER_FRAGMENTS: tuple[str, ...] = (
    "change-this",
    "change_this",
    "changeme",
    "your-secret",
    "your_secret",
    "your-key",
    "your_key",
    "secret-here",
    "key-here",
    "placeholder",
    "generate-with",
    "openssl rand",
    "openssl-rand",
    "insert-here",
    "replace-me",
    "replaceme",
    "example",
)

# Backwards-compatible private alias for code that previously imported the
# list from core.config. Either name is acceptable.
_PLACEHOLDER_FRAGMENTS = PLACEHOLDER_FRAGMENTS
