// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Rebase a backend-provided stream URL onto the origin the UI is
 * currently being served from.
 *
 * The backend builds WHEP/HLS URLs from MEDIAMTX_EXTERNAL_BASE_URL,
 * which start.sh pins to ONE origin — the detected LAN IP (e.g.
 * https://192.168.1.4/webrtc/cam-1/whep). nginx, however, proxies
 * /webrtc/ and /hls/ identically on EVERY origin it serves: localhost,
 * the LAN IP, a hostname, a VPN address. A browser on
 * https://localhost that receives the absolute LAN-IP URL makes a
 * cross-origin request to a host whose self-signed cert it has never
 * accepted, and the fetch fails silently — live view shows NO LINK on
 * https://localhost while working on the LAN URL.
 *
 * Fix: if the URL's path is one nginx proxies for streaming, ignore
 * the backend's choice of host and use window.location.origin. This is
 * a no-op when the user is already on the pinned origin, and it makes
 * every origin nginx serves equally valid. Non-proxied URLs (direct
 * MediaMTX ports, absolute URLs to other services) pass through
 * untouched, as does anything that fails to parse.
 *
 * /playback/get and /playback/list are the recording-playback
 * endpoints nginx proxies the same way (exact-match locations). The
 * backend returns absolute playback_url values built from the same
 * pinned base; today the SPA only truthiness-checks them, but any
 * consumer that starts playing them must go through here too.
 */
const PROXIED_STREAM_PREFIXES = ['/webrtc/', '/hls/', '/playback/get', '/playback/list']

export function rebaseToCurrentOrigin(url: string | undefined): string | undefined {
  if (!url || typeof window === 'undefined') return url
  try {
    const parsed = new URL(url, window.location.origin)
    if (!PROXIED_STREAM_PREFIXES.some((p) => parsed.pathname.startsWith(p))) {
      return url
    }
    return new URL(parsed.pathname + parsed.search + parsed.hash, window.location.origin).toString()
  } catch {
    return url
  }
}

/**
 * Resolve the WHEP session URL from the POST response's Location header.
 *
 * MediaMTX answers with a path relative to its own root
 * (/cam-1/whep/<session>), but the browser reaches it through a proxy
 * that strips a prefix (nginx and the Vite dev proxy both map
 * /webrtc/cam-1/whep → /cam-1/whep). Used as-is, the Location resolves
 * to /cam-1/whep/<session> on the SPA origin, which is core, not
 * MediaMTX: the teardown DELETE gets a 405 and the session lingers until
 * MediaMTX's ICE timeout. Put the stripped prefix back by matching the
 * session's parent path against the tail of the URL we POSTed to.
 */
export function resolveWhepSessionUrl(location: string | null, whepUrl: string): string | null {
  if (!location) return null
  try {
    const base = new URL(whepUrl, typeof window === 'undefined' ? undefined : window.location.origin)
    if (location.startsWith('/') && !location.startsWith('//')) {
      const parent = location.slice(0, location.lastIndexOf('/'))
      const whepPath = base.pathname.replace(/\/+$/, '')
      if (parent && whepPath.endsWith(parent)) {
        return new URL(whepPath.slice(0, whepPath.length - parent.length) + location, base).toString()
      }
    }
    return new URL(location, base).toString()
  } catch {
    return null
  }
}
