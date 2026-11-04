// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's Image Register and Video Register, as one page: the person's
// details and consent first, then their face from photos or the webcam.
// Core checks each photo (exactly one face, not someone already enrolled)
// and keeps the ones that pass.

import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Camera, CheckCircle2, ImagePlus, RotateCcw, Search, Trash2, UserPlus, Video, XCircle } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { api, type EnrolResult, type Person } from '../attendance'
import { useFeedback } from '../components/feedback'
import { Badge, Button, Card, Empty, ErrorNote, Field, Loading, PageTitle, inputClass } from '../components/ui'
import { useGroups, useLoad } from '../hooks'

const MAX_PHOTOS = 10
const MAX_BYTES = 8 * 1024 * 1024

type Shot = { blob: Blob; url: string; label: string }

export default function Register() {
  const { can } = useAuth()
  const [params, setParams] = useSearchParams()
  const personId = Number(params.get('person')) || null
  const person = useLoad<Person | null>(() => (personId ? api.person(personId) : Promise.resolve(null)), [personId])

  if (!can.enroll) {
    return (
      <Card>
        <Empty icon={<UserPlus className="h-10 w-10" />} title="Registering people is not part of your role">
          It needs the attendance.enroll permission, because a face is biometric data.
        </Empty>
      </Card>
    )
  }

  const choose = (id: number | null) => setParams(id ? { person: String(id) } : {})

  return (
    <>
      <PageTitle
        title="Register"
        subtitle="Add a person, record their consent, then enrol their face. Several angles recognise better than one."
      />
      {personId === null ? (
        <div className="grid gap-4 lg:grid-cols-5">
          <div className="lg:col-span-3">
            <DetailsForm onCreated={(p) => choose(p.id)} />
          </div>
          <div className="lg:col-span-2">
            <FindPerson onPick={(p) => choose(p.id)} />
          </div>
        </div>
      ) : person.loading && !person.data ? (
        <Loading />
      ) : person.error ? (
        <ErrorNote message={person.error} onRetry={person.reload} />
      ) : person.data ? (
        <FaceStep person={person.data} onChanged={person.reload} onDone={() => choose(null)} />
      ) : null}
    </>
  )
}

function DetailsForm({ onCreated }: { onCreated: (p: Person) => void }) {
  const { can } = useAuth()
  const { toast } = useFeedback()
  const groups = useGroups()
  const [name, setName] = useState('')
  const [ref, setRef] = useState('')
  const [email, setEmail] = useState('')
  const [groupId, setGroupId] = useState('')
  const [consent, setConsent] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const p = await api.createPerson({
        display_name: name.trim(),
        external_ref: ref.trim() || null,
        email: email.trim() || null,
      })
      await api.setConsent(p.id, true)
      if (groupId) {
        try {
          await api.addMembers(Number(groupId), [p.id])
        } catch (e) {
          toast('error', `Registered, but not added to the group: ${errorText(e)}`)
        }
      }
      toast('success', `${p.display_name} registered. Now enrol their face.`)
      onCreated(p)
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card className="p-5">
      <h2 className="mb-4 flex items-center gap-2 text-base font-semibold text-gray-900">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-600 text-xs text-white">1</span>
        Who is this?
      </h2>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <Field label="Full name">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} maxLength={200} autoFocus />
          </Field>
        </div>
        <Field label="ID" hint="Roll number, USN or employee ID. Must be unique.">
          <input className={inputClass} value={ref} onChange={(e) => setRef(e.target.value)} maxLength={100} />
        </Field>
        <Field label="Email" hint="Optional.">
          <input className={inputClass} type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
        </Field>
        {can.manage && (
          <div className="sm:col-span-2">
            <Field label="Add to group" hint="Optional. You can change groups later.">
              <select className={inputClass} value={groupId} onChange={(e) => setGroupId(e.target.value)}>
                <option value="">No group yet</option>
                {(groups.data ?? []).filter((g) => g.active).map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                    {g.code ? ` (${g.code})` : ''}
                  </option>
                ))}
              </select>
            </Field>
          </div>
        )}
      </div>
      <label className="mt-5 flex items-start gap-3 rounded-lg border border-indigo-100 bg-indigo-50/60 p-3 text-sm text-gray-800">
        <input type="checkbox" className="mt-0.5 h-4 w-4 accent-indigo-600" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
        <span>
          This person (or their parent or guardian) has agreed to their face being used to take attendance. Their face
          data stays on this OpenNVR server, and is deleted if they withdraw or are removed.
        </span>
      </label>
      {error && (
        <div className="mt-4">
          <ErrorNote message={error} />
        </div>
      )}
      <div className="mt-5 flex justify-end">
        <Button onClick={submit} busy={busy} disabled={!name.trim() || !consent}>
          <UserPlus className="h-4 w-4" /> Register and continue
        </Button>
      </div>
    </Card>
  )
}

