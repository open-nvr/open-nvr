// The webcam that takes the photos itself (from PR #791's Register page).
// A small frame is checked about once a second (face boxes and the five
// landmarks, nothing kept); the page says what to change — distance, the
// oval, which way to turn, judged against the person's own straight-on
// photo — speaks it when asked, and takes each angle's photo once the
// face has been in place and steady. Each photo is checked against
// everyone already enrolled, so a person registered twice is told at the
// first photo.
import { useEffect, useRef, useState } from 'react'
import axios from 'axios'
import { Camera, RotateCcw, Video, XCircle } from 'lucide-react'
import { config } from '../config/environment'

const API = config.API_BASE_URL

export type Shot = { blob: Blob; url: string; label: string; key: AngleKey | 'extra'; bbox?: number[] | null }
export type AngleKey = 'front' | 'left' | 'right' | 'up' | 'down'
type Angle = { key: AngleKey; text: string }
export const ANGLES: Angle[] = [
  { key: 'front', text: 'Look straight at the camera' },
  { key: 'left', text: 'Turn your head a little to the left' },
  { key: 'right', text: 'Turn your head a little to the right' },
  { key: 'up', text: 'Lift your chin slightly' },
  { key: 'down', text: 'Lower your chin slightly' },
]

export type Duplicate = { student_code: string; full_name: string; email?: string | null; course_code?: string | null; section?: string | null; kind?: string; similarity?: number }
type FaceCheck = { faces: Array<{ bbox: number[]; confidence?: number | null; landmarks?: number[][] | null }>; frame: { w: number; h: number }; too_small: boolean }

/** The guidance frame: small, since only the face's place is wanted. */
const CHECK_WIDTH = 640
/** Good checks in a row before the photo is taken: the person is still. */
const STEADY = 2
/** Time to take the next pose after a photo. */
const NEXT_POSE_MS = 3000

/** How the head is turned, from the detector's five points (eyes, nose,
 * mouth corners). yaw: the nose's offset from between the eyes, in
 * eye-distances (0 facing the camera). pitch: where the nose sits between
 * the eye line (0) and the mouth line (1). Compared with the person's own
 * straight-on photo, never with fixed values. */
type Pose = { yaw: number; pitch: number }

export function poseOf(landmarks: number[][] | null | undefined): Pose | null {
  if (!landmarks || landmarks.length < 5) return null
  const [le, re, nose, ml, mr] = landmarks
  const eyes = Math.hypot(re[0] - le[0], re[1] - le[1])
  const eyeY = (le[1] + re[1]) / 2
  const mouthY = (ml[1] + mr[1]) / 2
  if (eyes < 1 || mouthY - eyeY < 1) return null
  return { yaw: (nose[0] - (le[0] + re[0]) / 2) / eyes, pitch: (nose[1] - eyeY) / (mouthY - eyeY) }
}

const TURN = { yawMin: 0.12, yawMax: 0.6, pitchMin: 0.07, pitchMax: 0.35, frontYaw: 0.1 }
type Hint = { tone: 'fix' | 'good'; text: string }

/** What to tell the person about one checked frame, for `angle`. Which way
 * is the person's left is not assumed (cameras and mirrors differ): the
 * second of a pair must be the other way from the first. */
