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
import { SessionExpiredError, WhipPublisher, type PublisherState, type WhipSession } from './whipPublisher'

export type WebcamSettings = { deviceId: string; resolution: string; audio: boolean }

export type WebcamSession = {
  cameraId: number
  cameraName: string
  stream: MediaStream
  settings: WebcamSettings
  state: PublisherState
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

async function iceConfig(): Promise<RTCConfiguration> {
  // Same source as the WHEP player: Settings → WebRTC, with a public STUN
  // fallback if that read fails.
  try {
    const { data } = await apiService.getWebRTCClientConfig()
    return {
      iceServers: Array.isArray(data?.iceServers) && data.iceServers.length
        ? data.iceServers
        : [{ urls: ['stun:stun.l.google.com:19302'] }],
      ...(data?.iceTransportPolicy ? { iceTransportPolicy: data.iceTransportPolicy } : {}),
    }
  } catch {
    return { iceServers: [{ urls: ['stun:stun.l.google.com:19302'] }] }
  }
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
      throw new Error(extractApiError(e, 'Could not reach OpenNVR'))
    }
  }
  const s: Internal = {
    cameraId, cameraName, stream, settings,
    state: { kind: 'connecting', attempt: 0 },
    publisher: null as unknown as WhipPublisher,
  }
  s.publisher = new WhipPublisher(getSession, iceConfig, (state) => {
    if (sessions.get(cameraId) !== s) return
    s.state = state
    emit()
  })
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
