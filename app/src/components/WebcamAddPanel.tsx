// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Add Camera → Manual → Webcam: one flow for both kinds of webcam.
 *
 *   1. Source: this browser, or the OpenNVR server (a USB webcam the host
 *      agent streams).
 *   2. Pick a webcam from the list — the browser's own cameras, or the ones
 *      the agent found — with a preview in a fixed-height box.
 *   3. Name it, Add.
 *   4. The dialog stays open and shows the camera's real stream coming up
 *      ("Starting the stream…" → "Live and recording"), then Done.
 *
 * A browser webcam keeps publishing after the dialog closes (it is handed to
 * lib/webcamSessions; the header shows it). A server webcam needs nothing
 * more: the host agent picks the camera up on its next sync.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { CheckCircle, Loader2, Mic, MicOff, Monitor, Server, Webcam } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { rebaseToCurrentOrigin } from '../lib/streamUrl'
import { describeStream } from '../lib/hostAgent'
import { startSession, useWebcamSessions } from '../lib/webcamSessions'
import { HostWebcamPicker, useHostAgent } from './HostWebcamPicker'
import { VideoPlayer } from './VideoPlayer'
import { Button } from './ui'
import { Field, Input } from './ui/form'
import { SegmentedControl } from './ui/SegmentedControl'

const RESOLUTIONS = [
  { label: '480p', width: 854, height: 480 },
  { label: '720p', width: 1280, height: 720 },
  { label: '1080p', width: 1920, height: 1080 },
]
const BROWSER_FPS = 15

/** The preview box: fixed height so the whole flow fits without scrolling. */
const PREVIEW_BOX = 'relative w-full h-[min(240px,32vh)] bg-black border border-[var(--border)] overflow-hidden'

type Source = 'browser' | 'server'

const SOURCE_HINT: Record<Source, string> = {
  browser: 'The camera of the device you are using now. Records while this browser tab stays open.',
  server: 'A USB webcam plugged into the computer OpenNVR runs on. Records around the clock.',
}

type LocalWebcam = { id: string; name: string; node: string }

/** GET /api/v1/webcams/local, polled while the picker is open so a webcam
 *  plugged in during the dialog shows up. */
function useLocalWebcams(enabled: boolean): { loading: boolean; available: boolean; webcams: LocalWebcam[] } {
  const [state, setState] = useState<{ loading: boolean; available: boolean; webcams: LocalWebcam[] }>({ loading: true, available: false, webcams: [] })
  useEffect(() => {
    if (!enabled) return
    let stop = false
    const tick = async () => {
      try {
        const { data } = await apiService.getLocalWebcams()
        if (!stop) setState({ loading: false, available: !!data?.available, webcams: data?.webcams ?? [] })
      } catch {
        if (!stop) setState({ loading: false, available: false, webcams: [] })
      }
    }
    tick()
    const t = setInterval(tick, 4000)
    return () => { stop = true; clearInterval(t) }
  }, [enabled])
  return state
}

function LocalWebcamPicker({ webcams, value, onChange }: { webcams: LocalWebcam[]; value: string | null; onChange: (id: string, name: string) => void }) {
  if (webcams.length === 0) {
    return (
      <div className="text-sm text-[var(--text-dim)]">
        No USB webcam is plugged into the OpenNVR server right now. Plug one in; it appears here within a few seconds.
      </div>
    )
  }
  return (
    <div className="space-y-1" role="radiogroup" aria-label="Webcam">
      {webcams.map((w) => {
        const selected = value === w.id
        return (
          <button
            key={w.id}
            type="button"
            role="radio"
            aria-checked={selected}
            onClick={() => onChange(w.id, w.name)}
            className={`w-full text-left px-3 py-2 border flex items-center gap-2 text-sm ${selected ? 'border-[var(--accent)] bg-[var(--accent)]/10' : 'border-[var(--border)] hover:border-[var(--accent)]'}`}
          >
            {selected ? <CheckCircle size={14} className="text-[var(--accent)]" /> : <Webcam size={14} className="text-[var(--text-dim)]" />}
            <span className="truncate">{w.name}</span>
            <span className="ml-auto text-xs text-[var(--text-dim)] font-mono">{w.node}</span>
          </button>
        )
      })}
    </div>
  )
}

