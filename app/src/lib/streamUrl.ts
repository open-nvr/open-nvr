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
