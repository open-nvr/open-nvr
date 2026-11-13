import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import {
  ICE_GIVE_UP_MS,
  MEDIAMTX_HANDSHAKE_MS,
  PROBE_INTERVAL_MS,
  addressBarHost,
  candidateLines,
  clearMemory,
  forgetProbe,
  hostCandidates,
  initialTransport,
  insertCandidates,
  pagePortOf,
  parseExtras,
  readMemory,
  rememberHls,
  shouldDeleteSession,
  shouldGiveUp,
} from '../src/lib/liveTransport.ts'

const extras = (o = {}) => ({ icePort: 8189, publicIcePort: null, singlePortWebrtc: false, serverHosts: [], ...o })

class MemStorage {
  constructor() { this.m = new Map() }
  getItem(k) { return this.m.has(k) ? this.m.get(k) : null }
  setItem(k, v) { this.m.set(k, String(v)) }
  removeItem(k) { this.m.delete(k) }
}

// ---- address bar ---------------------------------------------------------

test('the address bar host is used only when it is an IP literal', () => {
  assert.equal(addressBarHost('192.168.2.51'), '192.168.2.51')
  assert.equal(addressBarHost('171.78.153.53'), '171.78.153.53')
  assert.equal(addressBarHost('[2001:db8::1]'), '2001:db8::1')
  assert.equal(addressBarHost('nvr.example.com'), null, 'domains are left to the server list')
  assert.equal(addressBarHost('localhost'), null)
  assert.equal(addressBarHost(''), null)
  assert.equal(addressBarHost(undefined), null)
})

test('loopback is kept (a page on localhost reaches MediaMTX through loopback); link-local is not', () => {
  assert.equal(addressBarHost('127.0.0.1'), '127.0.0.1')
  assert.equal(addressBarHost('[::1]'), '::1')
  assert.equal(addressBarHost('169.254.10.1'), null)
  assert.equal(addressBarHost('[fe80::1]'), null)
  assert.equal(addressBarHost('0.0.0.0'), null)
})

// ---- candidates ----------------------------------------------------------

test('the page host gets UDP and TCP on the ICE port, UDP first', () => {
  const c = hostCandidates({ pageHost: '192.168.2.51', pagePort: 443, extras: extras() })
  assert.deepEqual(c, [
    { ip: '192.168.2.51', port: 8189, proto: 'udp' },
    { ip: '192.168.2.51', port: 8189, proto: 'tcp' },
  ])
})

test('a public ICE port mapped differently on the router is tried too', () => {
  const c = hostCandidates({ pageHost: '171.78.153.53', pagePort: 13443, extras: extras({ publicIcePort: 18189 }) })
  assert.deepEqual(c.map((x) => `${x.proto}:${x.port}`), ['udp:8189', 'tcp:8189', 'udp:18189', 'tcp:18189'])
})

test('single-port mode adds TCP on the page port for the page host and every server IP', () => {
  const c = hostCandidates({
    pageHost: '171.78.153.53',
    pagePort: 13443,
    extras: extras({ singlePortWebrtc: true, serverHosts: ['192.168.2.51', 'nvr.example.com', '171.78.153.53'] }),
  })
  const tcp443 = c.filter((x) => x.port === 13443).map((x) => x.ip)
  assert.deepEqual(tcp443, ['171.78.153.53', '192.168.2.51'], 'deduplicated; the domain is skipped')
  assert.equal(c.filter((x) => x.port === 8189).length, 2)
})

test('without single-port mode the server list adds nothing (MediaMTX advertises it itself)', () => {
  const c = hostCandidates({ pageHost: null, pagePort: 443, extras: extras({ serverHosts: ['192.168.2.51'] }) })
  assert.deepEqual(c, [])
})

