// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Add Camera → Manual → Browser webcam / Server webcam: one flow for both
 * kinds of webcam (the dialog's tab says which, as ``source``).
 *
 *   1. Source: this browser, or the OpenNVR server (a USB webcam MediaMTX
 *      or the host agent streams).
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

import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { AlertTriangle, CheckCircle, Loader2, Usb, Webcam } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { rebaseToCurrentOrigin } from '../lib/streamUrl'
import { describeStream } from '../lib/hostAgent'
import { sessionOnDevice, startSession, streamDevice, useWebcamSessions } from '../lib/webcamSessions'
import { HostWebcamPicker, useHostAgent } from './HostWebcamPicker'
import { VideoPlayer } from './VideoPlayer'
import { WebcamList, WebcamOption, webcamLabel } from './WebcamOption'
import { Badge } from './ui'
import { Input, Select } from './ui/form'
import { Switch } from './ui/Switch'

const RESOLUTIONS = [
  { label: '480p', width: 854, height: 480 },
  { label: '720p', width: 1280, height: 720 },
  { label: '1080p', width: 1920, height: 1080 },
]
const BROWSER_FPS = 15

/** The preview box: fixed height so the whole flow fits without scrolling. */
const PREVIEW_BOX = 'relative w-full h-[min(240px,32vh)] bg-black border border-[var(--border)] overflow-hidden'

type Source = 'browser' | 'server'

/** What each kind of webcam is, said once above the steps. */
const SOURCE_TEXT: Record<Source, string> = {
  browser: 'A camera on the device you are using now. It records while this browser tab is open.',
  server: 'A USB webcam plugged into the OpenNVR server. It records around the clock.',
}

/** What Add does, said next to the preview so nobody has to guess. */
const AFTER_ADD: Record<Source, string> = {
  browser: 'After you add it, this tab streams the webcam to OpenNVR, which records it. Keep the tab open; you can use the rest of OpenNVR meanwhile.',
  server: 'After you add it, the server streams the webcam at 25 fps and records it around the clock, also after restarts and re-plugging.',
}

type LocalWebcam = {
  id: string; name: string; node: string
  assigned_camera_id?: number | null; assigned_camera_name?: string | null
}

/** GET /api/v1/webcams/local, polled while the picker is open so a webcam
 *  plugged in during the dialog shows up. */
type LocalWebcams = {
  loading: boolean; available: boolean; webcams: LocalWebcam[]
  /** The server's OS as its launcher wrote it (linux/darwin/windows), or null. */
  hostOs: string | null
}

function useLocalWebcams(enabled: boolean): LocalWebcams {
  const [state, setState] = useState<LocalWebcams>({ loading: true, available: false, webcams: [], hostOs: null })
  useEffect(() => {
    if (!enabled) return
    let stop = false
    const tick = async () => {
      try {
        const { data } = await apiService.getLocalWebcams()
        if (!stop) setState({ loading: false, available: !!data?.available, webcams: data?.webcams ?? [], hostOs: data?.host_os ?? null })
      } catch {
        if (!stop) setState({ loading: false, available: false, webcams: [], hostOs: null })
      }
    }
    tick()
    const t = setInterval(tick, 4000)
    return () => { stop = true; clearInterval(t) }
  }, [enabled])
  return state
}

/** A Linux server with USB webcams switched off (WEBCAMS=off, or a kernel
 *  with no video4linux yet). The host agent is not the answer there —
 *  MediaMTX serves the webcam once WEBCAMS is on (#904). */
function WebcamsOffNotice() {
  return (
    <div className="p-3 border border-[var(--warn)] bg-[color-mix(in_oklab,var(--warn)_10%,transparent)] text-sm space-y-1">
      <div className="flex items-center gap-2 font-medium"><AlertTriangle size={14} className="text-[var(--warn)]" />
        USB webcams are off on the OpenNVR server
      </div>
      <div className="text-[var(--text-dim)]">
        Turn them on in the OpenNVR folder there: set this in <code className="font-mono">.env</code>, then run the second line.
      </div>
      <code className="block mt-1 px-2 py-1 text-xs font-mono bg-[var(--bg-2)] border border-[var(--border)] select-all break-all">WEBCAMS=on</code>
      <code className="block mt-1 px-2 py-1 text-xs font-mono bg-[var(--bg-2)] border border-[var(--border)] select-all break-all">./start.sh up</code>
      <div className="text-xs text-[var(--text-dim)]">
        Already on? The server's kernel had no video4linux when OpenNVR started: plug the webcam in, then run <code className="font-mono">./start.sh up</code> again.
      </div>
    </div>
  )
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
    <WebcamList>
      {webcams.map((w) => (
        <WebcamOption
          key={w.id}
          icon={<Usb size={14} />}
          name={w.name}
          subtitle={`USB · ${w.node}`}
          selected={value === w.id}
          takenBy={w.assigned_camera_id ? w.assigned_camera_name || `camera ${w.assigned_camera_id}` : null}
          onSelect={() => onChange(w.id, w.name)}
        />
      ))}
    </WebcamList>
  )
}

