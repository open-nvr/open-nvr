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

"""Redact credentials from URLs before they reach log files.

Camera RTSP/RTSPS URLs and the MediaMTX ``source_url`` derived from them carry
``user:pass@`` basic-auth userinfo (and sometimes ``?token=`` / ``?jwt=`` query
secrets). Those must never be written to ``logs/server.log`` or the audit log.

Call :func:`redact_url_credentials` at every logging site that includes a
stream/source URL. Payloads *sent to* MediaMTX must keep their credentials —
only the copy that goes to a logger should be redacted.

Mirrors the logic in ``services.kai_c_service._redact_rtsp_url_for_log`` so the
two stay consistent; that private copy predates this shared helper.
"""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Query-string keys whose VALUES are secrets and must never be logged. Matched
# case-insensitively. Covers camera creds (username/password), stream/webhook
# tokens (t, jwt, token), MFA codes, and generic api-key/secret params.
SENSITIVE_QUERY_KEYS = frozenset(
    {
        "password", "passwd", "pwd", "username", "user",
        "token", "jwt", "t", "access_token", "refresh_token",
        "api_key", "apikey", "secret", "code", "key",
        "auth", "sig", "signature",
    }
)


def redact_query_params(params) -> dict:
    """Return a plain dict copy of a query-param mapping with the values of
    sensitive keys (see ``SENSITIVE_QUERY_KEYS``) replaced by ``<redacted>``."""
    out: dict = {}
    for k, v in dict(params).items():
        out[k] = "<redacted>" if str(k).lower() in SENSITIVE_QUERY_KEYS else v
    return out


def redact_url_query(url: str | None) -> str | None:
    """Return ``url`` with basic-auth userinfo stripped and the VALUES of
    sensitive query keys masked (keys preserved for debugging). Non-sensitive
    params (``page``, ``limit``, …) are kept as-is."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        return redact_url_credentials(url)
    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username or parsed.password:
        netloc = f"<redacted>@{netloc}"
    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        pairs = [
            (k, "<redacted>" if k.lower() in SENSITIVE_QUERY_KEYS else v)
            for k, v in pairs
        ]
        # safe="<>" keeps the "<redacted>" placeholder readable in logs
        # instead of percent-encoding it to %3Credacted%3E.
        query = urlencode(pairs, safe="<>")
    else:
        query = ""
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, query, "")
    )


def redact_url_credentials(url: str | None) -> str | None:
    """Return ``url`` with basic-auth userinfo and the query string replaced by
    ``<redacted>`` placeholders.

    * ``None`` / empty input passes through unchanged.
    * ``rtsp://admin:pass@1.2.3.4:554/stream?jwt=x`` ->
      ``rtsp://<redacted>@1.2.3.4:554/stream?<redacted>``
    * Best-effort: if the URL can't be parsed we still drop the query string
      (a cheap substring op that cannot raise) rather than risk logging it.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
    except Exception:
        if "?" in url:
            base, _, _ = url.partition("?")
            return f"{base}?<redacted>"
        return url

    netloc = parsed.hostname or ""
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    if parsed.username or parsed.password:
        netloc = f"<redacted>@{netloc}"

    query = "<redacted>" if parsed.query else ""
    return urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, query, "")
    )
