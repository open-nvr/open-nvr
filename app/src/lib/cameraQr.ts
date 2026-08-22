// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Turn a scanned camera QR code into the fields of an add/edit camera form.
 *
 * The QR from the OpenNVR Cam app (and from most IP cameras that print one)
 * encodes a single rtsp:// URL, which already carries everything the form
 * asks for separately: host, port, and — when the stream is protected —
 * credentials. Scanning used to drop the whole string into the RTSP URL box
 * and leave Camera Name, IP Address, Port, Username and Password empty, so
 * the operator retyped by hand what they had just scanned.
 *
 * Credentials are lifted OUT of the URL into their own fields rather than
 * left embedded. That matches what the form tells the operator ("no need to
 * put credentials in the URL — the Username/Password above are added
 * automatically"), keeps a scanned password visible and editable instead of
 * buried in a query-ish blob, and avoids handing the server a URL whose
 * credentials disagree with the fields next to it.
 *
 * Anything that is not an rtsp(s) URL is passed through untouched, which is
 * the behaviour scanning had before: a QR carrying some other payload still
 * lands in the RTSP URL box for the operator to deal with, and nothing else
 * on the form is disturbed.
 */
export type ScannedCamera = {
  /** Always set — the URL with any credentials stripped, or the raw text. */
  rtsp_url: string
  ip_address?: string
  port?: number
  username?: string
  password?: string
  /** Suggested display name; callers should not overwrite a name already typed. */
  name?: string
}

/** RTSP's IANA-assigned default, used when the URL carries no explicit port. */
const DEFAULT_RTSP_PORT = 554

export function parseCameraQr(text: string): ScannedCamera {
  const raw = text.trim()
  if (!raw) return { rtsp_url: raw }

  let url: URL
  try {
    url = new URL(raw)
  } catch {
    return { rtsp_url: raw }
  }

  // Only rtsp(s) is unpacked. A QR holding some other scheme would put a
  // non-RTSP URL in a field the server treats as a stream URL, so leave it
  // alone rather than half-filling the form from it.
  if (url.protocol !== 'rtsp:' && url.protocol !== 'rtsps:') {
    return { rtsp_url: raw }
  }

  // Hostname keeps IPv6 in brackets; the IP Address field holds a bare host.
  const host = url.hostname.replace(/^\[|\]$/g, '')
  if (!host) return { rtsp_url: raw }

  // The URL API percent-encodes the userinfo, so "p%40ss" must come back as
  // "p@ss" before it reaches the Password field.
  const username = safeDecode(url.username)
  const password = safeDecode(url.password)

  url.username = ''
  url.password = ''

  const port = url.port ? Number(url.port) : DEFAULT_RTSP_PORT

  return {
    rtsp_url: url.toString(),
    ip_address: host,
    port: Number.isFinite(port) && port > 0 ? port : DEFAULT_RTSP_PORT,
    ...(username ? { username } : {}),
    ...(password ? { password } : {}),
    name: `Camera ${host}`,
  }
}

// A malformed escape sequence throws; the literal is more useful than losing
// the credential entirely.
function safeDecode(value: string): string {
  if (!value) return ''
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}
