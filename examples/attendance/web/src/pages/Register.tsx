// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's Image Register and Video Register, as one page: the person's
// details and consent first, then their face from photos or the webcam.
// Core checks each photo (exactly one face, not someone already enrolled)
// and keeps the ones that pass.

import { useEffect, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Camera, CheckCircle2, ImagePlus, RotateCcw, Search, Trash2, UserPlus, Video, XCircle } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { api, type Duplicate, type EnrolResult, type FaceCheck, type Person } from '../attendance'
import { Avatar, cutPicture, pictureChanged, shrinkPhoto } from '../components/faces'
import { useFeedback } from '../components/feedback'
import { Badge, Button, Card, Empty, ErrorNote, Field, Loading, Modal, PageTitle, inputClass } from '../components/ui'
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
      try {
        await api.setConsent(p.id, true)
      } catch (e) {
        // Not left half-registered: a retry would collide on the
        // reference number of a person with no consent. Removing someone
        // with no consent needs no face service.
        await api.removePerson(p.id).catch(() => undefined)
        throw e
      }
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
  const webcamOk = typeof window !== 'undefined' && window.isSecureContext && !!navigator.mediaDevices?.getUserMedia
  const [mode, setMode] = useState<'photos' | 'webcam'>(webcamOk ? 'webcam' : 'photos')
  const [pictureVersion, setPictureVersion] = useState(0)
  const review = useRef<HTMLDivElement>(null)
  const [shots, setShots] = useState<Shot[]>([])
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<{ shots: Shot[]; r: EnrolResult } | null>(null)
  const [error, setError] = useState<string | null>(null)

  // The photos on screen when the step goes away; a ref, because the
  // cleanup closure would otherwise see only the first (empty) list.
  const shotsNow = useRef(shots)
  shotsNow.current = shots
  useEffect(() => () => shotsNow.current.forEach((s) => URL.revokeObjectURL(s.url)), [])

  const add = (more: Shot[]) =>
    setShots((s) => {
      const all = [...s, ...more]
      all.slice(MAX_PHOTOS).forEach((x) => URL.revokeObjectURL(x.url))
      return all.slice(0, MAX_PHOTOS)
    })
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

  // The person's picture: cut from the first photo core accepted (the
  // straight-on one, from the webcam). Not worth failing an enrolment for.
  // ``sent`` are the photos as enrolled (shrunk), which the face boxes
  // in ``r`` are measured on.
  const keepPicture = async (r: EnrolResult, sent: Blob[]) => {
    const first = r.results.find((x) => x.ok && x.bbox)
    const photo = first ? sent[first.index] : undefined
    if (!first?.bbox || !photo) return
    try {
      const picture = await cutPicture(photo, first.bbox)
      if (!picture) return
      await api.setPhoto(person.id, picture)
      pictureChanged(person.id)
      setPictureVersion((v) => v + 1)
    } catch (e) {
      toast('info', `Enrolled, but the picture was not saved: ${errorText(e)}`)
    }
  }

  const enrol = async () => {
    setBusy(true)
    setError(null)
    try {
      const sent = await Promise.all(shots.map((s) => shrinkPhoto(s.blob)))
      const r = await api.enrol(person.id, sent)
      await keepPicture(r, sent)
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
      <Card className="order-last p-5 lg:order-none lg:col-span-1">
        <h2 className="mb-3 flex items-center gap-2 text-base font-semibold text-gray-900">
          <span className="flex h-6 w-6 items-center justify-center rounded-full bg-indigo-600 text-xs text-white">2</span>
          Enrol a face
        </h2>
        <div className="flex items-start gap-3">
          <Avatar person={{ ...person, has_photo: person.has_photo || pictureVersion > 0 }} size={56} version={pictureVersion} />
        <div className="space-y-1 text-sm">
          <div className="text-lg font-semibold text-gray-900">{person.display_name}</div>
          {person.external_ref && <div className="text-gray-600">ID {person.external_ref}</div>}
          {person.email && <div className="break-all text-gray-600">{person.email}</div>}
          <div className="pt-2">
            <Badge tone={person.face_samples ? 'green' : 'amber'}>
              {person.face_samples} photo{person.face_samples === 1 ? '' : 's'} enrolled
            </Badge>
          </div>
        </div>
        </div>
        <EnrolledPhotos person={person} onChanged={onChanged} />
        <div className="mt-5 rounded-lg bg-gray-50 p-3 text-xs text-gray-600">
          <p className="mb-1 font-medium text-gray-700">For good recognition</p>
          <ul className="list-disc space-y-0.5 pl-4">
            <li>One face per photo, clearly lit from the front, nothing covering it (no cap, mask or hand).</li>
            <li>3 to 5 photos: straight on, turned a little left and right, chin up and down.</li>
            <li>Glasses on if they usually wear them.</li>
            <li>A recent photo of the person themselves, not of a printout or a screen.</li>
          </ul>
          <p className="mt-2">The first accepted photo also becomes their picture in People.</p>
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

            <p className="mb-3 text-sm text-gray-600">
              {mode === 'photos'
                ? 'Choose 3 to 5 clear photos of this person alone: straight on, and turned a little each way. Phone photos are fine.'
                : 'Ask the person to sit in front of the camera. Follow the steps below; each angle is taken by itself.'}
            </p>
            {mode === 'photos' ? (
              <PhotoPicker onAdd={add} room={MAX_PHOTOS - shots.length} />
            ) : (
              <WebcamCapture
                person={person}
                onAdd={add}
                room={MAX_PHOTOS - shots.length}
                taken={shots.map((s) => s.label)}
                onRestart={() =>
                  setShots((all) => {
                    const angles = new Set(ANGLES.map((a) => a.text))
                    all.filter((s) => angles.has(s.label)).forEach((s) => URL.revokeObjectURL(s.url))
                    return all.filter((s) => !angles.has(s.label))
                  })
                }
                onReview={() => review.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })}
              />
            )}

            {shots.length > 0 && (
              <div ref={review} className="mt-5 scroll-mt-20">
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

/** Who the refused face belongs to, said with what tells two people of
 * the same name apart: the ID and e-mail. */
function DuplicateNote({ id, self }: { id: number; self: Person }) {
  const other = useLoad(() => api.person(id), [id])
  const p = other.data
  const name = p ? p.display_name : `person ${id}`
  const details = p ? [p.external_ref && `ID ${p.external_ref}`, p.email].filter(Boolean).join(', ') : ''
  const sameName = p && p.display_name.trim().toLowerCase() === self.display_name.trim().toLowerCase()
  return (
    <div className="mb-3 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
      <p>
        <strong>This face is already enrolled</strong> as{' '}
        <strong>
          {name}
          {details && ` (${details})`}
        </strong>
        {sameName ? ', another record with the same name' : ''}. A face can belong to one person only, so no photo was enrolled.
      </p>
      <p className="mt-1">
        If it is the same person, use that record and remove this one. If it is not, check the photos are of {self.display_name}.
      </p>
      <div className="mt-2">
        <Link className="font-medium text-indigo-700 underline" to={`/register?person=${id}`}>
          Open {name}
          {p?.external_ref ? ` (ID ${p.external_ref})` : ''} →
        </Link>
      </div>
    </div>
  )
}

function EnrolOutcome({ shots, r }: { shots: Shot[]; r: EnrolResult }) {
  const dup = r.results.find((x) => x.duplicate_of)?.duplicate_of ?? null
  return (
    <div className="mt-6 border-t pt-4">
      <div className="mb-2 text-sm font-medium text-gray-700">Last enrolment</div>
      {dup && <DuplicateNote id={dup} self={r.person} />}
      <ul className="space-y-2">
        {r.results.map((x) => {
          const shot = shots[x.index]
          return (
            <li key={x.index} className="flex items-center gap-3 text-sm">
              {shot && <img src={shot.url} alt="" className="h-10 w-10 rounded object-cover" />}
              {x.ok ? <CheckCircle2 className="h-4 w-4 text-green-600" /> : <XCircle className="h-4 w-4 text-red-600" />}
              <span className={x.ok ? 'text-gray-700' : 'text-red-700'}>
                {shot?.label ?? `Photo ${x.index + 1}`}: {x.ok ? 'enrolled' : x.error ?? 'refused'}
                {x.duplicate_of && x.duplicate_of !== dup ? (
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

type Angle = { key: 'front' | 'left' | 'right' | 'up' | 'down'; text: string }

const ANGLES: Angle[] = [
  { key: 'front', text: 'Look straight at the camera' },
  { key: 'left', text: 'Turn your head a little to the left' },
  { key: 'right', text: 'Turn your head a little to the right' },
  { key: 'up', text: 'Lift your chin slightly' },
  { key: 'down', text: 'Lower your chin slightly' },
]

/** The guidance frame: small, since only the face's place is wanted. */
const CHECK_WIDTH = 640
/** Good checks in a row before the photo is taken: the person is still. */
const STEADY = 2
/** Time to take the next pose after a photo. */
const NEXT_POSE_MS = 3000

/** How the head is turned, from the detector's five points (eyes, nose,
 * mouth corners). ``yaw``: the nose's offset from between the eyes, in
 * eye-distances (0 facing the camera). ``pitch``: where the nose sits
 * between the eye line (0) and the mouth line (1). Both are compared with
 * the person's own straight-on photo, never with fixed values: faces
 * differ more than the turns asked for. */
type Pose = { yaw: number; pitch: number }

function poseOf(landmarks: number[][] | null | undefined): Pose | null {
  if (!landmarks || landmarks.length < 5) return null
  const [le, re, nose, ml, mr] = landmarks
  const eyes = Math.hypot(re[0] - le[0], re[1] - le[1])
  const eyeY = (le[1] + re[1]) / 2
  const mouthY = (ml[1] + mr[1]) / 2
  if (eyes < 1 || mouthY - eyeY < 1) return null
  return { yaw: (nose[0] - (le[0] + re[0]) / 2) / eyes, pitch: (nose[1] - eyeY) / (mouthY - eyeY) }
}

/** Turns big enough to give recognition a different view; small enough
 * that the face is still a face. */
const TURN = { yawMin: 0.12, yawMax: 0.6, pitchMin: 0.07, pitchMax: 0.35, frontYaw: 0.1 }

type Hint = { tone: 'fix' | 'good'; text: string }

/** What to tell the person about one checked frame, for ``angle``.
 * ``poses`` are the poses of the photos already taken. Which way is the
 * person's left is not assumed (cameras and mirrors differ): the second
 * of a pair must be the other way from the first. */
function judge(check: FaceCheck, w: number, h: number, angle: Angle, poses: Map<string, Pose>): Hint {
  const faces = check.faces
  if (faces.length === 0) {
    return check.too_small
      ? { tone: 'fix', text: 'Come closer to the camera' }
      : { tone: 'fix', text: 'No face found. Look at the camera, with light on your face' }
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
  // An adapter without landmarks: the place is all that can be checked.
  if (!pose) return { tone: 'good', text: 'Hold still…' }
  if (angle.key === 'front') {
    return Math.abs(pose.yaw) > TURN.frontYaw
      ? { tone: 'fix', text: 'Face the camera straight on' }
      : { tone: 'good', text: 'Hold still…' }
  }
  if (!front) {
    // No straight-on reference (taken before the guide could see it):
    // a turn is judged from facing forward, and up/down is not judged.
    if (angle.key === 'up' || angle.key === 'down') return { tone: 'good', text: 'Hold still…' }
    return judge(check, w, h, angle, new Map([...poses, ['front', { ...pose, yaw: 0 }]]))
  }
  const pair = { left: 'right', right: 'left', up: 'down', down: 'up' } as const
  const other = poses.get(pair[angle.key])
  if (angle.key === 'left' || angle.key === 'right') {
    const turn = pose.yaw - front.yaw
    if (Math.abs(turn) > TURN.yawMax) return { tone: 'fix', text: 'Not so far, just a little' }
    if (other && Math.sign(turn) === Math.sign(other.yaw - front.yaw) && Math.abs(turn) >= TURN.yawMin / 2) {
      return { tone: 'fix', text: 'The other way, please' }
    }
    if (Math.abs(turn) < TURN.yawMin) return { tone: 'fix', text: 'Turn your head a little more' }
    if (Math.abs(pose.pitch - front.pitch) > TURN.pitchMax) return { tone: 'fix', text: 'Keep your chin level' }
    return { tone: 'good', text: 'Hold still…' }
  }
  const tilt = pose.pitch - front.pitch
  if (Math.abs(tilt) > TURN.pitchMax) return { tone: 'fix', text: 'Not so far, just a little' }
  if (other && Math.sign(tilt) === Math.sign(other.pitch - front.pitch) && Math.abs(tilt) >= TURN.pitchMin / 2) {
    return { tone: 'fix', text: angle.key === 'up' ? 'Chin up, not down' : 'Chin down, not up' }
  }
  if (Math.abs(tilt) < TURN.pitchMin) {
    return { tone: 'fix', text: angle.key === 'up' ? 'Lift your chin a little more' : 'Lower your chin a little more' }
  }
  if (Math.abs(pose.yaw - front.yaw) > TURN.yawMin) return { tone: 'fix', text: 'Keep facing the camera' }
  return { tone: 'good', text: 'Hold still…' }
}

const sleep = (ms: number) => new Promise((r) => window.setTimeout(r, ms))

// Sounds, made here (no files): a camera shutter on each photo, a chime
// when the set is done. A browser plays sound only after a click on the
// page, so the context is made on "Start the camera".
let audio: AudioContext | null = null

function unlockSound() {
  try {
    audio ??= new AudioContext()
    void audio.resume()
  } catch {
    audio = null
  }
}

function shutterSound() {
  if (!audio || audio.state !== 'running') return
  const t = audio.currentTime
  // A burst of noise that dies away in 80 ms, twice: the shutter opening
  // and closing.
  const length = Math.floor(audio.sampleRate * 0.08)
  const buffer = audio.createBuffer(1, length, audio.sampleRate)
  const data = buffer.getChannelData(0)
  for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / length, 4)
  for (const [at, level] of [
    [0, 0.35],
    [0.06, 0.25],
  ]) {
    const src = audio.createBufferSource()
    const gain = audio.createGain()
    const tone = audio.createBiquadFilter()
    tone.type = 'bandpass'
    tone.frequency.value = 2500
    src.buffer = buffer
    gain.gain.value = level
    src.connect(tone).connect(gain).connect(audio.destination)
    src.start(t + at)
  }
}

function doneSound() {
  if (!audio || audio.state !== 'running') return
  const t = audio.currentTime
  for (const [at, freq] of [
    [0, 660],
    [0.14, 880],
  ]) {
    const osc = audio.createOscillator()
    const gain = audio.createGain()
    osc.type = 'sine'
    osc.frequency.value = freq
    gain.gain.setValueAtTime(0.0001, t + at)
    gain.gain.exponentialRampToValueAtTime(0.2, t + at + 0.02)
    gain.gain.exponentialRampToValueAtTime(0.0001, t + at + 0.35)
    osc.connect(gain).connect(audio.destination)
    osc.start(t + at)
    osc.stop(t + at + 0.4)
  }
}

// Spoken guidance, with the browser's own speech (no files, no server):
// the person in front of the camera is often not the one reading the
// screen. A voice installed on the computer is preferred, so the words do
// not leave it; a browser that has only an online voice uses that (the
// words are these fixed instructions, nothing about the person).
const speech = typeof window !== 'undefined' && 'speechSynthesis' in window ? window.speechSynthesis : null
let voice: SpeechSynthesisVoice | null = null
let lastSaid = ''
let lastSaidAt = 0
/** The same correction is not repeated sooner than this. */
const REPEAT_MS = 6000

function pickVoice(): SpeechSynthesisVoice | null {
  if (!speech) return null
  const all = speech.getVoices()
  const lang = (document.documentElement.lang || navigator.language || 'en').toLowerCase()
  const base = lang.split('-')[0]
  const score = (v: SpeechSynthesisVoice) =>
    (v.lang.toLowerCase() === lang ? 4 : v.lang.toLowerCase().startsWith(base) ? 2 : 0) + (v.localService ? 1 : 0)
  return all.length ? all.reduce((best, v) => (score(v) > score(best) ? v : best)) : null
}
speech?.addEventListener?.('voiceschanged', () => {
  voice = pickVoice()
})

/** Say ``text``. An instruction (``now``) cuts off whatever is being said;
 * a correction waits its turn and is dropped if something is still being
 * said, or if it was just said: the checks come about once a second, far
 * faster than anyone can be talked to. */
function say(text: string, now = false) {
  if (!speech) return
  const words = text.replace(/[…!]/g, '').trim()
  if (!words) return
  if (now) speech.cancel()
  else if (speech.speaking || speech.pending || (words === lastSaid && Date.now() - lastSaidAt < REPEAT_MS)) return
  voice ??= pickVoice()
  const u = new SpeechSynthesisUtterance(words)
  if (voice) {
    u.voice = voice
    u.lang = voice.lang
  }
  u.rate = 0.95
  lastSaid = words
  lastSaidAt = Date.now()
  speech.speak(u)
}

function hush() {
  speech?.cancel()
  lastSaid = ''
}

/** "Suraj (ID 1001, suraj@example.com)": enough to tell two records of
 * the same name apart. */
function dupName(d: Duplicate): string {
  const details = [d.external_ref && `ID ${d.external_ref}`, d.email].filter(Boolean).join(', ')
  return `${d.display_name ?? 'someone else'}${details ? ` (${details})` : ''}`
}

function WebcamCapture({
  person,
  onAdd,
  room,
  taken,
  onRestart,
  onReview,
}: {
  person: Person
  onAdd: (s: Shot[]) => void
  room: number
  /** Labels of the photos waiting to be enrolled: an angle whose photo
   * was deleted is taken again. */
  taken: string[]
  onRestart: () => void
  onReview: () => void
}) {
  const box = useRef<HTMLDivElement>(null)
  const video = useRef<HTMLVideoElement>(null)
  const stream = useRef<MediaStream | null>(null)
  const poses = useRef(new Map<string, Pose>())
  /** The pose in the latest check, and when: a photo taken by hand (the
   * button, the timer) records it too, or the straight-on reference would
   * be missing and every turn after it would pass unchecked. */
  const lastPose = useRef<{ pose: Pose; at: number } | null>(null)
  /** Set by an automatic photo: the next angle starts with time to pose. */
  const justTook = useRef(false)
  const [state, setState] = useState<'idle' | 'starting' | 'live' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)
  const [flash, setFlash] = useState(false)
  const [countdown, setCountdown] = useState<number | null>(null)
  const [auto, setAuto] = useState(true)
  const [sound, setSound] = useState(true)
  const soundRef = useRef(sound)
  soundRef.current = sound
  const [voiceOn, setVoiceOn] = useState(!!speech)
  const voiceRef = useRef(voiceOn)
  voiceRef.current = voiceOn
  const [hint, setHint] = useState<Hint | null>(null)
  const [nextIn, setNextIn] = useState<number | null>(null)
  /** Somebody else already enrolled with this face, found in a photo just
   * taken: taking the rest would only have the whole set refused. */
  const [dup, setDup] = useState<Duplicate | null>(null)
  const secure = typeof window !== 'undefined' && window.isSecureContext && !!navigator.mediaDevices?.getUserMedia

  const done = new Set(ANGLES.filter((a) => taken.includes(a.text)).map((a) => a.key))
  const doneRef = useRef(done)
  doneRef.current = done
  // A deleted photo's pose no longer counts as a reference.
  for (const key of [...poses.current.keys()]) if (!done.has(key as Angle['key'])) poses.current.delete(key)
  const angle = ANGLES.find((a) => !done.has(a.key)) ?? null
  const angleRef = useRef(angle)
  angleRef.current = angle

  const stop = () => {
    stream.current?.getTracks().forEach((t) => t.stop())
    stream.current = null
  }
  useEffect(
    () => () => {
      stop()
      hush()
    },
    [],
  )

  const start = async () => {
    setState('starting')
    setError(null)
    try {
      const s = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' },
        audio: false,
      })
      stream.current = s
      // The click on "Start the camera" is what lets the page play sound.
      unlockSound()
      if (video.current) {
        video.current.srcObject = s
        await video.current.play()
      }
      setState('live')
      // Bring the picture and its controls into view: their bottom at the
      // window's bottom, but never the picture's top under the header.
      window.requestAnimationFrame(() => {
        const r = box.current?.getBoundingClientRect()
        if (!r) return
        const header = 80
        const by = Math.min(r.bottom - window.innerHeight + 16, r.top - header)
        if (by > 0) window.scrollBy({ top: by, behavior: 'smooth' })
      })
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

  /** The video's current frame as a JPEG, at most ``width`` wide. */
  const grab = (width: number, quality: number): Promise<{ blob: Blob; w: number; h: number } | null> => {
    const v = video.current
    if (!v || !v.videoWidth) return Promise.resolve(null)
    const scale = Math.min(1, width / v.videoWidth)
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(v.videoWidth * scale)
    canvas.height = Math.round(v.videoHeight * scale)
    canvas.getContext('2d')?.drawImage(v, 0, 0, canvas.width, canvas.height)
    return new Promise((resolve) =>
      canvas.toBlob((blob) => resolve(blob ? { blob, w: canvas.width, h: canvas.height } : null), 'image/jpeg', quality),
    )
  }

  /** Take the photo for the current angle; ``pose`` is how the head was
   * turned in it, when known. Past the five angles, extra photos are
   * labelled as such. */
  const capture = async (pose?: Pose | null) => {
    const shot = await grab(Number.MAX_SAFE_INTEGER, 0.92)
    if (!shot) return
    const a = angleRef.current
    const recent = lastPose.current && Date.now() - lastPose.current.at < 2000 ? lastPose.current.pose : null
    const taken = pose ?? recent
    if (a && taken) poses.current.set(a.key, taken)
    const label = a ? a.text : `Extra photo ${new Date().toLocaleTimeString()}`
    onAdd([{ blob: shot.blob, url: URL.createObjectURL(shot.blob), label }])
    // Asked of each photo while the next pose is being taken (~0.5 s, well
    // inside the time to pose); a failure here is not worth stopping for:
    // enrolling checks again.
    const personId = person.id
    api
      .checkDuplicate(personId, shot.blob)
      .then(({ duplicate }) => {
        if (!duplicate) return
        setDup((d) => d ?? duplicate)
        if (voiceRef.current) {
          const who = duplicate.display_name
            ? `${duplicate.display_name}${duplicate.external_ref ? `, ID ${duplicate.external_ref}` : ''}`
            : 'someone else'
          window.setTimeout(() => say(`Please stop. This face is already registered as ${who}.`, true), 400)
        }
      })
      .catch(() => {})
    setFlash(true)
    window.setTimeout(() => setFlash(false), 150)
    if (soundRef.current) {
      shutterSound()
      // The last angle: a chime after the shutter says the set is done.
      if (a && ANGLES.every((x) => x === a || doneRef.current.has(x.key))) window.setTimeout(doneSound, 350)
    }
    if (voiceRef.current && a && ANGLES.every((x) => x === a || doneRef.current.has(x.key))) {
      window.setTimeout(() => say('All done. Thank you.', true), 700)
    }
  }

  // Taking photos by hand: the pose is said when it changes; the person
  // holds it for whoever presses Capture.
  useEffect(() => {
    if (state === 'live' && !auto && angle && voiceRef.current) say(angle.text, true)
  }, [state, auto, angle?.key]) // eslint-disable-line react-hooks/exhaustive-deps

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

  // Automatic capture: check a small frame about once a second, say what
  // to change, and take the photo once one face has been well placed and
  // turned as asked for STEADY checks in a row. One check in flight at a
  // time. Restarts for each angle, so a deleted photo is taken again.
  const running = state === 'live' && auto && room > 0 && angle !== null && dup === null
  const key = angle?.key ?? null
  useEffect(() => {
    if (!running || key === null) {
      setHint(null)
      return
    }
    let cancelled = false
    ;(async () => {
      // The pose is said at the start, so it is heard during the time to
      // take it. After a photo, the shutter goes first.
      const a0 = angleRef.current
      if (a0 && voiceRef.current) {
        if (justTook.current) window.setTimeout(() => !cancelled && voiceRef.current && say(a0.text, true), 400)
        else say(a0.text, true)
      }
      if (justTook.current) {
        justTook.current = false
        for (let left = NEXT_POSE_MS; left > 0 && !cancelled; left -= 1000) {
          setNextIn(Math.ceil(left / 1000))
          await sleep(1000)
        }
        setNextIn(null)
      }
      let steady = 0
      let lastFix = ''
      while (!cancelled) {
        const frame = await grab(CHECK_WIDTH, 0.8)
        if (!frame) {
          await sleep(500)
          continue
        }
        let verdict: Hint
        let pose: Pose | null = null
        try {
          const check = await api.checkFace(frame.blob)
          const a = angleRef.current
          if (!a) return
          verdict = judge(check, check.frame.w || frame.w, check.frame.h || frame.h, a, poses.current)
          pose = check.faces.length === 1 ? poseOf(check.faces[0].landmarks) : null
          lastPose.current = pose ? { pose, at: Date.now() } : null
        } catch (e) {
          if (cancelled) return
          setHint({ tone: 'fix', text: `Cannot check the face right now: ${errorText(e)}` })
          await sleep(3000)
          continue
        }
        if (cancelled) return
        steady = verdict.tone === 'good' ? steady + 1 : 0
        setHint(verdict)
        // A correction is said only once it has been seen twice running: a
        // single odd frame is not worth a word.
        if (voiceRef.current && verdict.tone === 'fix' && verdict.text === lastFix) say(verdict.text)
        lastFix = verdict.text
        if (steady < STEADY) {
          await sleep(400)
          continue
        }
        // The photo moves the page to the next angle, which restarts this
        // loop, starting with time to take the pose.
        justTook.current = true
        setHint({ tone: 'good', text: 'Got it!' })
        await capture(pose)
        return
      }
    })()
    return () => {
      cancelled = true
      setNextIn(null)
    }
  }, [running, key]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!secure) {
    return (
      <Empty icon={<Camera className="h-10 w-10" />} title="The webcam needs a secure connection">
        Browsers only allow the camera on https:// pages. Open this page over HTTPS, or upload photos instead.
      </Empty>
    )
  }

  const waiting = nextIn !== null
  const ring = waiting ? 'border-white/70' : hint?.tone === 'good' ? 'border-green-400' : hint?.tone === 'fix' ? 'border-amber-300' : 'border-white/70'

  return (
    <div>
      <ol className="mb-3 list-decimal space-y-0.5 pl-5 text-sm text-gray-700">
        <li>Sit facing the camera, about an arm's length away, with light in front of you (not a window behind).</li>
        <li>Take off a cap or mask. Keep glasses on if you usually wear them.</li>
        <li>
          Put your face in the oval and do what the top of the picture says.{' '}
          {auto ? 'Each photo is taken by itself once your face is placed and turned as asked.' : 'Press Capture, or use the timer.'}
        </li>
      </ol>
      {/* As large as fits under the header with the controls still in view. */}
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
                  {dup
                    ? 'Already registered'
                    : !angle
                    ? 'All angles taken'
                    : waiting
                      ? `Next: ${angle.text.toLowerCase()}… ${nextIn}`
                      : `${ANGLES.indexOf(angle) + 1} of ${ANGLES.length}: ${angle.text}`}
                </div>
              </div>
              {hint && angle && !waiting && (
                <div className="absolute inset-x-0 bottom-0 flex justify-center p-2 sm:p-3">
                  <span
                    aria-live="polite"
                    className={`rounded-full px-3 py-1 text-xs font-medium shadow sm:text-sm ${hint.tone === 'good' ? 'bg-green-600 text-white' : 'bg-amber-100 text-amber-900'}`}
                  >
                    {hint.text}
                  </span>
                </div>
              )}
              {countdown !== null && (
                <div className="absolute inset-0 flex items-center justify-center text-7xl font-bold text-white drop-shadow">{countdown}</div>
              )}
              {flash && <div className="absolute inset-0 bg-white/70" />}
              {dup && (
                <div className="absolute inset-0 flex items-center justify-center bg-black/55 p-4">
                  <div role="alert" className="max-w-md rounded-xl bg-white p-4 text-center text-sm text-gray-800 shadow-lg">
                    <XCircle className="mx-auto mb-2 h-8 w-8 text-red-600" />
                    <div className="text-base font-semibold text-gray-900">This face is already registered</div>
                    <div className="mt-1">
                      as <strong>{dupName(dup)}</strong>
                      {dup.display_name && dup.display_name.trim().toLowerCase() === person.display_name.trim().toLowerCase()
                        ? ', another record with the same name'
                        : ''}
                      . A face can belong to one person only, so these photos cannot be enrolled for {person.display_name}.
                    </div>
                    <div className="mt-3 flex flex-wrap justify-center gap-2">
                      {dup.id !== null && (
                        <Link
                          to={`/register?person=${dup.id}`}
                          className="rounded-lg bg-indigo-600 px-3 py-1.5 font-medium text-white hover:bg-indigo-700"
                        >
                          Open {dup.display_name}
                          {dup.external_ref ? ` (ID ${dup.external_ref})` : ''}
                        </Link>
                      )}
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => {
                          hush()
                          setDup(null)
                          onRestart()
                        }}
                      >
                        <RotateCcw className="h-3.5 w-3.5" /> Start over with someone else
                      </Button>
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
              <Button onClick={start} busy={state === 'starting'}>
                Start the camera
              </Button>
            </div>
          )}
        </div>
        {state === 'live' && (
          <>
            {!angle && (
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg bg-green-50 p-3 text-sm text-green-800">
                <span>All {ANGLES.length} angles are taken. Check the photos, remove any that are blurred, then enrol.</span>
                <Button size="sm" onClick={onReview}>
                  Review and enrol ↓
                </Button>
              </div>
            )}
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
              <div className="flex flex-wrap items-center gap-3">
                <div className="flex gap-1">
                  {ANGLES.map((a) => (
                    <span
                      key={a.key}
                      title={a.text}
                      className={`h-2 w-6 rounded-full sm:w-8 ${done.has(a.key) ? 'bg-green-500' : a === angle ? 'bg-indigo-500' : 'bg-gray-200'}`}
                    />
                  ))}
                </div>
                <label className="flex items-center gap-1.5 text-sm text-gray-700">
                  <input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={auto} onChange={(e) => setAuto(e.target.checked)} />
                  Take photos automatically
                </label>
                <label className="flex items-center gap-1.5 text-sm text-gray-700">
                  <input
                    type="checkbox"
                    className="h-4 w-4 accent-indigo-600"
                    checked={sound}
                    onChange={(e) => {
                      setSound(e.target.checked)
                      if (e.target.checked) unlockSound()
                    }}
                  />
                  Sound
                </label>
                {speech && (
                  <label className="flex items-center gap-1.5 text-sm text-gray-700" title="Say each step and correction out loud">
                    <input
                      type="checkbox"
                      className="h-4 w-4 accent-indigo-600"
                      checked={voiceOn}
                      onChange={(e) => {
                        setVoiceOn(e.target.checked)
                        if (e.target.checked) {
                          if (angle) say(angle.text, true)
                        } else hush()
                      }}
                    />
                    Voice
                  </label>
                )}
              </div>
              <div className="flex gap-2">
                <Button variant="ghost" size="sm" onClick={onRestart} disabled={!done.size}>
                  <RotateCcw className="h-3.5 w-3.5" /> Start over
                </Button>
                {!auto && (
                  <>
                    <Button variant="secondary" onClick={() => setCountdown(3)} disabled={room <= 0 || countdown !== null}>
                      Timer (3 s)
                    </Button>
                    <Button onClick={() => capture()} disabled={room <= 0 || countdown !== null}>
                      <Camera className="h-4 w-4" /> Capture
                    </Button>
                  </>
                )}
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

/** The photos the person is enrolled with: what recognition compares
 * against. Each can be looked at and deleted; samples enrolled before
 * photos were kept count, but have nothing to show. */
function EnrolledPhotos({ person, onChanged }: { person: Person; onChanged: () => void }) {
  const { toast, confirm } = useFeedback()
  const list = useLoad(() => api.enrolledPhotos(person.id), [person.id, person.face_samples])
  const [urls, setUrls] = useState<Record<number, string>>({})
  const [open, setOpen] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let live = true
    const made: string[] = []
    const kept = (list.data?.photos ?? []).filter((p) => p.kept)
    Promise.all(
      kept.map((p) =>
        api
          .enrolledPhoto(person.id, p.index)
          .then((b) => (b ? [p.index, URL.createObjectURL(b)] : null))
          .catch(() => null),
      ),
    ).then((pairs) => {
      const next: Record<number, string> = {}
      for (const pair of pairs) {
        if (!pair) continue
        const [i, u] = pair as [number, string]
        made.push(u)
        next[i] = u
      }
      if (live) setUrls(next)
      else made.forEach((u) => URL.revokeObjectURL(u))
    })
    return () => {
      live = false
      made.forEach((u) => URL.revokeObjectURL(u))
    }
  }, [list.data, person.id])

  const photos = list.data?.photos ?? []
  const old = photos.filter((p) => !p.kept).length

  const removeOne = async (index: number) => {
    const ok = await confirm({
      title: 'Delete this photo?',
      body: `${person.display_name} will no longer be matched against this photo. Their other photos stay.`,
      confirm: 'Delete photo',
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      await api.forgetFace(person.id, index)
      setOpen(null)
      toast('success', 'Photo deleted')
      onChanged()
    } catch (e) {
      toast('error', errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const removeAll = async () => {
    const ok = await confirm({
      title: 'Delete all face photos?',
      body: `All ${person.face_samples} enrolled photos of ${person.display_name} are deleted, and they are not recognised until new ones are enrolled. Consent and past attendance are kept.`,
      confirm: 'Delete all',
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      await api.forgetFaces(person.id)
      pictureChanged(person.id)
      toast('success', 'All face photos deleted')
      onChanged()
    } catch (e) {
      toast('error', errorText(e))
    } finally {
      setBusy(false)
    }
  }

  if (!person.face_samples) {
    return <p className="mt-4 text-sm text-gray-500">No photos enrolled yet. Enrol some on the right.</p>
  }

  return (
    <div className="mt-4">
      <div className="mb-2 flex items-center justify-between">
        <span className="text-sm font-medium text-gray-700">Enrolled photos</span>
        <Button variant="ghost" size="sm" onClick={removeAll} busy={busy}>
          <Trash2 className="h-3.5 w-3.5" /> Delete all
        </Button>
      </div>
      {list.error ? (
        <ErrorNote message={list.error} onRetry={list.reload} />
      ) : (
        <div className="grid grid-cols-4 gap-2">
          {photos.map((p) =>
            p.kept && urls[p.index] ? (
              <button
                key={p.index}
                onClick={() => setOpen(p.index)}
                className="overflow-hidden rounded-lg border bg-gray-100 hover:ring-2 hover:ring-indigo-400"
                title={p.enrolled_at ? `Enrolled ${new Date(p.enrolled_at).toLocaleString()}` : undefined}
              >
                <img src={urls[p.index]} alt={`Enrolled photo ${p.index + 1}`} className="aspect-square w-full object-cover" />
              </button>
            ) : (
              <div
                key={p.index}
                className="flex aspect-square items-center justify-center rounded-lg border border-dashed bg-gray-50 text-gray-400"
                title={p.kept ? 'Loading' : 'Enrolled before photos were kept'}
              >
                <ImagePlus className="h-5 w-5" />
              </div>
            ),
          )}
        </div>
      )}
      {old > 0 && (
        <p className="mt-2 text-xs text-gray-500">
          {old} {old === 1 ? 'photo was' : 'photos were'} enrolled before photos were kept, so {old === 1 ? 'it' : 'they'} cannot be
          shown. To replace them, delete all and enrol again.
        </p>
      )}
      {open !== null && urls[open] && (
        <Modal
          title={`Enrolled photo ${open + 1} of ${photos.length}`}
          onClose={() => setOpen(null)}
          wide
          footer={
            <>
              <Button variant="secondary" onClick={() => setOpen(null)}>
                Close
              </Button>
              <Button variant="danger" onClick={() => removeOne(open)} busy={busy} disabled={photos.length <= 1}>
                <Trash2 className="h-4 w-4" /> Delete this photo
              </Button>
            </>
          }
        >
          <img src={urls[open]} alt="" className="mx-auto max-h-[60vh] rounded-lg" />
          {photos.length <= 1 && (
            <p className="mt-2 text-sm text-gray-600">This is their only photo. To remove it, use Delete all.</p>
          )}
        </Modal>
      )}
    </div>
  )
}
