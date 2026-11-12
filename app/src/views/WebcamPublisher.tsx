// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Publish this device's camera into a webcam camera (/cameras/:cameraId/publish).
 *
 * The browser is the camera: once Started, its webcam is pushed over WHIP
 * into the camera's MediaMTX path, where it is recorded, shown in Live View
 * and analysed like any other camera. The session lives in
 * lib/webcamSessions, not in this page: moving to another page of OpenNVR in
 * this tab keeps publishing (the header shows it), closing the tab stops it —
 * said plainly on the page, because a recorder that silently stops is the
 * worst surprise.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlertTriangle, Camera as CameraIcon, Loader2, Mic, MicOff, Play, Square } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import type { PublisherState } from '../lib/whipPublisher'
import { startSession, stopSession, useWebcamSessions } from '../lib/webcamSessions'
import { Badge, Button, Card, CardContent, EmptyState, PageHeader } from '../components/ui'

const RESOLUTIONS = [
  { label: '480p', width: 854, height: 480 },
  { label: '720p', width: 1280, height: 720 },
  { label: '1080p', width: 1920, height: 1080 },
]
// 15 fps: enough for recording and detection, and it keeps the encoder,
// the upload and the disk at sub-stream levels.
const FPS = 15

function stateBadge(s: PublisherState) {
  switch (s.kind) {
    case 'live':
      return <Badge variant="success">Publishing</Badge>
    case 'connecting':
      return <Badge variant="info">Connecting…</Badge>
    case 'retrying':
      return <Badge variant="warning">Reconnecting in {s.inSeconds}s</Badge>
    case 'error':
      return <Badge variant="destructive">Stopped</Badge>
    default:
      return <Badge variant="neutral">Not publishing</Badge>
  }
}