function SourceSwitch({ value, onChange }: { value: Source; onChange: (s: Source) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-3">
      <SegmentedControl
        label="Where is the webcam?"
        options={[
          { value: 'browser' as Source, label: <span className="inline-flex items-center gap-1.5 px-1"><Monitor size={13} /> This browser</span> },
          { value: 'server' as Source, label: <span className="inline-flex items-center gap-1.5 px-1"><Server size={13} /> OpenNVR server</span> },
        ]}
        value={value}
        onChange={onChange}
      />
      <span className="text-xs text-[var(--text-dim)] min-w-0 flex-1">{SOURCE_HINT[value]}</span>
    </div>
  )
}

/** The camera's real stream through OpenNVR, once it exists. */
function LivePreview({ cameraId, offlineLabel, version }: { cameraId: number; offlineLabel: string; version: number }) {
  const [info, setInfo] = useState<{ whep?: string; hls?: string; token?: string } | null>(null)
  const load = useCallback(async () => {
    try {
      const { data } = await apiService.getStreamInfo(cameraId)
      setInfo({ whep: rebaseToCurrentOrigin(data?.urls?.webrtc), hls: rebaseToCurrentOrigin(data?.urls?.hls), token: data?.token })
    } catch {
      setInfo(null)
    }
  }, [cameraId])
  useEffect(() => { void load() }, [load])
  if (!info) {
    return <div className="absolute inset-0 flex items-center justify-center"><Loader2 className="animate-spin text-white/60" /></div>
  }
  return (
    <VideoPlayer
      key={version}
      mode="live"
      chrome="none"
      whepUrl={info.whep}
      hlsUrl={info.hls}
      mediamtxToken={info.token}
      onAuthExpired={() => void load()}
      preferredStreamType="webrtc"
      autoPlay
      muted
      offlineLabel={offlineLabel}
      className="w-full h-full !bg-black"
    />
  )
}

export type WebcamAction = { label: string; onClick: () => void; disabled: boolean }