function FindPerson({ onPick }: { onPick: (p: Person) => void }) {
  const [q, setQ] = useState('')
  const [term, setTerm] = useState('')
  useEffect(() => {
    const t = window.setTimeout(() => setTerm(q.trim()), 250)
    return () => window.clearTimeout(t)
  }, [q])
  const found = useLoad(() => (term ? api.listPeople({ q: term, limit: 8 }) : Promise.resolve(null)), [term])

  return (
    <Card className="p-5">
      <h2 className="mb-1 text-base font-semibold text-gray-900">Already registered?</h2>
      <p className="mb-3 text-sm text-gray-600">Find them to add more photos.</p>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
        <input className={`${inputClass} pl-9`} placeholder="Name or ID" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ul className="mt-3 divide-y">
        {found.data?.people.map((p) => (
          <li key={p.id}>
            <button className="flex w-full items-center justify-between gap-2 py-2 text-left text-sm hover:text-indigo-700" onClick={() => onPick(p)}>
              <span>
                <span className="font-medium">{p.display_name}</span>
                {p.external_ref && <span className="ml-2 text-gray-500">{p.external_ref}</span>}
              </span>
              <Badge tone={p.face_samples ? 'green' : 'gray'}>{p.face_samples} photos</Badge>
            </button>
          </li>
        ))}
        {term && found.data && !found.data.people.length && <li className="py-2 text-sm text-gray-500">Nobody matches.</li>}
      </ul>
    </Card>
  )
}

