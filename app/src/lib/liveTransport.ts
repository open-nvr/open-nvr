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

/** After the bounded fast retries (2 s doubling to 30 s) a live tile keeps
 *  trying this often, for as long as it is on screen. A VPN that comes back
 *  after two minutes, a router reboot, a laptop lid: none of them should
 *  leave a tile parked on a "Retry" button. */
export const SLOW_RETRY_MS = 30000

/** The WebRTC frame watchdog: inbound video frames are counted this often,
 *  and a connection whose count has not moved for STALL_AFTER_MS is torn
 *  down and rebuilt. ICE can stay "connected" for a long time after a route
 *  change (consent checks still pass over the old path, or never fail at
 *  all over TCP) while no media arrives; the frame count is the truth. */
export const STALL_CHECK_MS = 3000
export const STALL_AFTER_MS = 8000

/** Before the first frame, a viewer that is receiving packets is waiting for
 *  a keyframe, not stalled: the browser can decode nothing until one comes,
 *  and MediaMTX cannot ask an RTSP camera for one. Cameras with a long
 *  I-frame interval or a "smart" codec (H.264+/H.265+) send one every 10 s
 *  or more, so with the 8 s rule above such a tile reconnected forever and
 *  only showed video when a reconnect happened to land just before a
 *  keyframe. This is how long it may wait for the first one. */
export const FIRST_FRAME_WAIT_MS = 30000

/** A tile that fell back to HLS probes WebRTC again this soon after the
 *  network changed (browser `online`, the tab shown again). */
export const RETURN_PROBE_DELAY_MS = 3000

/** A tile that had WebRTC working and then lost it (the reconnect after a
 *  drop did not connect either) falls back to HLS but probes again this
 *  soon, not after the full PROBE_INTERVAL_MS: a Wi-Fi hand-over or a VPN
 *  re-key is a blip, not a network that cannot do WebRTC. */
export const BLIP_PROBE_MS = 60 * 1000

/** Fired on `window` by a tile whose probe reached WebRTC: every other tile
 *  on HLS by fallback switches back at once instead of waiting for its own
 *  probe. */
export const WEBRTC_BACK_EVENT = 'opennvr:webrtc-back'

/** Fired on `window` by a tile whose probe could not even start because its
 *  own camera has no stream (WHEP 404, an error before ICE): that says
 *  nothing about the network, so another fallen-back tile whose picture is
 *  playing claims the probe (claimProbe) and runs it at once. */
export const PROBE_HANDOFF_EVENT = 'opennvr:probe-handoff'

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
 * on `localhost` reaches MediaMTX through Docker's loopback publication, and
 * `localhost` itself becomes 127.0.0.1 for the same reason: MediaMTX no
 * longer advertises its own loopback (webrtcIPsFromInterfaces is off), so
 * on a box with no advertised list this candidate is the only one a local
 * browser has.
 */
export function addressBarHost(hostname: string | null | undefined): string | null {
  if (!hostname) return null
  let h = hostname.trim()
  if (h.startsWith('[') && h.endsWith(']')) h = h.slice(1, -1)
  if (h.toLowerCase() === 'localhost') return '127.0.0.1'
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
 * - Every IP in the server's list (rtc-config: the admin's public address
 *   resolved, the STUN result, the launcher's LAN seed) on the same ports.
 *   MediaMTX normally advertises these itself, but a page opened on a domain
 *   name has no address-bar candidate, and MediaMTX's own list can be stale
 *   (restarted before core applied it); these candidates cover both.
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
  const addHost = (ip: string) => {
    add(ip, extras.icePort, 'udp')
    add(ip, extras.icePort, 'tcp')
    if (extras.publicIcePort && extras.publicIcePort !== extras.icePort) {
      add(ip, extras.publicIcePort, 'udp')
      add(ip, extras.publicIcePort, 'tcp')
    }
    if (extras.singlePortWebrtc) add(ip, pagePort, 'tcp')
  }
  // The page host first: it is proven reachable from this browser, and
  // each later candidate ranks one lower.
  if (pageHost) addHost(pageHost)
  for (const h of extras.serverHosts) {
    const ip = addressBarHost(h)
    if (ip && ip !== '127.0.0.1') addHost(ip)
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

/**
 * Stall detection over a frame counter. Feed it the decoded-frame count, the
 * time and the received-byte count at each check; it answers true once the
 * frame count has stood still for `afterMs`. The first call only records; a
 * count that never moves from its first value (connected, nothing ever
 * arrives) is a stall too.
 *
 * Until the first frame is decoded, bytes that keep arriving mean the
 * stream is up and the browser is waiting for a keyframe; that is allowed
 * up to `firstFrameMs` from the first check. Without a byte count (or with
 * one that stands still) the plain `afterMs` rule applies.
 */
export function makeStallTracker(
  afterMs: number = STALL_AFTER_MS,
  firstFrameMs: number = FIRST_FRAME_WAIT_MS,
): (frames: number, now: number, bytes?: number) => boolean {
  let last = -1
  let lastMoveAt = 0
  let startedAt = -1
  let lastBytes = -1
  let lastBytesAt = 0
  return (frames: number, now: number, bytes?: number) => {
    if (startedAt < 0) startedAt = now
    if (bytes !== undefined && bytes !== lastBytes) {
      lastBytes = bytes
      lastBytesAt = now
    }
    if (frames !== last) {
      last = frames
      lastMoveAt = now
      return false
    }
    if (frames === 0 && bytes !== undefined && now - lastBytesAt < afterMs) {
      return now - startedAt >= firstFrameMs
    }
    return now - lastMoveAt >= afterMs
  }
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

/** WebRTC did not connect: later tiles in this session start on HLS.
 *  `probeAfterMs` brings the next probe forward (BLIP_PROBE_MS after a
 *  connection that had worked); without it the earlier schedule stands. */
export function rememberHls(
  storage: StorageLike | null | undefined,
  reason: string,
  now: number,
  probeAfterMs?: number,
) {
  const prev = readMemory(storage)
  let probedAt = prev?.probedAt ?? now
  if (probeAfterMs !== undefined) {
    probedAt = Math.min(probedAt, now - PROBE_INTERVAL_MS + probeAfterMs)
  }
  writeMemory(storage, { mode: 'hls', reason, at: now, probedAt })
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

/** Take a handed-off probe (forgetProbe left it unclaimed): true for the
 *  first tile that asks, which records the probe; false for the rest. */
export function claimProbe(storage: StorageLike | null | undefined, now: number): boolean {
  const m = readMemory(storage)
  if (!m || m.probedAt !== 0) return false
  writeMemory(storage, { ...m, probedAt: now })
  return true
}

/**
 * What a new tile starts with. `preferred` is the caller's wish (every live
 * surface asks for WebRTC); the session's memory turns that into HLS. Once
 * per PROBE_INTERVAL_MS one tile is told to probe: it still STARTS on HLS
 * (a picture at once, never a black tile waiting out ICE) and tries WebRTC
 * at once in the background; when that connects, every fallen-back tile
 * switches. The probe is recorded here so a wall of tiles mounting together
 * runs one.
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
    return { start: 'hls', probing: true, fallenBack: true }
  }
  return { start: 'hls', probing: false, fallenBack: true }
}