/** A numbered step: what to do here, in a few words. */
function Step({ n, title, children }: { n: number; title: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-2 text-xs font-medium text-[var(--text-dim)]">
        <span className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-[var(--border)] text-[10px]">{n}</span>
        {title}
      </div>
      {children}
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

export function WebcamAddPanel({ source, onDone, onError, onAction }: {
  /** Which kind of webcam: the dialog's Browser webcam / Server webcam tab. */
  source: Source
  onDone: (cameraId: number) => void
  onError: (msg: string | null) => void
  /** The panel's Add / Done, for the dialog to show in its footer. */
  onAction: (action: WebcamAction | null) => void
}) {
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
  // The camera the preview really opened ("Default camera" resolves to one).
  const [previewDevice, setPreviewDevice] = useState('')
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const handedOver = useRef(false)

  // Server source. Two ways a webcam on the OpenNVR machine streams: on
  // Linux MediaMTX serves it itself (the device is passed into its
  // container; GET /webcams/local lists them), elsewhere the host agent
  // does. The local list wins when it is available; the agent is the
  // fallback, and the only option on Docker Desktop. A Linux server never
  // falls back to the agent: there the answer is WEBCAMS=on (#904).
  const [serverDevice, setServerDevice] = useState<string | null>(null)
  const local = useLocalWebcams(source === 'server' && !created)
  const useAgent = !local.loading && !local.available && local.hostOs !== 'linux'
  const { info: agent } = useHostAgent(useAgent && (source === 'server' || created?.source === 'server'))

  const sessions = useWebcamSessions()
  const secure = typeof window !== 'undefined' && window.isSecureContext
  const hasMedia = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia

  const suggestName = (n: string) => { if (!nameTouched) setName(n) }

  const stopPreview = () => {
    if (!handedOver.current) previewRef.current?.getTracks().forEach((t) => t.stop())
    previewRef.current = null
    setPreviewReady(false)
    setPreviewDevice('')
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
      setPreviewDevice(streamDevice(stream))
      if (videoRef.current) { videoRef.current.srcObject = stream; videoRef.current.play().catch(() => {}) }
      const list = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput')
      setDevices(list)
      const label = stream.getVideoTracks()[0]?.label
      if (label) suggestName(webcamLabel(label))
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

  // This tab already publishes the previewed camera to another OpenNVR
  // camera: one webcam feeds one camera.
  const browserTakenBy = source === 'browser' ? sessionOnDevice(sessions, previewDevice) : undefined

  const canAdd = !busy && !created && name.trim() !== '' &&
    (source === 'browser' ? previewReady && !mediaError && !browserTakenBy
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
  // Three steps on the left (which webcam, its name, picture and sound),
  // the preview and what Add does on the right; the source on top says
  // which kind of webcam this is.
  const browserRows = devices.length ? devices : [{ deviceId: '', label: 'Default camera' } as MediaDeviceInfo]
  const shownDevice = deviceId || previewDevice
  return (
    <div className="space-y-3">
      <p className="text-xs leading-snug text-[var(--text-dim)]">{SOURCE_TEXT[source]}</p>

      <div className="grid sm:grid-cols-2 gap-4">
        <div className="space-y-3 min-w-0">
          <Step n={1} title={source === 'browser' ? 'Pick a camera on this device' : 'Pick a webcam on the server'}>
            {source === 'browser' ? (
              !secure || !hasMedia ? (
                <div className="text-sm text-[var(--danger)]">The browser opens cameras only on a secure page: use OpenNVR’s https:// address.</div>
              ) : (
                <WebcamList>
                  {browserRows.map((d, i) => {
                    const taken = sessionOnDevice(sessions, d.deviceId)
                    return (
                      <WebcamOption
                        key={d.deviceId || i}
                        icon={<Webcam size={14} />}
                        name={webcamLabel(d.label) || `Camera ${i + 1}`}
                        subtitle="On this device"
                        selected={shownDevice ? d.deviceId === shownDevice : i === 0}
                        takenBy={taken?.cameraName}
                        onSelect={() => setDeviceId(d.deviceId)}
                      />
                    )
                  })}
                </WebcamList>
              )
            ) : local.loading ? (
              <div className="text-sm text-[var(--text-dim)]">Looking for webcams on the server…</div>
            ) : local.available ? (
              <LocalWebcamPicker webcams={local.webcams} value={serverDevice} onChange={(id, n) => { setServerDevice(id); suggestName(n) }} />
            ) : local.hostOs === 'linux' ? (
              <WebcamsOffNotice />
            ) : (
              <HostWebcamPicker value={serverDevice} onChange={(id, n) => { setServerDevice(id); suggestName(n) }} />
            )}
            {browserTakenBy && (
              <div className="flex items-start gap-2 text-xs text-[var(--warn)]">
                <AlertTriangle size={14} className="shrink-0 mt-0.5" />
                <span>
                  This tab already streams this camera to “{browserTakenBy.cameraName}”. A webcam can feed one
                  camera: pick another one, or use “{browserTakenBy.cameraName}”.
                </span>
              </div>
            )}
          </Step>

          <Step n={2} title="Name the camera">
            <Input
              aria-label="Camera name"
              placeholder="e.g., Reception desk"
              value={name}
              onChange={(e) => { setName(e.target.value); setNameTouched(true) }}
            />
          </Step>

          {source === 'browser' && (
            <Step n={3} title="Picture and sound">
              <div className="flex items-center gap-4">
                <Select
                  aria-label="Picture quality"
                  className="!w-40 shrink-0"
                  value={resolution}
                  onChange={(e) => setResolution(e.target.value)}
                >
                  {RESOLUTIONS.map((r) => <option key={r.label} value={r.label}>{r.label} · {BROWSER_FPS} fps</option>)}
                </Select>
                <label className="flex items-center gap-2 text-sm whitespace-nowrap">
                  <Switch checked={audio} onChange={setAudio} label="Record sound" />
                  Record sound
                </label>
              </div>
            </Step>
          )}
        </div>

        <div className="space-y-1.5 min-w-0">
          <div className="flex items-center justify-between text-xs font-medium text-[var(--text-dim)]">
            <span>Preview</span>
            <Badge>Not recording yet</Badge>
          </div>
          <div className={PREVIEW_BOX}>
            {source === 'browser' ? (
              <>
                <video ref={videoRef} muted playsInline className="w-full h-full object-contain" />
                {mediaError && <div className="absolute inset-0 flex items-center justify-center p-3 text-center text-sm text-[var(--danger)] bg-black/80">{mediaError}</div>}
              </>
            ) : (
              <div className="absolute inset-0 flex items-center justify-center p-3 text-center text-xs text-white/60">
                The server webcam’s picture appears here once you add it.
              </div>
            )}
          </div>
          <div className="text-xs text-[var(--text-dim)]">{AFTER_ADD[source]}</div>
        </div>
      </div>
    </div>
  )
}
