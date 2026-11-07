/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// People: who attendance is taken for, their consent, and their faces.
// Faces are biometric data: nothing can be enrolled before consent is
// recorded, and withdrawing it or removing the person deletes them.

import { useEffect, useRef, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Camera, Plus, Trash2, Upload, UserCheck } from 'lucide-react'
import { Modal } from '../../components/Modal'
import { Badge, Button, EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { useConfirm } from '../../components/ui/ConfirmDialog'
import { Field, Input } from '../../components/ui/form'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import { attendanceService, type EnrolResult, type Person } from '../../services/attendanceService'
import { groupName, useAttendancePermissions, useGroups } from './shared'

const PAGE = 50

export function PeopleTab() {
  const qc = useQueryClient()
  const groups = useGroups()
  const { canEnroll } = useAttendancePermissions()
  const { showError, showSuccess } = useSnackbar()
  const confirm = useConfirm()
  const [q, setQ] = useState('')
  const [offset, setOffset] = useState(0)
  const [adding, setAdding] = useState(false)
  const [enrolling, setEnrolling] = useState<Person | null>(null)

  const list = useQuery({
    queryKey: ['attendance', 'people', q, offset],
    queryFn: () => attendanceService.listPeople({ q: q || undefined, limit: PAGE, offset }),
    placeholderData: keepPreviousData,
  })
  const refresh = () => qc.invalidateQueries({ queryKey: ['attendance', 'people'] })

  const consent = useMutation({
    mutationFn: ({ p, given }: { p: Person; given: boolean }) => attendanceService.setConsent(p.id, given),
    onSuccess: refresh,
    onError: (e) => showError(extractApiError(e, 'Could not record consent')),
  })
  const forget = useMutation({
    mutationFn: (p: Person) => attendanceService.forgetFaces(p.id),
    onSuccess: () => { refresh(); showSuccess('Faces deleted') },
    onError: (e) => showError(extractApiError(e, 'Could not delete the faces')),
  })
  const remove = useMutation({
    mutationFn: (p: Person) => attendanceService.removePerson(p.id),
    onSuccess: () => { refresh(); qc.invalidateQueries({ queryKey: ['attendance', 'groups'] }) },
    onError: (e) => showError(extractApiError(e, 'Could not remove the person')),
  })

  async function withdraw(p: Person) {
    const ok = await confirm({
      title: `Withdraw ${p.display_name}'s consent?`,
      message: 'Their enrolled faces are deleted, and attendance can no longer recognise them.',
      confirmLabel: 'Withdraw', danger: true,
    })
    if (ok) consent.mutate({ p, given: false })
  }

  async function removeOne(p: Person) {
    const ok = await confirm({
      title: `Remove ${p.display_name}?`,
      message: 'Their faces are deleted and they leave every group. Past attendance keeps their name.',
      confirmLabel: 'Remove', danger: true,
    })
    if (ok) remove.mutate(p)
  }

  const rows = list.data?.people ?? []
  const total = list.data?.total ?? 0

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Search">
          <Input placeholder="Name, reference or e-mail" value={q}
                 onChange={(e) => { setQ(e.target.value); setOffset(0) }} />
        </Field>
        {canEnroll && (
          <Button variant="primary" className="ml-auto" onClick={() => setAdding(true)}>
            <Plus size={14} /> Add person
          </Button>
        )}
      </div>
      {list.isLoading && <Skeleton className="h-40" />}
      {list.isError && <ErrorCard message={extractApiError(list.error, 'Could not load people')} />}
      {list.isSuccess && rows.length === 0 && (
        <EmptyState title={q ? 'Nobody matches' : 'No people yet'}
                    description="Add the people attendance is taken for, record their consent, then enrol a few photos of each." />
      )}
      {rows.length > 0 && (
        <table className="w-full text-sm [&_th]:px-2 [&_td]:px-2">
          <thead className="text-left text-[var(--text-dim)]">
            <tr><th className="py-1">Name</th><th>Reference</th><th>Groups</th><th>Consent</th><th>Faces</th><th /></tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.id} className="border-t border-[var(--border)]">
                <td className="py-1">{p.display_name}{p.active ? '' : ' (inactive)'}</td>
                <td>{p.external_ref ?? ''}</td>
                <td className="max-w-48 truncate">{p.group_ids.map((g) => groupName(groups.data, g)).join(', ')}</td>
                <td>{p.consent_at
                  ? <Badge variant="success">Given</Badge>
                  : <Badge variant="neutral">Not recorded</Badge>}</td>
                <td>{p.face_samples || '—'}</td>
                <td className="flex justify-end gap-1 py-1">
                  {canEnroll && !p.consent_at && (
                    <Button size="sm" variant="outline" onClick={() => consent.mutate({ p, given: true })}>
                      <UserCheck size={14} /> Record consent
                    </Button>
                  )}
                  {canEnroll && p.consent_at && (
                    <>
                      <Button size="sm" variant="outline" onClick={() => setEnrolling(p)}>
                        <Camera size={14} /> Enrol faces
                      </Button>
                      {p.face_samples > 0 && (
                        <Button size="sm" variant="ghost" onClick={() => forget.mutate(p)}>Delete faces</Button>
                      )}
                      <Button size="sm" variant="ghost" onClick={() => withdraw(p)}>Withdraw consent</Button>
                    </>
                  )}
                  {canEnroll && (
                    <Button size="sm" variant="ghost" aria-label={`Remove ${p.display_name}`}
                            onClick={() => removeOne(p)}><Trash2 size={14} /></Button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {total > PAGE && (
        <div className="flex items-center gap-2 text-sm">
          <Button size="sm" variant="outline" disabled={offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</Button>
          <span>{offset + 1}–{Math.min(offset + PAGE, total)} of {total}</span>
          <Button size="sm" variant="outline" disabled={offset + PAGE >= total}
                  onClick={() => setOffset(offset + PAGE)}>Next</Button>
        </div>
      )}
      {adding && <AddPerson onClose={() => setAdding(false)} onAdded={refresh} />}
      {enrolling && (
        <EnrolFaces person={enrolling} onClose={() => setEnrolling(null)} onDone={refresh} />
      )}
    </div>
  )
}

function AddPerson({ onClose, onAdded }: { onClose: () => void; onAdded: () => void }) {
  const { showError } = useSnackbar()
  const [name, setName] = useState('')
  const [ref, setRef] = useState('')
  const [email, setEmail] = useState('')
  const add = useMutation({
    mutationFn: () => attendanceService.createPerson({
      display_name: name, external_ref: ref || null, email: email || null,
    }),
    onSuccess: () => { onAdded(); onClose() },
    onError: (e) => showError(extractApiError(e, 'Could not add the person')),
  })
  return (
    <Modal open title="Add person" onClose={onClose}
           footer={<Button variant="primary" disabled={!name.trim() || add.isPending}
                           onClick={() => add.mutate()}>Add</Button>}>
      <div className="space-y-3">
        <Field label="Name" required><Input autoFocus value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Reference" hint="Roll number, USN or employee id: unique among current people">
          <Input value={ref} onChange={(e) => setRef(e.target.value)} />
        </Field>
        <Field label="E-mail"><Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} /></Field>
      </div>
    </Modal>
  )
}

const ANGLES = ['Look straight at the camera', 'Turn slightly to your left', 'Turn slightly to your right',
  'Chin slightly up', 'Chin slightly down']

function EnrolFaces({ person, onClose, onDone }: {
  person: Person
  onClose: () => void
  onDone: () => void
}) {
  const { showError, showSuccess } = useSnackbar()
  const [photos, setPhotos] = useState<Blob[]>([])
  const [result, setResult] = useState<EnrolResult | null>(null)
  const [webcam, setWebcam] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  const send = useMutation({
    mutationFn: () => attendanceService.enrol(person.id, photos),
    onSuccess: (r) => {
      setResult(r)
      onDone()
      if (r.enrolled === photos.length) showSuccess(`${r.enrolled} photo(s) enrolled`)
    },
    onError: (e) => showError(extractApiError(e, 'Could not enrol the photos')),
  })

  return (
    <Modal open title={`Enrol ${person.display_name}`} onClose={onClose} widthClassName="max-w-2xl"
           footer={
             <Button variant="primary" disabled={!photos.length || photos.length > 10 || send.isPending}
                     onClick={() => send.mutate()}>
               Enrol {photos.length || ''} photo{photos.length === 1 ? '' : 's'}
             </Button>}>
      <div className="space-y-3 text-sm">
        <p>
          Three to five photos, one face in each: straight on, slightly left and right, with
          and without glasses. Good light on the face matters more than resolution.
          {person.face_samples > 0 && ` ${person.face_samples} already enrolled; these are added.`}
        </p>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => fileRef.current?.click()}>
            <Upload size={14} /> Choose photos
          </Button>
          <Button variant="outline" onClick={() => setWebcam((w) => !w)}>
            <Camera size={14} /> {webcam ? 'Close webcam' : 'Use webcam'}
          </Button>
          <input ref={fileRef} type="file" accept="image/jpeg,image/png" multiple hidden
                 onChange={(e) => setPhotos((ps) => [...ps, ...Array.from(e.target.files ?? [])].slice(0, 10))} />
        </div>
        {webcam && (
          <Webcam hint={ANGLES[Math.min(photos.length, ANGLES.length - 1)]}
                  onShot={(b) => setPhotos((ps) => [...ps, b].slice(0, 10))} />
        )}
        {photos.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {photos.map((p, i) => (
              <Thumb key={i} blob={p} status={result?.results[i]}
                     onRemove={result ? undefined : () => setPhotos((ps) => ps.filter((_, j) => j !== i))} />
            ))}
          </div>
        )}
        {result && (
          <p>{result.enrolled} of {result.results.length} enrolled.
            {result.enrolled < result.results.length && ' The others are listed with the reason.'}</p>
        )}
      </div>
    </Modal>
  )
}

function Thumb({ blob, status, onRemove }: {
  blob: Blob
  status?: { ok: boolean; error?: string }
  onRemove?: () => void
}) {
  const [url, setUrl] = useState('')
  useEffect(() => {
    const u = URL.createObjectURL(blob)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [blob])
  return (
    <div className="w-28 text-xs">
      <img src={url} alt="" className="h-20 w-28 object-cover" />
      {status && (status.ok
        ? <span className="text-[var(--ok)]">Enrolled</span>
        : <span className="text-[var(--danger)]">{status.error}</span>)}
      {onRemove && <button type="button" className="underline" onClick={onRemove}>Remove</button>}
    </div>
  )
}

function Webcam({ hint, onShot }: { hint: string; onShot: (b: Blob) => void }) {
  const video = useRef<HTMLVideoElement>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    let stream: MediaStream | null = null
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setError('The browser only allows the webcam over HTTPS (or on localhost). Choose photos instead.')
      return
    }
    navigator.mediaDevices.getUserMedia({ video: { width: 1280, height: 720 } })
      .then((s) => {
        stream = s
        if (video.current) video.current.srcObject = s
      })
      .catch(() => setError('Could not open the webcam (permission refused, or none connected).'))
    return () => stream?.getTracks().forEach((t) => t.stop())
  }, [])

  function shoot() {
    const v = video.current
    if (!v || !v.videoWidth) return
    const c = document.createElement('canvas')
    c.width = v.videoWidth
    c.height = v.videoHeight
    c.getContext('2d')?.drawImage(v, 0, 0)
    c.toBlob((b) => { if (b) onShot(b) }, 'image/jpeg', 0.92)
  }

  if (error) return <p className="text-[var(--danger)]">{error}</p>
  return (
    <div className="space-y-2">
      <video ref={video} autoPlay playsInline muted className="max-h-64 bg-black" />
      <div className="flex items-center gap-3">
        <Button onClick={shoot}><Camera size={14} /> Take photo</Button>
        <span>{hint}</span>
      </div>
    </div>
  )
}