test('candidate lines are well-formed, UDP above TCP, each one lower than the last', () => {
  const lines = candidateLines(hostCandidates({ pageHost: '10.0.0.5', pagePort: 443, extras: extras({ singlePortWebrtc: true }) }))
  assert.equal(lines.length, 3)
  assert.match(lines[0], /^candidate:onvr1 1 udp 2130706431 10\.0\.0\.5 8189 typ host$/)
  assert.match(lines[1], /^candidate:onvr2 1 tcp \d+ 10\.0\.0\.5 8189 typ host tcptype passive$/)
  assert.match(lines[2], /^candidate:onvr3 1 tcp \d+ 10\.0\.0\.5 443 typ host tcptype passive$/)
  const prio = (l) => Number(l.split(' ')[3])
  assert.ok(prio(lines[0]) > prio(lines[1]) && prio(lines[1]) > prio(lines[2]))
})

test('pagePortOf reads the explicit port and falls back to the scheme default', () => {
  assert.equal(pagePortOf({ port: '13443', protocol: 'https:' }), 13443)
  assert.equal(pagePortOf({ port: '', protocol: 'https:' }), 443)
  assert.equal(pagePortOf({ port: '', protocol: 'http:' }), 80)
})

test('parseExtras tolerates an older core that sends none of it', () => {
  assert.deepEqual(parseExtras({ iceServers: [] }), extras())
  assert.deepEqual(parseExtras({ icePort: 18189, publicIcePort: 70000, singlePortWebrtc: 'yes', serverHosts: ['a', 1] }),
    extras({ icePort: 18189, publicIcePort: null, singlePortWebrtc: false, serverHosts: ['a'] }))
})

// ---- SDP insertion -------------------------------------------------------

const ANSWER = [
  'v=0', 'o=- 1 1 IN IP4 0.0.0.0', 's=-', 't=0 0', 'a=group:BUNDLE 0 1',
  'm=video 9 UDP/TLS/RTP/SAVPF 96', 'c=IN IP4 0.0.0.0', 'a=mid:0', 'a=ice-ufrag:abc', 'a=ice-pwd:def',
  'a=candidate:1 1 udp 2130706431 172.28.0.5 8189 typ host', 'a=end-of-candidates',
  'm=audio 9 UDP/TLS/RTP/SAVPF 111', 'c=IN IP4 0.0.0.0', 'a=mid:1',
].join('\r\n') + '\r\n'

test('candidates go into the first m-section, before end-of-candidates, keeping CRLF', () => {
  const out = insertCandidates(ANSWER, ['candidate:onvr1 1 udp 2130706430 192.168.2.51 8189 typ host'])
  const rows = out.split('\r\n')
  const i = rows.indexOf('a=candidate:onvr1 1 udp 2130706430 192.168.2.51 8189 typ host')
  assert.ok(i > rows.indexOf('a=ice-pwd:def'))
  assert.equal(rows[i + 1], 'a=end-of-candidates')
  assert.ok(i < rows.findIndex((r) => r.startsWith('m=audio')))
  assert.ok(out.endsWith('\r\n'))
  assert.ok(!out.includes('\n\n'))
})

test('without an end-of-candidates marker they go at the end of the first section', () => {
  const sdp = ANSWER.replace('a=end-of-candidates\r\n', '')
  const rows = insertCandidates(sdp, ['candidate:x 1 tcp 1 10.0.0.1 443 typ host tcptype passive']).split('\r\n')
  const i = rows.indexOf('a=candidate:x 1 tcp 1 10.0.0.1 443 typ host tcptype passive')
  assert.equal(rows[i + 1], 'm=audio 9 UDP/TLS/RTP/SAVPF 111')
})

test('nothing to insert, or no media section, leaves the SDP untouched', () => {
  assert.equal(insertCandidates(ANSWER, []), ANSWER)
  assert.equal(insertCandidates('v=0\r\n', ['candidate:x 1 udp 1 1.2.3.4 1 typ host']), 'v=0\r\n')
})

// ---- decisions -----------------------------------------------------------

