// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Publish a local MediaStream into a webcam camera's MediaMTX path over
 * WHIP — the mirror of the WHEP player in components/VideoPlayer.
 *
 * Each (re)connect asks the server for a fresh WHIP session (URL + a
 * publish-only token that lives a few minutes): MediaMTX checks the token
 * only when the publisher connects, so a session outlives its token, and
 * a reconnect simply gets a new one.
 *
 * Reconnects are part of the job. Wi-Fi drops, the server restarts,
 * MediaMTX reloads. MediaMTX keeps one publisher per path
 * (overridePublisher: no), so right after a drop it still holds the dead
 * session and answers the new POST with 400 "someone is already
 * publishing" until that session times out — that is retried like any
 * other transient failure, never shown as fatal.
 */

import { resolveWhepSessionUrl, rebaseToCurrentOrigin } from './streamUrl'

export type WhipSession = { whip_url: string; token: string }

export type PublisherState =
  | { kind: 'idle' }
  | { kind: 'connecting'; attempt: number }
  | { kind: 'live' }
  | { kind: 'retrying'; attempt: number; inSeconds: number; reason: string }
  | { kind: 'error'; reason: string }

export class NoH264Error extends Error {
  constructor() {
    super(
      'This browser cannot send H.264 video, which OpenNVR records. ' +
        'Use a current Chrome, Edge, Firefox or Safari.',
    )
  }
}

/** Raised by getSession when the user's own session is gone (401/403). */
export class SessionExpiredError extends Error {}

/** Raised by getSession when the camera no longer exists (404): it was
 *  deleted, here or anywhere else. Publishing ends instead of retrying. */
export class CameraGoneError extends Error {}

/**
 * Put H.264 first (packetization-mode=1 variants ahead of the rest) and
 * drop everything else, so the browser cannot fall back to VP8: recording
 * (fMP4), HLS and the detection pipeline are all built around H.264.
 * Returns false when the browser offers no H.264 at all.
 */
export function preferH264(transceiver: RTCRtpTransceiver): boolean {
  const caps = typeof RTCRtpSender !== 'undefined' ? RTCRtpSender.getCapabilities?.('video') : null
  if (!caps || typeof transceiver.setCodecPreferences !== 'function') {
    // Can't steer the codec (very old browser): let the offer decide and
    // hope for H.264 rather than refusing outright.
    return true
  }
  const h264 = caps.codecs.filter((c) => c.mimeType.toLowerCase() === 'video/h264')
  if (h264.length === 0) return false
  const mode1 = (c: RTCRtpCodec) => /packetization-mode=1/.test(c.sdpFmtpLine || '')
  const ordered = [...h264.filter(mode1), ...h264.filter((c) => !mode1(c))]
  // RTX/RED/FEC entries keep retransmission working; they are not codecs
  // the stream can be encoded in.
  const helpers = caps.codecs.filter((c) => /\/(rtx|red|ulpfec|flexfec-03)$/i.test(c.mimeType))
  transceiver.setCodecPreferences([...ordered, ...helpers])
  return true
}

const BACKOFF_SECONDS = [1, 2, 4, 8, 15, 30]

/** Optional help from the app (lib/webcamSessions): extra ICE candidates
 *  for MediaMTX's answer, and the media port to name when none connects. */
export type PublisherExtras = {
  augmentAnswer?: (sdp: string) => string
  mediaPort?: () => number
}

/** ICE failed this many times without ever connecting: most likely the
 *  media port is not reachable from this network, and the retry message
 *  says so. Retrying goes on regardless (at the longest backoff), because
 *  the same signal also comes from a MediaMTX restart or a network that is
 *  still coming up, and a page left open should recover from those by
 *  itself, as the viewer side does. */
const ICE_FAILURES_BEFORE_NAMING_THE_PORT = 2

export class WhipPublisher {
  private pc: RTCPeerConnection | null = null
  private resource: string | null = null
  private token: string | null = null
  private attempt = 0
  private iceFailures = 0
  private everLive = false
  private retryTimer: number | null = null
  private stopped = true
  private stream: MediaStream | null = null

  constructor(
    private readonly getSession: () => Promise<WhipSession>,
    private readonly getIceConfig: () => Promise<RTCConfiguration>,
    private readonly onState: (s: PublisherState) => void,
    private readonly extras: PublisherExtras = {},
  ) {}

  start(stream: MediaStream) {
    this.stream = stream
    this.stopped = false
    this.attempt = 0
    this.iceFailures = 0
    this.everLive = false
    void this.connect()
  }

