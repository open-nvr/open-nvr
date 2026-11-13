// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Browser webcams being published from this tab, for the whole app session.
 *
 * The publisher page starts a session and hands it the camera stream; the
 * session — not the page — owns it from then on. Moving to Live View,
 * Recordings or any other page in the same tab keeps publishing (the obvious
 * thing to do right after Start is to go and look at the camera), and the
 * header shows it (components/WebcamPublishingIndicator). What ends it: Stop
 * (on the page or in the header), closing or reloading the tab, or signing
 * out.
 */

import { useSyncExternalStore } from 'react'
import { apiService } from './apiService'
import { extractApiError } from './apiError'
import { CameraGoneError, SessionExpiredError, WhipPublisher, type PublisherState, type WhipSession } from './whipPublisher'
import {
  DEFAULT_EXTRAS,
  addressBarHost,
  candidateLines,
  hostCandidates,
  insertCandidates,
  pagePortOf,
  parseExtras,
  type RtcExtras,
} from './liveTransport'

export type WebcamSettings = { deviceId: string; resolution: string; audio: boolean }

export type WebcamSession = {
  cameraId: number
  cameraName: string
  stream: MediaStream
  settings: WebcamSettings
  /** The physical camera the stream really comes from (settings.deviceId
   *  may be '' for "Default camera"). */
  device: string
  state: PublisherState
}

/** The device a stream's video comes from, as the browser names it. */
export function streamDevice(stream: MediaStream | null | undefined): string {
  return stream?.getVideoTracks()[0]?.getSettings().deviceId ?? ''
}

/** The session in this tab already publishing `device`, other than
 *  `exceptCameraId`'s own. One webcam feeds one camera: a second camera on
 *  it is the same picture recorded twice. */
export function sessionOnDevice(
  sessions: WebcamSession[], device: string, exceptCameraId?: number,
): WebcamSession | undefined {
  if (!device) return undefined
  return sessions.find((s) => s.device === device && s.cameraId !== exceptCameraId)
}

type Internal = WebcamSession & { publisher: WhipPublisher }

const sessions = new Map<number, Internal>()
const listeners = new Set<() => void>()
let snapshot: WebcamSession[] = []
let wakeLock: any = null

function emit() {
  snapshot = [...sessions.values()].map(({ publisher: _p, ...s }) => ({ ...s }))
  listeners.forEach((l) => l())
}

// The ICE port and the server's address list from the last rtc-config read
// (lib/liveTransport); shared by every session in the tab.
let rtcExtras: RtcExtras = DEFAULT_EXTRAS

async function iceConfig(): Promise<RTCConfiguration> {
  // Same source as the WHEP player: Settings → WebRTC, and nothing else.
  // MediaMTX's advertised addresses plus the address-bar host (below) are
  // what connect; a STUN server is contacted only if the admin set one.
  try {
    const { data } = await apiService.getWebRTCClientConfig()
    rtcExtras = parseExtras(data)
    return {
      iceServers: Array.isArray(data?.iceServers) ? data.iceServers : [],
      ...(data?.iceTransportPolicy ? { iceTransportPolicy: data.iceTransportPolicy } : {}),
    }
  } catch {
    return { iceServers: [] }
  }
}

// The same address-bar candidates the WHEP player adds: the page loaded
// from that address, so MediaMTX is reachable there from this browser.
const publisherExtras = {
  augmentAnswer: (sdp: string) => insertCandidates(sdp, candidateLines(hostCandidates({
    pageHost: addressBarHost(window.location.hostname),
    pagePort: pagePortOf(window.location),
    extras: rtcExtras,
  }))),
  mediaPort: () => rtcExtras.publicIcePort ?? rtcExtras.icePort,
}

// A wake lock keeps a laptop from sleeping mid-recording. One for all
// sessions; the browser drops it when the tab is hidden, so take it again.
async function holdWakeLock() {
  if (wakeLock || sessions.size === 0) return
  try { wakeLock = await (navigator as any).wakeLock?.request?.('screen') } catch { /* optional */ }
  wakeLock?.addEventListener?.('release', () => { wakeLock = null })
}

function releaseWakeLock() {
  wakeLock?.release?.().catch(() => {})
  wakeLock = null
}

let wired = false
function wireOnce() {
  if (wired || typeof window === 'undefined') return
  wired = true
  // Closing or reloading the tab ends every session at once, so the next
  // publisher is not refused while MediaMTX times the old one out.
  window.addEventListener('pagehide', () => {
    sessions.forEach((s) => s.publisher.stopOnUnload())
  })
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void holdWakeLock()
  })
}

/** Start (or restart, with a new stream or settings) publishing a camera.
 *  The session takes ownership of the stream. */
export function startSession(cameraId: number, cameraName: string, stream: MediaStream, settings: WebcamSettings) {
  wireOnce()
  const old = sessions.get(cameraId)
  if (old) {
    old.publisher.stop()
    if (old.stream !== stream) old.stream.getTracks().forEach((t) => t.stop())
  }
  const getSession = async (): Promise<WhipSession> => {
    try {
      const { data } = await apiService.getWhipSession(cameraId)
      return data
    } catch (e: any) {
      if (e?.status === 401 || e?.status === 403) throw new SessionExpiredError()
      if (e?.status === 404) {
        // Deleted (in another tab, on another device): a session retrying
        // for ever would keep the webcam open, its light on, for nothing.
        window.setTimeout(() => stopSession(cameraId), 0)
        throw new CameraGoneError()
      }
      throw new Error(extractApiError(e, 'Could not reach OpenNVR'))
    }
  }
  const s: Internal = {
    cameraId, cameraName, stream, settings,
    device: streamDevice(stream) || settings.deviceId,
    state: { kind: 'connecting', attempt: 0 },
    publisher: null as unknown as WhipPublisher,
  }
  s.publisher = new WhipPublisher(getSession, iceConfig, (state) => {
    if (sessions.get(cameraId) !== s) return
    s.state = state
    emit()
  }, publisherExtras)
  sessions.set(cameraId, s)
  s.publisher.start(stream)
  emit()
  void holdWakeLock()
}

/** Stop publishing a camera and release its camera/microphone. */
export function stopSession(cameraId: number) {
  const s = sessions.get(cameraId)
  if (!s) return
  sessions.delete(cameraId)
  s.publisher.stop()
  s.stream.getTracks().forEach((t) => t.stop())
  if (sessions.size === 0) releaseWakeLock()
  emit()
}

/** Stop everything (signing out). */
export function stopAllSessions() {
  ;[...sessions.keys()].forEach(stopSession)
}

function subscribe(l: () => void) {
  listeners.add(l)
  return () => { listeners.delete(l) }
}

/** Every session in this tab, live. */
export function useWebcamSessions(): WebcamSession[] {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot)
}