function FaceStep({ person, onChanged, onDone }: { person: Person; onChanged: () => void; onDone: () => void }) {
  const { toast } = useFeedback()
  const [mode, setMode] = useState<'photos' | 'webcam'>('photos')
  const [shots, setShots] = useState<Shot[]>([])
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ shots: Shot[]; r: EnrolResult } | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => () => shots.forEach((s) => URL.revokeObjectURL(s.url)), []) // eslint-disable-line react-hooks/exhaustive-deps

  const add = (more: Shot[]) => setShots((s) => [...s, ...more].slice(0, MAX_PHOTOS))
  const remove = (i: number) =>
    setShots((s) => {
      URL.revokeObjectURL(s[i].url)
      return s.filter((_, j) => j !== i)
    })

  const consent = async () => {
    setBusy(true)
    try {
      await api.setConsent(person.id, true)
      onChanged()
    } catch (e) {
      toast('error', errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const enrol = async () => {
    setBusy(true)
    setError(null)
    try {
      const r = await api.enrol(person.id, shots.map((s) => s.blob))
      setResult({ shots, r })
      setShots([])
      onChanged()
      toast(r.enrolled ? 'success' : 'error', r.enrolled ? `${r.enrolled} of ${shots.length} photos enrolled` : 'No photo was enrolled')
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-3">
      <Card className="p-5 lg:col-span-1">
        <h2 className="mb-3 flex items-center gap-2 text-base font-semibold text-gray-900">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-600 text-xs text-white">2</span>
          Enrol a face
        </h2>
        <div className="space-y-1 text-sm">
          <div className="text-lg font-semibold text-gray-900">{person.display_name}</div>
          {person.external_ref && <div className="text-gray-600">ID {person.external_ref}</div>}
          {person.email && <div className="break-all text-gray-600">{person.email}</div>}
          <div className="pt-2">
            <Badge tone={person.face_samples ? 'green' : 'amber'}>{person.face_samples} photos enrolled</Badge>
          </div>
        </div>
        <div className="mt-5 rounded-lg bg-gray-50 p-3 text-xs text-gray-600">
          <p className="mb-1 font-medium text-gray-700">For good recognition</p>
          <ul className="list-disc space-y-0.5 pl-4">
            <li>One face per photo, clearly lit, nothing covering it.</li>
            <li>3 to 5 photos: straight on, turned a little left and right, chin up and down.</li>
            <li>Glasses on if they usually wear them.</li>
          </ul>
        </div>
        <div className="mt-5 flex flex-wrap gap-2">
          <Button variant="secondary" onClick={onDone}>
            <UserPlus className="h-4 w-4" /> Register another
          </Button>
          <Link to="/people" className="inline-flex items-center rounded-md px-3 py-2 text-sm text-indigo-700 hover:bg-indigo-50">
            All people →
          </Link>
        </div>
      </Card>

      <Card className="p-5 lg:col-span-2">
        {!person.consent_at ? (
          <Empty title="No consent on record">
            <p>A face can only be enrolled once the person has agreed.</p>
            <div className="mt-3">
              <Button onClick={consent} busy={busy}>
                Record their consent
              </Button>
            </div>
          </Empty>
        ) : (
          <>
            <div className="mb-4 inline-flex rounded-lg border bg-gray-50 p-1 text-sm">
              {(['photos', 'webcam'] as const).map((m) => (
                <button
                  key={m}
                  onClick={() => setMode(m)}
                  className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 ${mode === m ? 'bg-white font-medium text-indigo-700 shadow-sm' : 'text-gray-600'}`}
                >
                  {m === 'photos' ? <ImagePlus className="h-4 w-4" /> : <Video className="h-4 w-4" />}
                  {m === 'photos' ? 'Upload photos' : 'Use the webcam'}
                </button>
              ))}
            </div>

            {mode === 'photos' ? (
              <PhotoPicker onAdd={add} room={MAX_PHOTOS - shots.length} />
            ) : (
              <WebcamCapture onAdd={add} room={MAX_PHOTOS - shots.length} />
            )}

            {shots.length > 0 && (
              <div className="mt-5">
                <div className="mb-2 text-sm font-medium text-gray-700">
                  Ready to enrol ({shots.length}/{MAX_PHOTOS})
                </div>
                <div className="grid grid-cols-3 gap-2 sm:grid-cols-5">
                  {shots.map((s, i) => (
                    <figure key={s.url} className="group relative overflow-hidden rounded-lg border bg-gray-100">
                      <img src={s.url} alt={s.label} className="aspect-square w-full object-cover" />
                      <figcaption className="truncate px-1.5 py-1 text-[11px] text-gray-600">{s.label}</figcaption>
                      <button
                        onClick={() => remove(i)}
                        className="absolute right-1 top-1 rounded bg-white/90 p-1 text-red-600 opacity-80 hover:opacity-100"
                        aria-label={`Remove ${s.label}`}
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </button>
                    </figure>
                  ))}
                </div>
                {error && (
                  <div className="mt-3">
                    <ErrorNote message={error} />
                  </div>
                )}
                <div className="mt-4 flex justify-end">
                  <Button onClick={enrol} busy={busy}>
                    Enrol {shots.length} photo{shots.length === 1 ? '' : 's'}
                  </Button>
                </div>
              </div>
            )}

            {result && <EnrolOutcome shots={result.shots} r={result.r} />}
          </>
        )}
      </Card>
    </div>
  )
}

function EnrolOutcome({ shots, r }: { shots: Shot[]; r: EnrolResult }) {
  return (
    <div className="mt-6 border-t pt-4">
      <div className="mb-2 text-sm font-medium text-gray-700">Last enrolment</div>
      <ul className="space-y-2">
        {r.results.map((x) => {
          const shot = shots[x.index]
          return (
            <li key={x.index} className="flex items-center gap-3 text-sm">
              {shot && <img src={shot.url} alt="" className="h-10 w-10 rounded object-cover" />}
              {x.ok ? <CheckCircle2 className="h-4 w-4 text-green-600" /> : <XCircle className="h-4 w-4 text-red-600" />}
              <span className={x.ok ? 'text-gray-700' : 'text-red-700'}>
                {shot?.label ?? `Photo ${x.index + 1}`}: {x.ok ? 'enrolled' : x.error ?? 'refused'}
                {x.duplicate_of ? (
                  <>
                    {' '}
                    — <Link className="underline" to={`/register?person=${x.duplicate_of}`}>see that person</Link>
                  </>
                ) : null}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function PhotoPicker({ onAdd, room }: { onAdd: (s: Shot[]) => void; room: number }) {
  const { toast } = useFeedback()
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)

  const take = (files: FileList | null) => {
    if (!files) return
    const ok: Shot[] = []
    for (const f of Array.from(files)) {
      if (!/^image\/(jpeg|png|webp)$/.test(f.type)) toast('error', `${f.name}: not a JPEG, PNG or WebP photo`)
      else if (f.size > MAX_BYTES) toast('error', `${f.name}: larger than 8 MB`)
      else ok.push({ blob: f, url: URL.createObjectURL(f), label: f.name })
    }
    if (ok.length > room) toast('info', `Only ${MAX_PHOTOS} photos at a time; the rest were left out.`)
    onAdd(ok.slice(0, room))
    if (input.current) input.current.value = ''
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault()
        setOver(false)
        take(e.dataTransfer.files)
      }}
      className={`flex flex-col items-center justify-center gap-3 rounded-xl border-2 border-dashed px-6 py-10 text-center ${over ? 'border-indigo-400 bg-indigo-50' : 'border-gray-300 bg-gray-50'}`}
    >
      <ImagePlus className="h-10 w-10 text-gray-400" />
      <div className="text-sm text-gray-600">Drop photos here, or</div>
      <Button variant="secondary" onClick={() => input.current?.click()} disabled={room <= 0}>
        Choose photos
      </Button>
      <input ref={input} type="file" accept="image/jpeg,image/png,image/webp" multiple hidden onChange={(e) => take(e.target.files)} />
      <div className="text-xs text-gray-500">Up to {MAX_PHOTOS} at a time, 8 MB each.</div>
    </div>
  )
}

const ANGLES = [
  'Look straight at the camera',
  'Turn your head a little to the left',
  'Turn your head a little to the right',
  'Lift your chin slightly',
  'Lower your chin slightly',
]

function WebcamCapture({ onAdd, room }: { onAdd: (s: Shot[]) => void; room: number }) {
  const video = useRef<HTMLVideoElement>(null)
  const stream = useRef<MediaStream | null>(null)
  const [state, setState] = useState<'idle' | 'starting' | 'live' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [step, setStep] = useState(0)
  const [flash, setFlash] = useState(false)
  const [countdown, setCountdown] = useState<number | null>(null)
  const secure = typeof window !== 'undefined' && window.isSecureContext && !!navigator.mediaDevices?.getUserMedia

  const stop = () => {
    stream.current?.getTracks().forEach((t) => t.stop())
    stream.current = null
  }
  useEffect(() => stop, [])

  const start = async () => {
    setState('starting')
    setError(null)
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' },
        audio: false,
      })
      stream.current = s
      if (video.current) {
        video.current.srcObject = s
        await video.current.play()
      }
      setState('live')
    } catch (e) {
      const name = (e as DOMException)?.name
      setError(
        name === 'NotAllowedError'
          ? 'The browser was not allowed to use the camera. Allow it for this site and try again.'
          : name === 'NotFoundError'
            ? 'No camera was found on this computer.'
            : errorText(e),
      )
      setState('error')
    }
  }

  const capture = () => {
    const v = video.current
    if (!v || !v.videoWidth) return
    const canvas = document.createElement('canvas')
    canvas.width = v.videoWidth
    canvas.height = v.videoHeight
    canvas.getContext('2d')?.drawImage(v, 0, 0)
    const label = ANGLES[step % ANGLES.length]
    canvas.toBlob(
      (blob) => {
        if (!blob) return
        onAdd([{ blob, url: URL.createObjectURL(blob), label }])
        setStep((s) => s + 1)
      },
      'image/jpeg',
      0.92,
    )
    setFlash(true)
    window.setTimeout(() => setFlash(false), 150)
  }

  // A 3-2-1 so the person can hold the pose without anyone at the keyboard.
  useEffect(() => {
    if (countdown === null) return
    if (countdown === 0) {
      capture()
      setCountdown(null)
      return
    }
    const t = window.setTimeout(() => setCountdown((c) => (c === null ? null : c - 1)), 1000)
    return () => window.clearTimeout(t)
  }, [countdown]) // eslint-disable-line react-hooks/exhaustive-deps

  const guide = useMemo(() => ANGLES[step % ANGLES.length], [step])

  if (!secure) {
    return (
      <Empty icon={<Camera className="h-10 w-10" />} title="The webcam needs a secure connection">
        Browsers only allow the camera on https:// pages. Open this page over HTTPS, or upload photos instead.
      </Empty>
    )
  }

  return (
    <div>
      <div className="relative overflow-hidden rounded-xl bg-gray-900">
        <video ref={video} playsInline muted className={`aspect-video w-full -scale-x-100 object-cover ${state === 'live' ? '' : 'invisible'}`} />
        {state === 'live' && (
          <>
            <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
              <div className="h-[62%] aspect-[3/4] rounded-[50%] border-4 border-white/70 shadow-[0_0_0_9999px_rgba(0,0,0,0.25)]" />
            </div>
            <div className="absolute inset-x-0 top-0 bg-gradient-to-b from-black/60 to-transparent p-3 text-center text-sm font-medium text-white">
              {step < ANGLES.length ? `${step + 1} of ${ANGLES.length}: ${guide}` : guide}
            </div>
            {countdown !== null && (
              <div className="absolute inset-0 flex items-center justify-center text-7xl font-bold text-white drop-shadow">{countdown}</div>
            )}
            {flash && <div className="absolute inset-0 bg-white/70" />}
          </>
        )}
        {state !== 'live' && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-6 text-center text-gray-300">
            <Video className="h-10 w-10" />
            {state === 'error' ? <p className="text-sm text-red-300">{error}</p> : <p className="text-sm">The camera is off.</p>}
            <Button onClick={start} busy={state === 'starting'}>
              Start the camera
            </Button>
          </div>
        )}
      </div>
      {state === 'live' && (
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex gap-1">
            {ANGLES.map((a, i) => (
              <span key={a} title={a} className={`h-2 w-8 rounded-full ${i < step ? 'bg-green-500' : i === step ? 'bg-indigo-500' : 'bg-gray-200'}`} />
            ))}
          </div>
          <div className="flex gap-2">
            <Button variant="ghost" size="sm" onClick={() => setStep(0)}>
              <RotateCcw className="h-3.5 w-3.5" /> Start over
            </Button>
            <Button variant="secondary" onClick={() => setCountdown(3)} disabled={room <= 0 || countdown !== null}>
              Timer (3 s)
            </Button>
            <Button onClick={capture} disabled={room <= 0 || countdown !== null}>
              <Camera className="h-4 w-4" /> Capture
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