test('give up on failed, or when the deadline passes without connected', () => {
  assert.equal(shouldGiveUp('failed', 100), true)
  assert.equal(shouldGiveUp('connecting', ICE_GIVE_UP_MS - 1), false)
  assert.equal(shouldGiveUp('connecting', ICE_GIVE_UP_MS), true)
  assert.equal(shouldGiveUp('new', ICE_GIVE_UP_MS), true)
  assert.equal(shouldGiveUp('connected', ICE_GIVE_UP_MS * 2), false)
})

test('no DELETE for a never-connected session MediaMTX has already dropped', () => {
  assert.equal(shouldDeleteSession(true, MEDIAMTX_HANDSHAKE_MS * 5), true)
  assert.equal(shouldDeleteSession(false, 2000), true)
  assert.equal(shouldDeleteSession(false, MEDIAMTX_HANDSHAKE_MS), false)
})

// ---- memory --------------------------------------------------------------

test('a fresh session starts on WebRTC; after a fallback later tiles start on HLS', () => {
  const s = new MemStorage()
  const now = 1_000_000
  assert.deepEqual(initialTransport('webrtc', true, s, now), { start: 'webrtc', probing: false, fallenBack: false })
  rememberHls(s, 'ice-timeout', now)
  assert.deepEqual(initialTransport('webrtc', true, s, now + 1000), { start: 'hls', probing: false, fallenBack: true })
  assert.equal(readMemory(s).reason, 'ice-timeout')
})

test('one tile probes WebRTC again after the interval, and only one', () => {
  const s = new MemStorage()
  const t0 = 1_000_000
  rememberHls(s, 'ice-failed', t0)
  const later = t0 + PROBE_INTERVAL_MS
  assert.deepEqual(initialTransport('webrtc', true, s, later), { start: 'webrtc', probing: true, fallenBack: false })
  assert.deepEqual(initialTransport('webrtc', true, s, later + 10), { start: 'hls', probing: false, fallenBack: true })
})

test('coming back online forces a probe; a successful connection clears everything', () => {
  const s = new MemStorage()
  rememberHls(s, 'ice-timeout', 5000)
  forgetProbe(s)
  assert.equal(initialTransport('webrtc', true, s, 6000).probing, true)
  clearMemory(s)
  assert.equal(readMemory(s), null)
  assert.deepEqual(initialTransport('webrtc', true, s, 7000), { start: 'webrtc', probing: false, fallenBack: false })
})

test('a manual HLS preference, or no HLS URL, bypasses the memory', () => {
  const s = new MemStorage()
  rememberHls(s, 'x', 1)
  assert.deepEqual(initialTransport('hls', true, s, 2), { start: 'hls', probing: false, fallenBack: false })
  assert.deepEqual(initialTransport('webrtc', false, s, 2), { start: 'webrtc', probing: false, fallenBack: false })
})

test('corrupt or missing storage is treated as no memory', () => {
  const s = new MemStorage()
  s.setItem('opennvr.live.transport', '{nope')
  assert.equal(readMemory(s), null)
  assert.equal(readMemory(null), null)
  assert.doesNotThrow(() => rememberHls(null, 'x', 1))
})

// ---- no hardcoded ICE servers anywhere in the app ----------------------------

test('no STUN or TURN host is hardcoded in app/src (ICE servers come from Settings only)', () => {
  const root = new URL('../src', import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, '$1')
  const offenders = []
  const walk = (dir) => {
    for (const name of readdirSync(dir)) {
      const p = join(dir, name)
      if (statSync(p).isDirectory()) walk(p)
      else if (/\.(ts|tsx)$/.test(name)) {
        // Placeholders on RFC 2606 example domains are fine; a real server is not.
        const hits = readFileSync(p, 'utf8').match(/\b(?:stun|turns?):[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?::\d+)?\b/g) || []
        if (hits.some((h) => !/\.example(?:\.(?:org|com|net))?(?::\d+)?$/.test(h.replace(/^(?:stun|turns?):/, '')))) offenders.push(p)
      }
    }
  }
  walk(root)
  assert.deepEqual(offenders, [])
})
