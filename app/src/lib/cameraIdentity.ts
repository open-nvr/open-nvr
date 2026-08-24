// Copyright (c) 2026 OpenNVR
// Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)

/**
 * Keeping a camera's identity fields and its RTSP URL agreeing.
 *
 * A camera's address, port and credentials are captured twice: as discrete
 * form fields, and again inside the RTSP URL, which embeds all three. Nothing
 * used to keep the two in step, so the edit dialog could save a camera whose
 * fields described one device and whose URL pointed at another — fields saying
 * 192.168.29.226 while the stream MediaMTX actually pulled came from
 * 192.168.1.18.
 *
 * The rule here: the control the operator edited drives, and the rest follow.
 * Nothing is "corrected" behind their back — which matters, because the field
 * that looks stale is not reliably the wrong one. A camera can be streaming
 * happily from the URL's host while its ip_address column holds the bogus value.
 *
 * Everything in this module is pure: no React, no network. Deriving a URL for a
 * moved camera rewrites the host in place and keeps the path, rather than
 * re-probing ONVIF — a subnet move almost never changes /Streaming/Channels/101,
 * and a probe cannot sit in a form-blur handler.
 */

/** Scheme defaults for an rtsp(s) URL that omits the port (RFC 7826). */
const RTSP_DEFAULT_PORTS: Record<string, number> = { 'rtsp:': 554, 'rtsps:': 322 }

/** The controls that can drive a sync. Editing one updates the others. */
export type IdentityField = 'ip_address' | 'port' | 'username' | 'password' | 'rtsp_url'

/**
 * The subset of a camera form this module touches. Declared structurally so
 * both the add and the edit dialogs can pass their own form state through
 * without having to share a type.
 */
export interface CameraIdentityFields {
  ip_address: string
  port: number
  username?: string
  password?: string
  rtsp_url?: string
  substream_url?: string
}

interface ParsedRtsp {
  hostname: string
  port: number
  username: string
  password: string
}

/** Percent-decode, falling back to the raw value on a malformed sequence. */
function safeDecode(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value
  }
}

/**
 * Parse an rtsp(s) URL into its identity parts, or null when there is nothing
 * trustworthy to read — a half-typed paste, or an http/onvif URL that this
 * module has no business rewriting.
 */
export function parseRtspUrl(url: string | null | undefined): ParsedRtsp | null {
  if (!url) return null
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return null
  }
  const fallback = RTSP_DEFAULT_PORTS[parsed.protocol]
  if (fallback === undefined) return null

  let port = fallback
  if (parsed.port) {
    const n = Number(parsed.port)
    if (!Number.isInteger(n) || n < 1 || n > 65535) return null
    port = n
  }
  return {
    // IPv6 arrives bracketed from the URL parser; the server validates the
    // bare address, so hand back what it expects.
    hostname: parsed.hostname.replace(/^\[|\]$/g, ''),
    port,
    username: safeDecode(parsed.username),
    password: safeDecode(parsed.password),
  }
}

/**
 * The port a camera actually streams on, read off its RTSP URL — the same rule
 * the server applies on save (`rtsp_port_from_url` in camera_source_resolver.py:
 * rtsp defaults to 554, rtsps to 322 when the URL omits it). null when there is
 * nothing to read, so callers fall back to the stored value rather than showing
 * a guess.
 */
export function rtspPortFromUrl(url: string | null | undefined): number | null {
  return parseRtspUrl(url)?.port ?? null
}

/**
 * Rewrite only the named parts of an rtsp(s) URL, leaving everything else —
 * crucially the path and query — exactly as it was. Returns the URL unchanged
 * when it cannot be parsed, so a URL mid-paste is never clobbered.
 *
 * Credentials are plaintext at this boundary and get percent-encoded on the way
 * in, matching the server's `quote(..., safe='')`. Passing an already-encoded
 * value would double-encode a literal '%'.
 */
export function applyIdentityToUrl(
  url: string | null | undefined,
  patch: { hostname?: string; port?: number; username?: string; password?: string },
): string | null | undefined {
  if (!url) return url
  let parsed: URL
  try {
    parsed = new URL(url)
  } catch {
    return url
  }
  if (RTSP_DEFAULT_PORTS[parsed.protocol] === undefined) return url

  if (patch.hostname) {
    parsed.hostname = patch.hostname
    // A hostname the parser rejects leaves the URL untouched rather than
    // silently producing one that points somewhere else.
    if (!parsed.hostname) return url
  }
  if (patch.port !== undefined) {
    parsed.port = String(patch.port)
  }
  if (patch.username !== undefined) {
    parsed.username = patch.username ? encodeURIComponent(patch.username) : ''
    // Clearing the user clears the whole userinfo, mirroring the server's
    // replace_credentials: a URL with a password but no user is meaningless.
    if (!patch.username) parsed.password = ''
  }
  if (patch.password !== undefined && patch.password !== '') {
    parsed.password = encodeURIComponent(patch.password)
  }
  return parsed.toString()
}

/**
 * Return `form` with every identity field brought back into agreement after
 * `changed` was edited. Pure — the caller assigns the result to state.
 *
 * Two guards are load-bearing:
 *
 * 1. A blank Password box means "keep the existing password", NOT "no
 *    password". It must never strip the URL's credentials, so a blank value
 *    syncs nothing. Password therefore flows out of the box only when the
 *    operator types one, and into it only when a pasted URL supplies one.
 * 2. A URL carrying no userinfo leaves the username and password fields alone.
 *    Pasting a bare `rtsp://host/path` means "the stream moved", not "this
 *    camera has no credentials".
 */
export function syncCameraIdentity<T extends CameraIdentityFields>(
  form: T,
  changed: IdentityField,
): T {
  if (changed === 'rtsp_url') {
    const parsed = parseRtspUrl(form.rtsp_url)
    if (!parsed) return form
    const next: T = {
      ...form,
      ip_address: parsed.hostname || form.ip_address,
      port: parsed.port,
    }
    if (parsed.username) next.username = parsed.username
    if (parsed.password) next.password = parsed.password
    return next
  }

  if (changed === 'ip_address') {
    if (!form.ip_address) return form
    return {
      ...form,
      rtsp_url: applyIdentityToUrl(form.rtsp_url, { hostname: form.ip_address }) ?? form.rtsp_url,
      // The substream is a second feed on the same device, so it moves too —
      // but it keeps its own port and path, which routinely differ.
      substream_url:
        applyIdentityToUrl(form.substream_url, { hostname: form.ip_address }) ?? form.substream_url,
    }
  }

  if (changed === 'port') {
    if (!form.port) return form
    return {
      ...form,
      rtsp_url: applyIdentityToUrl(form.rtsp_url, { port: form.port }) ?? form.rtsp_url,
    }
  }

  if (changed === 'username') {
    return {
      ...form,
      rtsp_url:
        applyIdentityToUrl(form.rtsp_url, { username: form.username ?? '' }) ?? form.rtsp_url,
      substream_url:
        applyIdentityToUrl(form.substream_url, { username: form.username ?? '' }) ??
        form.substream_url,
    }
  }

  // changed === 'password' — blank means keep, so it is not a sync trigger.
  if (!form.password) return form
  return {
    ...form,
    rtsp_url: applyIdentityToUrl(form.rtsp_url, { password: form.password }) ?? form.rtsp_url,
    substream_url:
      applyIdentityToUrl(form.substream_url, { password: form.password }) ?? form.substream_url,
  }
}
