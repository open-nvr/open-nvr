// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * How a live tile decides the way its video travels (issue #925).
 *
 * One ladder, climbed top-down, stopping at the first rung that works:
 *
 *   1. WebRTC over UDP on the ICE port       (LAN, VPN, or the port is forwarded)
 *   2. WebRTC over TCP on the ICE port       (UDP blocked)
 *   3. WebRTC over TCP on the page's own port (single-port mode: nginx hands
 *      non-TLS traffic on 443 to MediaMTX)
 *   4. Low-latency HLS over the page's port  (anything that can load the page)
 *
 * Rungs 1-3 are ICE candidates offered together; the browser checks them all
 * at once and keeps the best that answers. Rung 4 is what the tile switches
 * to when none of them does.
 *
 * The addresses come from two places. MediaMTX advertises the server's own
 * list (LAN IP, STUN-discovered public IP, the admin's address). On top of
 * that the player adds the address in the browser's address bar: the page
 * just loaded from it, so it is proven reachable from here, whatever `.env`
 * or the server list says. That candidate only steers this viewer's own
 * browser, so nothing is stored or shared.
 *
 * Everything here is pure so the node tests in app/tests can cover it; the
 * player (components/VideoPlayer) owns the timers and the RTCPeerConnection.
 */

/** Give WebRTC this long after the answer to reach `connected` before the
 *  tile switches to HLS. TCP candidates through a proxy need more than the
 *  5 s a LAN connection takes. */
export const ICE_GIVE_UP_MS = 8000

/** MediaMTX drops a session whose ICE never completes after this long
 *  (webrtcHandshakeTimeout). A DELETE for an older never-connected session
 *  can only 404. */
export const MEDIAMTX_HANDSHAKE_MS = 10000

/** Once a browser session has fallen back to HLS, probe WebRTC again this
 *  often (and when the browser comes back online). One tile per probe. */
export const PROBE_INTERVAL_MS = 15 * 60 * 1000

/** How long the viewer waits for its own candidates before the WHEP POST.
 *  Without STUN gathering finishes in well under 100 ms. */
export const GATHER_WAIT_MS = 500

export const MEMORY_KEY = 'opennvr.live.transport'

export type Proto = 'udp' | 'tcp'

export type HostCandidate = { ip: string; port: number; proto: Proto }

/** What /webrtc/rtc-config says beyond the ICE servers. */
export type RtcExtras = {
  icePort: number
  publicIcePort: number | null
  singlePortWebrtc: boolean
  serverHosts: string[]
}

export const DEFAULT_EXTRAS: RtcExtras = {
  icePort: 8189,
  publicIcePort: null,
  singlePortWebrtc: false,
  serverHosts: [],
}

/** Read the extras out of an rtc-config response; anything missing keeps the
 *  default, so an older core still works. */
export function parseExtras(data: any): RtcExtras {
  const num = (v: any) => (typeof v === 'number' && Number.isInteger(v) && v > 0 && v < 65536 ? v : null)
  const icePort = num(data?.icePort) ?? DEFAULT_EXTRAS.icePort
  return {
    icePort,
    publicIcePort: num(data?.publicIcePort),
    singlePortWebrtc: data?.singlePortWebrtc === true,
    serverHosts: Array.isArray(data?.serverHosts) ? data.serverHosts.filter((h: any) => typeof h === 'string') : [],
  }
}

const V4 = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/

export function isIPv4(s: string): boolean {
  const m = V4.exec(s)
  return !!m && m.slice(1).every((o) => Number(o) <= 255)
}

export function isIPv6(s: string): boolean {
  if (!s.includes(':') || /[^0-9a-fA-F:.]/.test(s)) return false
  const parts = s.split('::')
  if (parts.length > 2) return false
  const groups = parts.flatMap((p) => (p === '' ? [] : p.split(':')))
  if (groups.some((g) => g.length > 4 && !g.includes('.'))) return false
  return parts.length === 2 ? groups.length <= 7 : groups.length === 8
}

/**
 * The address-bar host as an ICE candidate address, or null when it cannot
 * be one. Only IP literals: browsers do not reliably resolve a domain name
 * in a remote candidate (the server list covers domains). Link-local and
 * unspecified addresses are useless; loopback is kept, because a page opened
 * on `localhost` reaches MediaMTX through Docker's loopback publication.
 */
export function addressBarHost(hostname: string | null | undefined): string | null {
  if (!hostname) return null
  let h = hostname.trim()
  if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1)
  if (isIPv4(h)) {
    if (h.startsWith('169.254.') || h === '0.0.0.0') return null
    return h
  }
  if (isIPv6(h)) {
    const low = h.toLowerCase()
    if (low === '::' || /^fe[89ab]/.test(low)) return null
    return low
  }
  return null
}