export function WebcamPublisher() {
  const { cameraId: idParam } = useParams()
  const cameraId = Number(idParam)
  const [camera, setCamera] = useState<{ name: string; source_type?: string } | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)

  // A session already running for this camera (started earlier, then the
  // user browsed away and came back) is shown, not restarted.
  const session = useWebcamSessions().find((s) => s.cameraId === cameraId)
  const sessionRef = useRef(session)
  sessionRef.current = session

  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [deviceId, setDeviceId] = useState<string>(session?.settings.deviceId ?? '')
  const [resolution, setResolution] = useState(session?.settings.resolution ?? '720p')
  const [audio, setAudio] = useState(session?.settings.audio ?? false)
  const [mediaError, setMediaError] = useState<string | null>(null)
  const state: PublisherState = session?.state ?? { kind: 'idle' }

  const videoRef = useRef<HTMLVideoElement | null>(null)
  // The page's own preview, before Start. On Start it is handed to the
  // session, which owns it from then on.
  const previewRef = useRef<MediaStream | null>(null)
  const [hasPicture, setHasPicture] = useState(false)

  const running = !!session && state.kind !== 'error'

  const secure = typeof window !== 'undefined' && window.isSecureContext
  const hasMedia = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia

  useEffect(() => {
    let cancelled = false
    apiService.getCamera(cameraId).then(
      (res: any) => { if (!cancelled) setCamera(res?.data ?? null) },
      (e: any) => { if (!cancelled) setLoadError(extractApiError(e, 'Could not load the camera')) },
    )
    return () => { cancelled = true }
  }, [cameraId])

  const show = (stream: MediaStream | null) => {
    setHasPicture(!!stream)
    if (videoRef.current) {
      videoRef.current.srcObject = stream
      if (stream) videoRef.current.play().catch(() => {})
    }
  }

  const stopPreview = () => {
    previewRef.current?.getTracks().forEach((t) => t.stop())
    previewRef.current = null
  }

  // Open the camera with the chosen settings. Device labels are only
  // readable once permission is granted, so the list is refreshed after.
  const openCamera = useCallback(async (): Promise<MediaStream | null> => {
    setMediaError(null)
    const res = RESOLUTIONS.find((r) => r.label === resolution) ?? RESOLUTIONS[1]
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
          width: { ideal: res.width },
          height: { ideal: res.height },
          frameRate: { ideal: FPS, max: FPS },
        },
        audio,
      })
      const list = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput')
      setDevices(list)
      return stream
    } catch (e: any) {
      setMediaError(
        e?.name === 'NotAllowedError'
          ? 'Camera access was blocked. Allow it in the browser’s site settings and try again.'
          : e?.name === 'NotReadableError'
          ? 'The camera is in use by another app (a video call?). Close it and try again.'
          : e?.message || 'Could not open the camera',
      )
      return null
    }
  }, [deviceId, resolution, audio])

  // The picture: the running session's stream, or the page's own preview.
  // A settings change re-opens the camera; while publishing, the session
  // moves to the new stream (it stops the old one).
  const settingsKey = `${deviceId}|${resolution}|${audio}`
  useEffect(() => {
    if (!secure || !hasMedia || camera?.source_type !== 'publisher') return
    const s = sessionRef.current
    const same = s && `${s.settings.deviceId}|${s.settings.resolution}|${s.settings.audio}` === settingsKey
    if (s && same) {
      show(s.stream)
      return
    }
    let cancelled = false
    void openCamera().then((stream) => {
      if (cancelled || !stream) {
        stream?.getTracks().forEach((t) => t.stop())
        return
      }
      const live = sessionRef.current
      if (live) {
        startSession(cameraId, live.cameraName, stream, { deviceId, resolution, audio })
      } else {
        stopPreview()
        previewRef.current = stream
      }
      show(stream)
    })
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settingsKey, secure, hasMedia, camera?.source_type])

  const start = async () => {
    const stream = previewRef.current ?? (await openCamera())
    if (!stream) return
    previewRef.current = null // the session owns it now
    startSession(cameraId, camera?.name ?? `Camera ${cameraId}`, stream, { deviceId, resolution, audio })
    show(stream)
  }

  const stop = async () => {
    stopSession(cameraId)
    show(null)
    // Back to a preview, so Start works again without a reload.
    const stream = await openCamera()
    if (stream) {
      previewRef.current = stream
      show(stream)
    }
  }

  // Leaving the page: a preview is released; a running session is not.
  useEffect(() => () => stopPreview(), [])

  const header = (
    <PageHeader
      breadcrumb={<Link to="/cameras" className="hover:underline">Cameras</Link>}
      title={<span className="inline-flex items-center gap-2"><CameraIcon size={18} /> {camera?.name ?? 'Webcam'}</span>}
      description="Use this device’s camera as an OpenNVR camera. It is recorded and shown in Live View like any other."
      actions={stateBadge(state)}
    />
  )

  if (loadError) {
    return <div className="p-4">{header}<EmptyState icon={<AlertTriangle size={28} />} title={loadError} /></div>
  }
  if (camera && camera.source_type !== 'publisher') {
    return (
      <div className="p-4">
        {header}
        <EmptyState
          icon={<AlertTriangle size={28} />}
          title="This is not a webcam camera"
          description="Only a camera added as a webcam can be fed from a browser."
        />
      </div>
    )
  }
  if (!secure || !hasMedia) {
    return (
      <div className="p-4">
        {header}
        <EmptyState
          icon={<AlertTriangle size={28} />}
          title="The browser won’t open the camera on this connection"
          description="Camera access needs HTTPS. Open OpenNVR at its https:// address (accept the certificate warning once), then come back to this page."
        />
      </div>
    )
  }

  return (
    <div className="p-4 space-y-4 max-w-4xl">
      {header}

      <div className="p-2 text-sm border border-[var(--warn)] bg-[color-mix(in_oklab,var(--warn)_12%,transparent)] flex items-start gap-2">
        <AlertTriangle size={16} className="shrink-0 mt-0.5 text-[var(--warn)]" />
        <span>
          Recording runs while this tab is open and publishing. You can use the
          rest of OpenNVR in this tab meanwhile (the header shows it is
          publishing), but closing or reloading the tab stops it. Keep the
          device awake and plugged in. For a camera that records around the
          clock, plug a USB webcam into the OpenNVR server and add it under
          Cameras → Add Camera → Manual → Webcam.
        </span>
      </div>

      <Card>
        <CardContent className="space-y-3">
          {/* Height-capped so the controls below stay on screen without scrolling. */}
          <div className="relative bg-black w-full h-[min(50vh,520px)]">
            <video ref={videoRef} muted playsInline className="w-full h-full object-contain" />
            {!hasPicture && !mediaError && (
              <div className="absolute inset-0 flex items-center justify-center text-[var(--text-dim)]">
                <Loader2 className="animate-spin" />
              </div>
            )}
          </div>
          {mediaError && <div className="text-sm text-[var(--danger)]">{mediaError}</div>}
          {state.kind === 'error' && <div className="text-sm text-[var(--danger)]">{state.reason}</div>}
          {state.kind === 'retrying' && (
            <div className="text-xs text-[var(--text-dim)]">{state.reason}</div>
          )}

          <div className="flex flex-wrap items-end gap-3">
            <label className="flex flex-col gap-1 text-xs text-[var(--text-dim)]">
              Camera
              <select
                className="bg-[var(--bg-2)] border border-[var(--border)] px-2 py-1.5 text-sm text-[var(--text)] min-w-[12rem]"
                value={deviceId}
                onChange={(e) => setDeviceId(e.target.value)}
              >
                <option value="">Default camera</option>
                {devices.map((d, i) => (
                  <option key={d.deviceId || i} value={d.deviceId}>{d.label || `Camera ${i + 1}`}</option>
                ))}
              </select>
            </label>
            <label className="flex flex-col gap-1 text-xs text-[var(--text-dim)]">
              Quality
              <select
                className="bg-[var(--bg-2)] border border-[var(--border)] px-2 py-1.5 text-sm text-[var(--text)]"
                value={resolution}
                onChange={(e) => setResolution(e.target.value)}
              >
                {RESOLUTIONS.map((r) => <option key={r.label} value={r.label}>{r.label} · {FPS} fps</option>)}
              </select>
            </label>
            <Button
              onClick={() => setAudio((a) => !a)}
              title={audio ? 'Microphone is recorded' : 'Microphone is off'}
              aria-pressed={audio}
            >
              {audio ? <Mic size={14} /> : <MicOff size={14} />} {audio ? 'Sound on' : 'Sound off'}
            </Button>
            <div className="ml-auto">
              {running ? (
                <Button variant="danger" onClick={() => void stop()}><Square size={14} /> Stop</Button>
              ) : (
                <Button variant="primary" onClick={() => void start()} disabled={!!mediaError && !hasPicture}>
                  <Play size={14} /> Start publishing
                </Button>
              )}
            </div>
          </div>
          {audio && (
            <div className="text-xs text-[var(--text-dim)]">
              Sound is recorded with the video. Recording people’s conversations
              may need their consent where you are.
            </div>
          )}
        </CardContent>
      </Card>

    </div>
  )
}
