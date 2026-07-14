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

Imported by config.py's startup validator (V-002) and the Makefile's
check-secrets linter. Kept dependency-free (stdlib only) so the Makefile can
import it without loading the rest of the app.
"""

# Substrings that mark a value as a shipped placeholder (case-insensitive,
# substring match). Each is >=6 chars: shorter fragments can occur by chance
# inside a real random secret and cause false positives. See V-002.
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

# Private alias kept for older import sites.
_PLACEHOLDER_FRAGMENTS = PLACEHOLDER_FRAGMENTS