/**
 * The extra host candidates the player adds to MediaMTX's answer.
 *
 * - The address-bar host on the ICE port (UDP + TCP), on the public ICE port
 *   when that differs, and in single-port mode on the page's own port (TCP).
 * - Every IP in the server's list on the page's port (TCP, single-port mode
 *   only): MediaMTX already advertises those on the ICE port itself.
 */
export function hostCandidates(opts: {
  pageHost: string | null
  pagePort: number
  extras: RtcExtras
}): HostCandidate[] {
  const { pageHost, pagePort, extras } = opts
  const out: HostCandidate[] = []
  const seen = new Set<string>()
  const add = (ip: string, port: number, proto: Proto) => {
    const key = `${ip}|${port}|${proto}`
    if (seen.has(key) || !(port > 0 && port < 65536)) return
    seen.add(key)
    out.push({ ip, port, proto })
  }
  if (pageHost) {
    add(pageHost, extras.icePort, 'udp')
    add(pageHost, extras.icePort, 'tcp')
    if (extras.publicIcePort && extras.publicIcePort !== extras.icePort) {
      add(pageHost, extras.publicIcePort, 'udp')
      add(pageHost, extras.publicIcePort, 'tcp')
    }
    if (extras.singlePortWebrtc) add(pageHost, pagePort, 'tcp')
  }
  if (extras.singlePortWebrtc) {
    for (const h of extras.serverHosts) {
      const ip = addressBarHost(h)
      if (ip) add(ip, pagePort, 'tcp')
    }
  }
  return out
}

/** The page's port as a number (location.port is '' for the default). */
export function pagePortOf(loc: { port?: string; protocol?: string }): number {
  const p = Number(loc.port)
  if (p > 0) return p
  return loc.protocol === 'http:' ? 80 : 443
}

// Type preference 126 (host) in the top byte; UDP above TCP so ICE prefers
// the datagram path whenever both answer. Each later candidate is one lower
// so the order above is also the order ICE tries them in.
const UDP_BASE = 2130706431
const TCP_BASE = 1694498815

/** `a=candidate` attribute values (without the `a=` prefix) for the SDP. */
export function candidateLines(cands: HostCandidate[]): string[] {
  return cands.map((c, i) => {
    const prio = (c.proto === 'udp' ? UDP_BASE : TCP_BASE) - i
    const foundation = `onvr${i + 1}`
    const tail = c.proto === 'tcp' ? ' tcptype passive' : ''
    return `candidate:${foundation} 1 ${c.proto} ${prio} ${c.ip} ${c.port} typ host${tail}`
  })
}

/**
 * Insert candidate lines into the first media section of an SDP answer,
 * before its `a=end-of-candidates` when there is one, so browsers that stop
 * listening at that marker still see them. The stream is bundled, so the
 * first section carries ICE for all of it. Keeps the SDP's own line endings.
 */
export function insertCandidates(sdp: string, lines: string[]): string {
  if (!lines.length || !sdp) return sdp
  const eol = sdp.includes('\r\n') ? '\r\n' : '\n'
  const rows = sdp.split(/\r?\n/)
  const firstM = rows.findIndex((r) => r.startsWith('m='))
  if (firstM < 0) return sdp
  let sectionEnd = rows.findIndex((r, i) => i > firstM && r.startsWith('m='))
  if (sectionEnd < 0) sectionEnd = rows.length
  // Drop a trailing empty row inside the section so we never insert after it.
  while (sectionEnd > firstM + 1 && rows[sectionEnd - 1] === '') sectionEnd--
  let at = rows.findIndex((r, i) => i > firstM && i < sectionEnd && r === 'a=end-of-candidates')
  if (at < 0) at = sectionEnd
  const inserted = lines.map((l) => `a=${l}`)
  rows.splice(at, 0, ...inserted)
  let out = rows.join(eol)
  if (!out.endsWith(eol)) out += eol
  return out
}