  /** Stop publishing and end the WHIP session (frees the path at once). */
  stop() {
    this.stopped = true
    if (this.retryTimer !== null) {
      window.clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
    this.teardown(false)
    this.onState({ kind: 'idle' })
  }

  /**
   * End the session from a page that is going away: a normal fetch is
   * cancelled with the page, a keepalive one is allowed to finish.
   */
  stopOnUnload() {
    this.stopped = true
    this.teardown(true)
  }

  private teardown(keepalive: boolean) {
    const pc = this.pc
    this.pc = null
    if (pc) {
      pc.onconnectionstatechange = null
      pc.close()
    }
    const resource = this.resource
    this.resource = null
    if (resource) {
      const headers: Record<string, string> = {}
      if (this.token) headers.Authorization = `Bearer ${this.token}`
      fetch(resource, { method: 'DELETE', headers, keepalive }).catch(() => {})
    }
  }

  private scheduleRetry(reason: string) {
    if (this.stopped) return
    this.teardown(false)
    const inSeconds = BACKOFF_SECONDS[Math.min(this.attempt, BACKOFF_SECONDS.length - 1)]
    this.attempt += 1
    this.onState({ kind: 'retrying', attempt: this.attempt, inSeconds, reason })
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null
      void this.connect()
    }, inSeconds * 1000)
  }

  private async connect() {
    if (this.stopped || !this.stream) return
    this.onState({ kind: 'connecting', attempt: this.attempt })
    let session: WhipSession
    try {
      session = await this.getSession()
    } catch (e: any) {
      if (e instanceof SessionExpiredError) {
        this.stopped = true
        this.onState({ kind: 'error', reason: 'Sign in again to keep publishing.' })
        return
      }
      if (e instanceof CameraGoneError) {
        this.stopped = true
        this.onState({ kind: 'error', reason: 'This camera was deleted.' })
        return
      }
      this.scheduleRetry(e?.message || 'Could not reach OpenNVR')
      return
    }
    if (this.stopped) return

    const pc = new RTCPeerConnection(await this.getIceConfig())
    this.pc = pc
    this.token = session.token
    let codecOk = true
    for (const track of this.stream.getTracks()) {
      const tr = pc.addTransceiver(track, { direction: 'sendonly', streams: [this.stream] })
      if (track.kind === 'video') codecOk = preferH264(tr)
    }
    if (!codecOk) {
      this.stopped = true
      this.teardown(false)
      this.onState({ kind: 'error', reason: new NoH264Error().message })
      return
    }

    pc.onconnectionstatechange = () => {
      if (this.pc !== pc) return
      if (pc.connectionState === 'connected') {
        this.attempt = 0
        this.iceFailures = 0
        this.everLive = true
        this.onState({ kind: 'live' })
      } else if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
        // Publishing has no HLS to fall back to. ICE failing before it ever
        // connected usually means the media port cannot be reached from
        // here: say so, slow the retries down to the longest backoff, and
        // keep trying (a MediaMTX restart looks the same and heals).
        if (!this.everLive && ++this.iceFailures >= ICE_FAILURES_BEFORE_NAMING_THE_PORT) {
          const port = this.extras.mediaPort?.()
          this.attempt = Math.max(this.attempt, BACKOFF_SECONDS.length - 1)
          this.scheduleRetry(
            'Real-time video cannot reach OpenNVR from this network' +
              (port ? ` (UDP/TCP port ${port} may be blocked)` : '') +
              '. Publishing a browser webcam needs that port; ask your administrator. Still trying.',
          )
          return
        }
        this.scheduleRetry('Connection lost')
      } else if (pc.connectionState === 'disconnected') {
        // Often heals on its own; give ICE a moment before starting over.
        window.setTimeout(() => {
          if (this.pc === pc && pc.connectionState === 'disconnected') {
            this.scheduleRetry('Connection lost')
          }
        }, 5000)
      }
    }

    try {
      const offer = await pc.createOffer()
      await pc.setLocalDescription(offer)
      // Gather candidates first: MediaMTX takes them in the offer, which
      // spares a trickle-ICE round of PATCH requests through the proxy.
      await waitForIceGathering(pc, 2000)
      const url = rebaseToCurrentOrigin(session.whip_url) || session.whip_url
      const resp = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp', Authorization: `Bearer ${session.token}` },
        body: pc.localDescription?.sdp || offer.sdp || '',
      })
      if (this.pc !== pc) return
      if (resp.status !== 200 && resp.status !== 201) {
        const text = (await resp.text().catch(() => '')).toLowerCase()
        // 400 right after a drop is MediaMTX still holding the old session.
        const reason = text.includes('already publishing') || text.includes('publisher')
          ? 'Waiting for the previous connection to time out'
          : `Server answered ${resp.status}`
        this.scheduleRetry(reason)
        return
      }
      this.resource = resolveWhepSessionUrl(resp.headers.get('Location'), url)
      const answer = await resp.text()
      await pc.setRemoteDescription({ type: 'answer', sdp: this.extras.augmentAnswer?.(answer) ?? answer })
    } catch (e: any) {
      if (this.pc === pc) this.scheduleRetry(e?.message || 'Connection failed')
    }
  }
}

function waitForIceGathering(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve()
  return new Promise((resolve) => {
    const done = () => {
      pc.removeEventListener('icegatheringstatechange', check)
      window.clearTimeout(timer)
      resolve()
    }
    const check = () => {
      if (pc.iceGatheringState === 'complete') done()
    }
    const timer = window.setTimeout(done, timeoutMs)
    pc.addEventListener('icegatheringstatechange', check)
  })
}