export function judge(check: FaceCheck, w: number, h: number, angle: Angle, poses: Map<string, Pose>): Hint {
  const faces = check.faces
  if (faces.length === 0) {
    return check.too_small ? { tone: 'fix', text: 'Come closer to the camera' } : { tone: 'fix', text: 'No face found. Look at the camera, with light on your face' }
  }
  if (faces.length > 1) return { tone: 'fix', text: 'Only one person in view, please' }
  const [x1, y1, x2, y2] = faces[0].bbox
  const width = (x2 - x1) / w
  const cx = (x1 + x2) / 2 / w
  const cy = (y1 + y2) / 2 / h
  if (width < 0.12) return { tone: 'fix', text: 'Come a little closer' }
  if (width > 0.42) return { tone: 'fix', text: 'Move back a little' }
  if (Math.abs(cx - 0.5) > 0.16 || Math.abs(cy - 0.5) > 0.22) return { tone: 'fix', text: 'Move your face into the oval' }
  if ((faces[0].confidence ?? 1) < 0.6) return { tone: 'fix', text: 'Hold still, and face the light' }

  const pose = poseOf(faces[0].landmarks)
  const front = poses.get('front')
  if (!pose) return { tone: 'good', text: 'Hold still…' } // no landmarks: the place is all that can be checked
  if (angle.key === 'front') {
    return Math.abs(pose.yaw) > TURN.frontYaw ? { tone: 'fix', text: 'Face the camera straight on' } : { tone: 'good', text: 'Hold still…' }
  }
  if (!front) {
    if (angle.key === 'up' || angle.key === 'down') return { tone: 'good', text: 'Hold still…' }
    return judge(check, w, h, angle, new Map([...poses, ['front', { ...pose, yaw: 0 }]]))
  }
  const pair = { left: 'right', right: 'left', up: 'down', down: 'up' } as const
  const other = poses.get(pair[angle.key])
  if (angle.key === 'left' || angle.key === 'right') {
    const turn = pose.yaw - front.yaw
    if (Math.abs(turn) > TURN.yawMax) return { tone: 'fix', text: 'Not so far, just a little' }
    if (other && Math.sign(turn) === Math.sign(other.yaw - front.yaw) && Math.abs(turn) >= TURN.yawMin / 2) return { tone: 'fix', text: 'The other way, please' }
    if (Math.abs(turn) < TURN.yawMin) return { tone: 'fix', text: 'Turn your head a little more' }
    if (Math.abs(pose.pitch - front.pitch) > TURN.pitchMax) return { tone: 'fix', text: 'Keep your chin level' }
    return { tone: 'good', text: 'Hold still…' }
  }
  const tilt = pose.pitch - front.pitch
  if (Math.abs(tilt) > TURN.pitchMax) return { tone: 'fix', text: 'Not so far, just a little' }
  if (other && Math.sign(tilt) === Math.sign(other.pitch - front.pitch) && Math.abs(tilt) >= TURN.pitchMin / 2) {
    return { tone: 'fix', text: angle.key === 'up' ? 'Chin up, not down' : 'Chin down, not up' }
  }
  if (Math.abs(tilt) < TURN.pitchMin) return { tone: 'fix', text: angle.key === 'up' ? 'Lift your chin a little more' : 'Lower your chin a little more' }
  if (Math.abs(pose.yaw - front.yaw) > TURN.yawMin) return { tone: 'fix', text: 'Keep facing the camera' }
  return { tone: 'good', text: 'Hold still…' }
}

const sleep = (ms: number) => new Promise(r => window.setTimeout(r, ms))

// Sounds made here (no files): a shutter on each photo, a chime when the
// set is done. A browser plays sound only after a click on the page.
let audio: AudioContext | null = null
function unlockSound() {
  try { audio ??= new AudioContext(); void audio.resume() } catch { audio = null }
}
function shutterSound() {
  if (!audio || audio.state !== 'running') return
  const t = audio.currentTime
  const length = Math.floor(audio.sampleRate * 0.08)
  const buffer = audio.createBuffer(1, length, audio.sampleRate)
  const data = buffer.getChannelData(0)
  for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, 4)
  for (const [at, level] of [[0, 0.35], [0.06, 0.25]]) {
    const src = audio.createBufferSource(); const gain = audio.createGain(); const tone = audio.createBiquadFilter()
    tone.type = 'bandpass'; tone.frequency.value = 2500; src.buffer = buffer; gain.gain.value = level
    src.connect(tone).connect(gain).connect(audio.destination); src.start(t + at)
  }
}
function doneSound() {
  if (!audio || audio.state !== 'running') return
  const t = audio.currentTime
  for (const [at, freq] of [[0, 660], [0.14, 880]]) {
    const osc = audio.createOscillator(); const gain = audio.createGain()
    osc.type = 'sine'; osc.frequency.value = freq
    gain.gain.setValueAtTime(0.0001, t + at); gain.gain.exponentialRampToValueAtTime(0.2, t + at + 0.02); gain.gain.exponentialRampToValueAtTime(0.0001, t + at + 0.35)
    osc.connect(gain).connect(audio.destination); osc.start(t + at); osc.stop(t + at + 0.4)
  }
}