// ---- decisions -----------------------------------------------------------

/** Give up on this WebRTC attempt: ICE failed, or it has had its time. */
export function shouldGiveUp(connectionState: string, elapsedMs: number): boolean {
  if (connectionState === 'connected' || connectionState === 'connecting' && elapsedMs < ICE_GIVE_UP_MS) return false
  if (connectionState === 'failed' || connectionState === 'closed') return true
  return elapsedMs >= ICE_GIVE_UP_MS
}

/** Worth sending the WHEP DELETE? Not for a session that never connected and
 *  that MediaMTX has already timed out: it can only answer 404. */
export function shouldDeleteSession(everConnected: boolean, ageMs: number): boolean {
  return everConnected || ageMs < MEDIAMTX_HANDSHAKE_MS
}

// ---- per-session memory --------------------------------------------------

export type TransportMemory = {
  mode: 'hls'
  reason: string
  at: number
  /** When a WebRTC probe last started; 0 forces one on the next mount. */
  probedAt: number
}

type StorageLike = { getItem(k: string): string | null; setItem(k: string, v: string): void; removeItem(k: string): void }

export function readMemory(storage: StorageLike | null | undefined): TransportMemory | null {
  try {
    const raw = storage?.getItem(MEMORY_KEY)
    if (!raw) return null
    const v = JSON.parse(raw)
    if (v?.mode !== 'hls' || typeof v.at !== 'number') return null
    return { mode: 'hls', reason: String(v.reason ?? ''), at: v.at, probedAt: typeof v.probedAt === 'number' ? v.probedAt : v.at }
  } catch {
    return null
  }
}

function writeMemory(storage: StorageLike | null | undefined, m: TransportMemory | null) {
  try {
    if (!m) storage?.removeItem(MEMORY_KEY)
    else storage?.setItem(MEMORY_KEY, JSON.stringify(m))
  } catch {
    // Storage can be unavailable (private windows, blocked site data). The
    // tile still works; it just forgets between mounts.
  }
}

/** WebRTC did not connect: later tiles in this session start on HLS. */
export function rememberHls(storage: StorageLike | null | undefined, reason: string, now: number) {
  const prev = readMemory(storage)
  writeMemory(storage, { mode: 'hls', reason, at: now, probedAt: prev?.probedAt ?? now })
}

/** WebRTC connected: forget the fallback. */
export function clearMemory(storage: StorageLike | null | undefined) {
  writeMemory(storage, null)
}

/** The network changed (browser `online`): the next tile probes WebRTC. */
export function forgetProbe(storage: StorageLike | null | undefined) {
  const m = readMemory(storage)
  if (m) writeMemory(storage, { ...m, probedAt: 0 })
}

/**
 * What a new tile starts with. `preferred` is the caller's wish (every live
 * surface asks for WebRTC); the session's memory can turn that into HLS,
 * except once per PROBE_INTERVAL_MS, when one tile tries WebRTC again. The
 * probe is recorded here so a wall of tiles mounting together runs one.
 */
export function initialTransport(
  preferred: 'webrtc' | 'hls',
  hasHls: boolean,
  storage: StorageLike | null | undefined,
  now: number,
): { start: 'webrtc' | 'hls'; probing: boolean; fallenBack: boolean } {
  if (preferred !== 'webrtc' || !hasHls) return { start: preferred, probing: false, fallenBack: false }
  const m = readMemory(storage)
  if (!m) return { start: 'webrtc', probing: false, fallenBack: false }
  if (m.probedAt === 0 || now - m.probedAt >= PROBE_INTERVAL_MS) {
    writeMemory(storage, { ...m, probedAt: now })
    return { start: 'webrtc', probing: true, fallenBack: false }
  }
  return { start: 'hls', probing: false, fallenBack: true }
}