export function WebcamAddPanel({ onDone, onError, onAction }: {
  onDone: (cameraId: number) => void
  onError: (msg: string | null) => void
  /** The panel's Add / Done, for the dialog to show in its footer. */
  onAction: (action: WebcamAction | null) => void
}) {
  const [source, setSource] = useState<Source>('browser')
  const [name, setName] = useState('')
  const [nameTouched, setNameTouched] = useState(false)
  const [busy, setBusy] = useState(false)
  const [created, setCreated] = useState<{ id: number; source: Source } | null>(null)

  // Browser source
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [deviceId, setDeviceId] = useState('')
  const [resolution, setResolution] = useState('720p')
  const [audio, setAudio] = useState(false)
  const [mediaError, setMediaError] = useState<string | null>(null)
  const previewRef = useRef<MediaStream | null>(null)
  const [previewReady, setPreviewReady] = useState(false)
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const handedOver = useRef(false)

  // Server source. Two ways a webcam on the OpenNVR machine streams: on
  // Linux MediaMTX serves it itself (the device is passed into its
  // container; GET /webcams/local lists them), elsewhere the host agent
  // does. The local list wins when it is available; the agent is the
  // fallback, and the only option on Docker Desktop.
  const [serverDevice, setServerDevice] = useState<string | null>(null)
  const local = useLocalWebcams(source === 'server' && !created)
  const useAgent = !local.loading && !local.available
  const { info: agent } = useHostAgent(useAgent && (source === 'server' || created?.source === 'server'))

  const sessions = useWebcamSessions()
  const secure = typeof window !== 'undefined' && window.isSecureContext
  const hasMedia = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia

  const suggestName = (n: string) => { if (!nameTouched) setName(n) }

  const stopPreview = () => {
    if (!handedOver.current) previewRef.current?.getTracks().forEach((t) => t.stop())
    previewRef.current = null
    setPreviewReady(false)
  }

  // The browser preview: (re)open the chosen camera with the chosen settings.
  useEffect(() => {
    if (source !== 'browser' || created || !secure || !hasMedia) return
    let cancelled = false
    const res = RESOLUTIONS.find((r) => r.label === resolution) ?? RESOLUTIONS[1]
    setMediaError(null)
    navigator.mediaDevices.getUserMedia({
      video: {
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
        width: { ideal: res.width }, height: { ideal: res.height },
        frameRate: { ideal: BROWSER_FPS, max: BROWSER_FPS },
      },
      audio,
    }).then(async (stream) => {
      if (cancelled) { stream.getTracks().forEach((t) => t.stop()); return }
      stopPreview()
      previewRef.current = stream
      setPreviewReady(true)
      if (videoRef.current) { videoRef.current.srcObject = stream; videoRef.current.play().catch(() => {}) }
      const list = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput')
      setDevices(list)
      const label = stream.getVideoTracks()[0]?.label
      if (label) suggestName(label.replace(/\s*\([0-9a-f]{4}:[0-9a-f]{4}\)\s*$/i, ''))
    }, (e: any) => {
      if (cancelled) return
      setMediaError(
        e?.name === 'NotAllowedError'
          ? 'Camera access was blocked. Allow it in the browser’s site settings.'
          : e?.name === 'NotReadableError'
          ? 'The camera is in use by another app (a video call?).'
          : e?.message || 'Could not open the camera',
      )
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [source, deviceId, resolution, audio, created, secure, hasMedia])

  // Leaving the dialog (or switching to the server) releases a preview that
  // was not handed to a session.
  useEffect(() => { if (source !== 'browser') stopPreview() }, [source])
  useEffect(() => () => stopPreview(), [])

  const canAdd = !busy && !created && name.trim() !== '' &&
    (source === 'browser' ? previewReady && !mediaError
      : !!serverDevice && (local.available ? true : !!agent?.connected))

  const add = async () => {
    setBusy(true)
    onError(null)
    try {
      const res = await apiService.createCamera({
        name: name.trim(),
        source_type: 'publisher',
        ...(source === 'server' ? { source_device: serverDevice } : {}),
      })
      const id = res?.data?.id as number | undefined
      if (!id) throw new Error('The camera was not created')
      if (res?.data?.mediamtx_provisioned !== true) {
        await apiService.provisionCameraMediaMTX(id, { enable_recording: true }).catch(() => {})
      }
      if (source === 'browser' && previewRef.current) {
        handedOver.current = true // the session owns the stream now
        startSession(id, name.trim(), previewRef.current, { deviceId, resolution, audio })
      }
      setCreated({ id, source })
    } catch (e) {
      onError(extractApiError(e, 'Failed to add the webcam'))
    } finally {
      setBusy(false)
    }
  }

  // ── After Add: the camera's own stream coming up ──
  const session = created ? sessions.find((s) => s.cameraId === created.id) : undefined
  const agentStream = created ? agent?.streams.find((s) => s.camera_id === created.id) : undefined
  const live = created?.source === 'browser' ? session?.state.kind === 'live' : agentStream?.state === 'live'
  // Remount the player when the source goes live, rather than waiting for its
  // slow offline poll.
  const [version, setVersion] = useState(0)
  const wasLive = useRef(false)
  useEffect(() => {
    if (live && !wasLive.current) setVersion((v) => v + 1)
    wasLive.current = !!live
  }, [live])

  const actionRef = useRef<() => void>(() => {})
  actionRef.current = created ? () => onDone(created.id) : () => void add()
  const actionLabel = created ? 'Done' : busy ? 'Adding…' : 'Add webcam'
  const actionDisabled = created ? false : !canAdd
  useEffect(() => {
    onAction({ label: actionLabel, disabled: actionDisabled, onClick: () => actionRef.current() })
  }, [actionLabel, actionDisabled]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => onAction(null), []) // eslint-disable-line react-hooks/exhaustive-deps

  if (created) {
    const statusText = live
      ? 'Live and recording.'
      : created.source === 'browser'
        ? session?.state.kind === 'error' ? session.state.reason : 'Starting the stream from this browser…'
        : local.available ? 'The OpenNVR server is starting the webcam — usually a few seconds; the preview shows it.'
        : agent && !agent.connected ? 'The host agent on the OpenNVR server is not running.'
        : agentStream ? `${describeStream(agentStream)} — usually a few seconds.` : 'The OpenNVR server is picking the webcam up — usually a few seconds.'
    return (
      <div className="space-y-3">
        <div className={PREVIEW_BOX}>
          <LivePreview cameraId={created.id} version={version} offlineLabel={live ? 'Connecting…' : 'Webcam starting…'} />
        </div>
        <div className="flex items-center gap-2 text-sm">
          {live ? <CheckCircle size={16} className="text-[var(--ok)]" /> : <Loader2 size={16} className="animate-spin text-[var(--text-dim)]" />}
          <span><span className="font-medium">{name}</span> added. {statusText}</span>
        </div>
        {created.source === 'browser' && (
          <div className="text-xs text-[var(--text-dim)]">
            It keeps streaming while you use OpenNVR in this tab (the header shows it). Closing or reloading the tab stops it.
          </div>
        )}
      </div>
    )
  }

  // ── Before Add ──
  return (
    <div className="space-y-3">
      <SourceSwitch value={source} onChange={(s) => { if (!busy) { setSource(s); onError(null) } }} />

      <div className="grid sm:grid-cols-2 gap-3">
        <div className="space-y-2 min-w-0">
          <div className="text-xs text-[var(--text-dim)]">Webcam</div>
          {source === 'browser' ? (
            !secure || !hasMedia ? (
              <div className="text-sm text-[var(--danger)]">The browser opens cameras only on a secure page: use OpenNVR’s https:// address.</div>
            ) : (
              <div className="space-y-1" role="radiogroup" aria-label="Webcam">
                {(devices.length ? devices : [{ deviceId: '', label: 'Default camera' } as MediaDeviceInfo]).map((d, i) => {
                  const selected = deviceId ? d.deviceId === deviceId : i === 0
                  return (
                    <button
                      key={d.deviceId || i}
                      type="button"
                      role="radio"
                      aria-checked={selected}
                      onClick={() => setDeviceId(d.deviceId)}
                      className={`w-full text-left px-3 py-2 border flex items-center gap-2 text-sm ${selected ? 'border-[var(--accent)] bg-[var(--accent)]/10' : 'border-[var(--border)] hover:border-[var(--accent)]'}`}
                    >
                      {selected ? <CheckCircle size={14} className="text-[var(--accent)]" /> : <Webcam size={14} className="text-[var(--text-dim)]" />}
                      <span className="truncate">{d.label || `Camera ${i + 1}`}</span>
                    </button>
                  )
                })}
              </div>
            )
          ) : local.loading ? (
            <div className="text-sm text-[var(--text-dim)]">Looking for webcams on the server…</div>
          ) : local.available ? (
            <LocalWebcamPicker webcams={local.webcams} value={serverDevice} onChange={(id, n) => { setServerDevice(id); suggestName(n) }} />
          ) : (
            <HostWebcamPicker value={serverDevice} onChange={(id, n) => { setServerDevice(id); suggestName(n) }} />
          )}

          <Field label="Camera name" required className="pt-1">
            <Input
              placeholder="e.g., Reception desk"
              value={name}
              onChange={(e) => { setName(e.target.value); setNameTouched(true) }}
            />
          </Field>

          {source === 'browser' && (
            <div className="flex items-center gap-2">
              <select
                aria-label="Quality"
                className="bg-[var(--bg-2)] border border-[var(--border)] px-2 py-1.5 text-sm"
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
              >
                {RESOLUTIONS.map((r) => <option key={r.label} value={r.label}>{r.label} · {BROWSER_FPS} fps</option>)}
              </select>
              <Button size="sm" onClick={() => setAudio((a) => !a)} aria-pressed={audio} title={audio ? 'The microphone is recorded' : 'The microphone is off'}>
                {audio ? <Mic size={14} /> : <MicOff size={14} />} {audio ? 'Sound on' : 'Sound off'}
              </Button>
            </div>
          )}
        </div>

        <div className="space-y-1 min-w-0">
          <div className="text-xs text-[var(--text-dim)]">Preview</div>
          <div className={PREVIEW_BOX}>
            {source === 'browser' ? (
              <>
                <video ref={videoRef} muted playsInline className="w-full h-full object-contain" />
                {mediaError && <div className="absolute inset-0 flex items-center justify-center p-3 text-center text-sm text-[var(--danger)] bg-black/80">{mediaError}</div>}
              </>
            ) : (
              <div className="absolute inset-0 flex items-center justify-center p-3 text-center text-xs text-white/60">
                The server webcam’s picture appears here once it is added and starts streaming.
              </div>
            )}
          </div>
          <div className="text-[11px] text-[var(--text-dim)]">
            {source === 'browser'
              ? 'Records only while this tab stays open; you can keep using OpenNVR in it.'
              : 'Streams at 25 fps, around the clock, also after restarts and re-plugging.'}
          </div>
        </div>
      </div>

    </div>
  )
}