// Spoken guidance with the browser's own speech: the person in front of
// the camera is often not the one reading the screen. A voice installed on
// the computer is preferred, so the words do not leave it; the words are
// these fixed instructions, never a name.
const speech = typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null
let voice: SpeechSynthesisVoice | null = null
let lastSaid = ''
let lastSaidAt = 0
const REPEAT_MS = 6000
function pickVoice(): SpeechSynthesisVoice | null {
  if (!speech) return null
  const all = speech.getVoices()
  const lang = (document.documentElement.lang || navigator.language || 'en').toLowerCase()
  const base = lang.split('-')[0]
  const score = (v: SpeechSynthesisVoice) => (v.lang.toLowerCase() === lang ? 4 : v.lang.toLowerCase().startsWith(base) ? 2 : 0) + (v.localService ? 1 : 0)
  return all.length ? all.reduce((best, v) => (score(v) > score(best) ? v : best)) : null
}
speech?.addEventListener?.('voiceschanged', () => { voice = pickVoice() })
/** An instruction (`now`) cuts off whatever is being said; a correction
 * waits its turn and is dropped if something is still being said or it was
 * just said. */
export function say(text: string, now = false) {
  if (!speech) return
  const words = text.replace(/[…!]/g, '').trim()
  if (!words) return
  if (now) speech.cancel()
  else if (speech.speaking || speech.pending || (words === lastSaid && Date.now() - lastSaidAt < REPEAT_MS)) return
  voice ??= pickVoice()
  const u = new SpeechSynthesisUtterance(words)
  if (voice) { u.voice = voice; u.lang = voice.lang }
  u.rate = 0.95
  lastSaid = words; lastSaidAt = Date.now()
  speech.speak(u)
}
export function hush() { speech?.cancel(); lastSaid = '' }

/** A square head-and-shoulders crop around the face box, as a small JPEG:
 * the person's picture, cut here (the server has no need to). */
export async function cutPicture(photo: Blob, bbox: number[], size = 192): Promise<Blob | null> {
  const [x1, y1, x2, y2] = bbox
  if (![x1, y1, x2, y2].every(Number.isFinite) || x2 <= x1 || y2 <= y1) return null
  const img = await createImageBitmap(photo)
  const side = Math.min(Math.max(x2 - x1, y2 - y1) * 1.8, img.width, img.height)
  const cx = (x1 + x2) / 2, cy = (y1 + y2) / 2
  const sx = Math.min(Math.max(cx - side / 2, 0), img.width - side)
  const sy = Math.min(Math.max(cy - side / 2, 0), img.height - side)
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  canvas.getContext('2d')?.drawImage(img, sx, sy, side, side, 0, 0, size, size)
  img.close()
  return new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.85))
}

export const api = {
  checkFace: async (blob: Blob): Promise<FaceCheck> => {
    const form = new FormData(); form.append('image', blob, 'check.jpg')
    return (await axios.post<FaceCheck>(`${API}/students/check_frame`, form)).data
  },
  checkDuplicate: async (blob: Blob, excludeCode?: string): Promise<{ duplicate: Duplicate | null }> => {
    const form = new FormData(); form.append('image', blob, 'photo.jpg')
    if (excludeCode) form.append('exclude_code', excludeCode)
    return (await axios.post(`${API}/students/check_duplicate`, form)).data
  },
}

const errorText = (e: any) => String(e?.response?.data?.detail || e?.message || 'error')

export default function GuidedCapture({ shots, onShots, personName, excludeCode, disabled }: {
  shots: Shot[]
  onShots: (next: Shot[]) => void
  personName: string
  /** When adding photos to an existing person, their own code is not a duplicate. */
  excludeCode?: string
  disabled?: boolean
}) {
  const box = useRef<HTMLDivElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const stream = useRef<MediaStream | null>(null)
  const poses = useRef(new Map<string, Pose>())
  const lastPose = useRef<{ pose: Pose; at: number } | null>(null)
  const lastBox = useRef<{ bbox: number[]; scale: number; at: number } | null>(null)
  const justTook = useRef(false)
  const [state, setState] = useState<'idle' | 'starting' | 'live' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState(false)
  const [countdown, setCountdown] = useState<number | null>(null)
  const [auto, setAuto] = useState(true)
  const [sound, setSound] = useState(true)
  const soundRef = useRef(sound); soundRef.current = sound
  const [voiceOn, setVoiceOn] = useState(() => { try { return (localStorage.getItem('attendly:voice') ?? 'on') === 'on' && !!speech } catch { return !!speech } })
  const voiceRef = useRef(voiceOn); voiceRef.current = voiceOn
  const [hint, setHint] = useState<Hint | null>(null)
  const [nextIn, setNextIn] = useState<number | null>(null)
  const [dup, setDup] = useState<Duplicate | null>(null)
  const secure = typeof window !== 'undefined' && window.isSecureContext && !!navigator.mediaDevices?.getUserMedia

  const shotsRef = useRef(shots); shotsRef.current = shots
  const done = new Set(shots.map(s => s.key))
  const doneRef = useRef(done); doneRef.current = done
  for (const key of [...poses.current.keys()]) if (!done.has(key as AngleKey)) poses.current.delete(key)
  const angle = ANGLES.find(a => !done.has(a.key)) ?? null
  const angleRef = useRef(angle); angleRef.current = angle

  const stop = () => { stream.current?.getTracks().forEach(t => t.stop()); stream.current = null }
  useEffect(() => () => { stop(); hush() }, [])
  useEffect(() => { try { localStorage.setItem('attendly:voice', voiceOn ? 'on' : 'off') } catch {} }, [voiceOn])

  const start = async () => {
    setState('starting'); setError(null)
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' }, audio: false })
      stream.current = s
      unlockSound()
      if (video.current) { video.current.srcObject = s; await video.current.play() }
      setState('live')
      window.requestAnimationFrame(() => {
        const r = box.current?.getBoundingClientRect()
        if (!r) return
        const by = Math.min(r.bottom - window.innerHeight + 16, r.top - 80)
        if (by > 0) window.scrollBy({ top: by, behavior: 'smooth' })
      })
    } catch (e) {
      const name = (e as DOMException)?.name
      setError(name === 'NotAllowedError' ? 'The browser was not allowed to use the camera. Allow it for this site and try again.' : name === 'NotFoundError' ? 'No camera was found on this computer.' : errorText(e))
      setState('error')
    }
  }

  const grab = (width: number, quality: number): Promise<{ blob: Blob; w: number; h: number } | null> => {
    const v = video.current
    if (!v || !v.videoWidth) return Promise.resolve(null)
    const scale = Math.min(1, width / v.videoWidth)
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(v.videoWidth * scale); canvas.height = Math.round(v.videoHeight * scale)
    canvas.getContext('2d')?.drawImage(v, 0, 0, canvas.width, canvas.height)
    return new Promise(resolve => canvas.toBlob(blob => resolve(blob ? { blob, w: canvas.width, h: canvas.height } : null), 'image/jpeg', quality))
  }

  const capture = async (pose?: Pose | null) => {
    const shot = await grab(Number.MAX_SAFE_INTEGER, 0.92)
    if (!shot) return
    const a = angleRef.current
    const recent = lastPose.current && Date.now() - lastPose.current.at < 2000 ? lastPose.current.pose : null
    const taken = pose ?? recent
    if (a && taken) poses.current.set(a.key, taken)
    // the face box of the last check, scaled to the full photo: the picture is cut from it
    const lb = lastBox.current && Date.now() - lastBox.current.at < 2000 ? lastBox.current : null
    const bbox = lb ? lb.bbox.map(v => v / lb.scale) : null
    const next: Shot = { blob: shot.blob, url: URL.createObjectURL(shot.blob), label: a ? a.text : `Extra photo ${new Date().toLocaleTimeString()}`, key: a ? a.key : 'extra', bbox }
    onShots([...shotsRef.current, next])
    api.checkDuplicate(shot.blob, excludeCode).then(({ duplicate }) => {
      if (!duplicate) return
      setDup(d => d ?? duplicate)
      if (voiceRef.current) window.setTimeout(() => say(`Please stop. This face is already registered as ${duplicate.full_name || 'someone else'}.`, true), 400)
    }).catch(() => {})
    setFlash(true); window.setTimeout(() => setFlash(false), 150)
    const last = !!a && ANGLES.every(x => x === a || doneRef.current.has(x.key))
    if (soundRef.current) { shutterSound(); if (last) window.setTimeout(doneSound, 350) }
    if (voiceRef.current && last) window.setTimeout(() => say('All done. Thank you.', true), 700)
  }

  useEffect(() => { if (state === 'live' && !auto && angle && voiceRef.current) say(angle.text, true) }, [state, auto, angle?.key]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (countdown === null) return
    if (countdown === 0) { capture(); setCountdown(null); return }
    const t = window.setTimeout(() => setCountdown(c => (c === null ? null : c - 1)), 1000)
    return () => window.clearTimeout(t)
  }, [countdown]) // eslint-disable-line react-hooks/exhaustive-deps

  const running = state === 'live' && auto && !disabled && angle !== null && dup === null
  const key = angle?.key ?? null
  useEffect(() => {
    if (!running || key === null) { setHint(null); return }
    let cancelled = false
    ;(async () => {
      const a0 = angleRef.current
      if (a0 && voiceRef.current) {
        if (justTook.current) window.setTimeout(() => !cancelled && voiceRef.current && say(a0.text, true), 400)
        else say(a0.text, true)
      }
      if (justTook.current) {
        justTook.current = false
        for (let left = NEXT_POSE_MS; left > 0 && !cancelled; left -= 1000) { setNextIn(Math.ceil(left / 1000)); await sleep(1000) }
        setNextIn(null)
      }
      let steady = 0
      let lastFix = ''
      while (!cancelled) {
        const frame = await grab(CHECK_WIDTH, 0.8)
        if (!frame) { await sleep(500); continue }
        let verdict: Hint
        let pose: Pose | null = null
        try {
          const check = await api.checkFace(frame.blob)
          const a = angleRef.current
          if (!a) return
          const w = check.frame.w || frame.w, h = check.frame.h || frame.h
          verdict = judge(check, w, h, a, poses.current)
          pose = check.faces.length === 1 ? poseOf(check.faces[0].landmarks) : null
          lastPose.current = pose ? { pose, at: Date.now() } : null
          lastBox.current = check.faces.length === 1 ? { bbox: check.faces[0].bbox, scale: w / (video.current?.videoWidth || w), at: Date.now() } : null
        } catch (e) {
          if (cancelled) return
          setHint({ tone: 'fix', text: `Cannot check the face right now: ${errorText(e)}` })
          await sleep(3000)
          continue
        }
        if (cancelled) return
        steady = verdict.tone === 'good' ? steady + 1 : 0
        setHint(verdict)
        if (voiceRef.current && verdict.tone === 'fix' && verdict.text === lastFix) say(verdict.text)
        lastFix = verdict.text
        if (steady < STEADY) { await sleep(400); continue }
        justTook.current = true
        setHint({ tone: 'good', text: 'Got it!' })
        await capture(pose)
        return
      }
    })()
    return () => { cancelled = true; setNextIn(null) }
  }, [running, key]) // eslint-disable-line react-hooks/exhaustive-deps

  const restart = () => { hush(); setDup(null); poses.current.clear(); shots.forEach(s => URL.revokeObjectURL(s.url)); onShots([]) }
  const remove = (i: number) => { URL.revokeObjectURL(shots[i].url); onShots(shots.filter((_, j) => j !== i)) }

  if (!secure) {
    return (
      <div className="rounded-lg border border-yellow-200 bg-yellow-50 p-4 text-sm text-yellow-800">
        <Camera className="inline w-4 h-4 mr-1" /> The webcam needs a secure connection: browsers only allow the camera on https:// pages (or localhost). Open this page over HTTPS.
      </div>
    )
  }

  const waiting = nextIn !== null
  const ring = waiting ? 'border-white/70' : hint?.tone === 'good' ? 'border-green-400' : hint?.tone === 'fix' ? 'border-amber-300' : 'border-white/70'

  return (
    <div>
      <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-sm text-gray-700">
        <li>Sit facing the camera, about an arm's length away, with light in front of you (not a window behind).</li>
        <li>Take off a cap or mask. Keep glasses on if you usually wear them.</li>
        <li>Put your face in the oval and do what the top of the picture says. {auto ? 'Each photo is taken by itself once your face is placed and turned as asked.' : 'Press Capture, or use the timer.'}</li>
      </ol>
      <div ref={box} className="mx-auto w-full scroll-mt-20" style={{ maxWidth: 'calc((100dvh - 13rem) * 16 / 9)' }}>
        <div className="relative overflow-hidden rounded-xl bg-gray-900">
          <video ref={video} playsInline muted className={`aspect-video w-full -scale-x-100 object-cover ${state === 'live' ? '' : 'invisible'}`} />
          {state === 'live' && (
            <>
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                <div className={`h-[62%] aspect-[3/4] rounded-[50%] border-4 ${ring} shadow-[0_0_0_9999px_rgba(0,0,0,0.25)] transition-colors`} />
              </div>
              <div className="absolute inset-x-0 top-0 bg-gradient-to-b from-black/70 to-transparent p-2 text-center text-white sm:p-3">
                <div className="text-sm font-semibold sm:text-base">
                  {dup ? 'Already registered' : !angle ? 'All angles taken' : waiting ? `Next: ${angle.text.toLowerCase()}… ${nextIn}` : `${ANGLES.indexOf(angle) + 1} of ${ANGLES.length}: ${angle.text}`}
                </div>
              </div>
              {hint && angle && !waiting && (
                <div className="absolute inset-x-0 bottom-0 flex justify-center p-2 sm:p-3">
                  <span aria-live="polite" className={`rounded-full px-3 py-1 text-xs font-medium shadow sm:text-sm ${hint.tone === 'good' ? 'bg-green-600 text-white' : 'bg-amber-100 text-amber-900'}`}>{hint.text}</span>
                </div>
              )}
              {countdown !== null && <div className="absolute inset-0 flex items-center justify-center text-7xl font-bold text-white drop-shadow">{countdown}</div>}
              {flash && <div className="absolute inset-0 bg-white/70" />}
              {dup && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/55 p-4">
                  <div role="alert" className="max-w-md rounded-xl bg-white p-4 text-center text-sm text-gray-800 shadow-lg">
                    <XCircle className="mx-auto mb-2 h-8 w-8 text-red-600" />
                    <div className="text-base font-semibold text-gray-900">This face is already registered</div>
                    <div className="mt-1">as <strong>{dup.full_name}</strong> ({dup.student_code}{dup.email ? `, ${dup.email}` : ''}). A face can belong to one person only, so these photos cannot be enrolled for {personName || 'this person'}.</div>
                    <div className="mt-3 flex flex-wrap justify-center gap-2">
                      <button type="button" className="inline-flex items-center gap-1 rounded-lg bg-gray-100 px-3 py-1.5 font-medium hover:bg-gray-200" onClick={restart}><RotateCcw className="h-3.5 w-3.5" /> Start over with someone else</button>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
          {state !== 'live' && (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center text-gray-300">
              <Video className="h-10 w-10" />
              {state === 'error' ? <p className="text-sm text-red-300">{error}</p> : <p className="text-sm">The camera is off.</p>}
              <button type="button" className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50" disabled={state === 'starting'} onClick={start}>{state === 'starting' ? 'Starting…' : 'Start the camera'}</button>
            </div>
          )}
        </div>
        {state === 'live' && (
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex flex-wrap items-center gap-3">
              <div className="flex gap-1">{ANGLES.map(a => <span key={a.key} title={a.text} className={`h-2 w-6 rounded-full sm:w-8 ${done.has(a.key) ? 'bg-green-500' : a === angle ? 'bg-indigo-500' : 'bg-gray-200'}`} />)}</div>
              <label className="flex items-center gap-1.5 text-sm text-gray-700"><input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={auto} onChange={e => setAuto(e.target.checked)} /> Take photos automatically</label>
              <label className="flex items-center gap-1.5 text-sm text-gray-700"><input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={sound} onChange={e => { setSound(e.target.checked); if (e.target.checked) unlockSound() }} /> Sound</label>
              {speech && <label className="flex items-center gap-1.5 text-sm text-gray-700" title="Say each step and correction out loud"><input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={voiceOn} onChange={e => { setVoiceOn(e.target.checked); if (e.target.checked) { if (angle) say(angle.text, true) } else hush() }} /> Voice</label>}
            </div>
            <div className="flex gap-2">
              <button type="button" className="rounded-lg px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-100 disabled:opacity-50" onClick={restart} disabled={!shots.length}><RotateCcw className="inline h-3.5 w-3.5" /> Start over</button>
              {!auto && (
                <>
                  <button type="button" className="rounded-lg bg-gray-100 px-3 py-1.5 text-sm hover:bg-gray-200 disabled:opacity-50" onClick={() => setCountdown(3)} disabled={disabled || countdown !== null}>Timer (3 s)</button>
                  <button type="button" className="rounded-lg bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50" onClick={() => capture()} disabled={disabled || countdown !== null}><Camera className="inline h-4 w-4" /> Capture</button>
                </>
              )}
            </div>
          </div>
        )}
      </div>
      {shots.length > 0 && (
        <div className="mt-4">
          <div className="text-sm font-medium text-gray-700 mb-2">{shots.length} photo{shots.length === 1 ? '' : 's'} ready — remove any that are blurred, then submit</div>
          <div className="flex flex-wrap gap-3">
            {shots.map((s, i) => (
              <figure key={s.url} className="w-28">
                <img src={s.url} alt="" className="w-28 h-20 object-cover rounded border -scale-x-100" />
                <figcaption className="mt-1 flex items-center justify-between text-[11px] text-gray-600"><span className="truncate" title={s.label}>{s.key === 'extra' ? 'extra' : s.key}</span><button type="button" className="text-red-600 hover:underline" onClick={() => remove(i)} disabled={disabled}>remove</button></figcaption>
              </figure>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
