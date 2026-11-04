// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// One session: everyone in the group, their mark, and how the cameras
// came to it. A person's say overrides the cameras', and survives a
// recompute; "From the cameras" hands the mark back.

import { useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router-dom'
import { AlertTriangle, ArrowLeft, RefreshCw } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { STATUS_LABEL, api, cameraNames, clock, groupName, longDate, type MarkStatus, type SessionPerson } from '../attendance'
import { useFeedback } from '../components/feedback'
import { Badge, Button, Card, ErrorNote, Loading, MarkBadge, Table, inlineInputClass, inputClass, td, th } from '../components/ui'
import { useCameras, useGroups, useLoad } from '../hooks'
import { SESSION_STATUS } from './Attendance'

const CHOICES: (MarkStatus | 'AUTO')[] = ['PRESENT', 'LATE', 'ABSENT', 'EXCUSED', 'AUTO']

export default function SessionPage() {
  const id = Number(useParams().id)
  const { can } = useAuth()
  const { toast, confirm } = useFeedback()
  const groups = useGroups()
  const cameras = useCameras()
  const s = useLoad(() => api.session(id), [id], 30000)
  const [busy, setBusy] = useState<number | 'all' | null>(null)
  const [noteFor, setNoteFor] = useState<SessionPerson | null>(null)

  if (s.error) return <ErrorNote message={s.error} onRetry={s.reload} />
  if (!s.data) return <Loading />
  const d = s.data
  const tz = groups.data?.find((g) => g.id === d.group_id)?.timezone
  const st = SESSION_STATUS[d.status]
  const decided = d.status === 'final'

  const mark = async (p: SessionPerson, status: MarkStatus | 'AUTO', note?: string) => {
    setBusy(p.person_id)
    try {
      await api.setMark(d.id, p.person_id, status, note)
      toast('success', status === 'AUTO' ? `${p.display_name}: back to the cameras' mark` : `${p.display_name}: ${STATUS_LABEL[status]}`)
      s.reload()
    } catch (e) {
      toast('error', errorText(e))
    } finally {
      setBusy(null)
    }
  }

  const recompute = async () => {
    const ok = await confirm({
      title: 'Recompute from the cameras?',
      body: "Every mark the cameras made is worked out again from what they saw. Marks a person set stay as they are.",
      confirm: 'Recompute',
    })
    if (!ok) return
    setBusy('all')
    try {
      await api.refinalize(d.id)
      toast('success', 'Recomputed')
      s.reload()
    } catch (e) {
      toast('error', errorText(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <Link to="/attendance" className="mb-3 inline-flex items-center gap-1 text-sm text-indigo-700 hover:underline">
        <ArrowLeft className="h-4 w-4" /> All sessions
      </Link>
      <Card className="mb-4 p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-gray-900">{groupName(groups.data, d.group_id)}</h1>
            <p className="mt-1 text-sm text-gray-600">
              {longDate(d.local_date)} · {clock(d.start_at, tz)}–{clock(d.end_at, tz)} · {cameraNames(cameras.data, d.camera_ids)}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Badge tone={st.tone}>{st.label}</Badge>
            {can.manage && decided && (
              <Button variant="secondary" size="sm" onClick={recompute} busy={busy === 'all'}>
                <RefreshCw className="h-3.5 w-3.5" /> Recompute
              </Button>
            )}
          </div>
        </div>
        <div className="mt-4 grid gap-3 text-sm sm:grid-cols-4">
          <Fact label="Present" value={d.counts.PRESENT ?? 0} tone="text-green-700" />
          <Fact label="Late" value={d.counts.LATE ?? 0} tone="text-amber-700" />
          <Fact label="Absent" value={d.counts.ABSENT ?? 0} tone="text-red-700" />
          <Fact
            label="Slices watched"
            value={d.total_buckets ? `${d.counted_buckets ?? 0} / ${d.total_buckets}` : '—'}
            hint={`${d.attempts_ok} looks, ${d.faces_seen} faces seen`}
          />
        </div>
        {d.no_coverage && (
          <div className="mt-4 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            The cameras gave no frame during this session, so nobody could be seen. Its automatic marks say ABSENT, but
            the report leaves them out of every count and rate: a dead camera is not an empty room. A mark you set by
            hand does count.
          </div>
        )}
      </Card>

      <Card>
        <Table>
          <thead className="bg-gray-50">
            <tr>
              <th className={th}>Person</th>
              <th className={th}>Mark</th>
              <th className={th}>Seen in</th>
              <th className={th}>First seen</th>
              <th className={th}>Note</th>
              {can.manage && decided && <th className={th}>Change</th>}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100 bg-white">
            {d.people.map((p) => (
              <tr key={p.person_id} className="hover:bg-gray-50">
                <td className={td}>
                  <div className="font-medium text-gray-900">{p.display_name}</div>
                  {p.external_ref && <div className="text-xs text-gray-500">{p.external_ref}</div>}
                </td>
                <td className={td}>
                  <MarkBadge status={p.status} manual={p.source === 'manual'} />
                </td>
                <td className={`${td} whitespace-nowrap`}>
                  {p.buckets_total ? `${p.buckets_present ?? 0} of ${p.buckets_total} slices` : '—'}
                  {p.sightings > 0 && <div className="text-xs text-gray-500">{p.sightings} sightings</div>}
                </td>
                <td className={`${td} whitespace-nowrap`}>{clock(p.first_seen_at, tz)}</td>
                <td className={`${td} max-w-[16rem] truncate text-gray-500`} title={p.note ?? ''}>
                  {p.note ?? ''}
                </td>
                {can.manage && decided && (
                  <td className={td}>
                    <div className="flex items-center gap-1">
                      <select
                        className={`${inlineInputClass} py-1`}
                        value=""
                        disabled={busy === p.person_id}
                        onChange={(e) => e.target.value && mark(p, e.target.value as MarkStatus | 'AUTO')}
                        aria-label={`Change ${p.display_name}'s mark`}
                      >
                        <option value="">Set…</option>
                        {CHOICES.filter((c) => c !== 'AUTO' || p.source === 'manual').map((c) => (
                          <option key={c} value={c}>
                            {c === 'AUTO' ? 'From the cameras' : STATUS_LABEL[c]}
                          </option>
                        ))}
                      </select>
                      <Button variant="ghost" size="sm" onClick={() => setNoteFor(p)}>
                        Note
                      </Button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </Table>
        {!d.people.length && <p className="p-6 text-center text-sm text-gray-500">This group has no members.</p>}
        {can.manage && !decided && d.people.length > 0 && (
          <p className="border-t px-4 py-3 text-xs text-gray-500">Marks can be corrected once the session is final.</p>
        )}
      </Card>

      {noteFor && (
        <NoteDialog
          person={noteFor}
          onClose={() => setNoteFor(null)}
          onSave={(status, note) => {
            setNoteFor(null)
            mark(noteFor, status, note)
          }}
        />
      )}
    </>
  )
}

function Fact({ label, value, tone = 'text-gray-900', hint }: { label: string; value: ReactNode; tone?: string; hint?: string }) {
  return (
    <div className="rounded-lg bg-gray-50 p-3">
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-xl font-bold ${tone}`}>{value}</div>
      {hint && <div className="text-xs text-gray-500">{hint}</div>}
    </div>
  )
}

function NoteDialog({
  person,
  onClose,
  onSave,
}: {
  person: SessionPerson
  onClose: () => void
  onSave: (status: MarkStatus, note: string) => void
}) {
  const [status, setStatus] = useState<MarkStatus>(person.status ?? 'EXCUSED')
  const [note, setNote] = useState(person.note ?? '')
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={onClose}>
      <div className="w-full max-w-md rounded-xl bg-white p-5 shadow-xl" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
        <h2 className="mb-3 text-base font-semibold text-gray-900">{person.display_name}</h2>
        <label className="mb-3 block text-sm">
          <span className="mb-1 block font-medium text-gray-700">Mark</span>
          <select className={inputClass} value={status} onChange={(e) => setStatus(e.target.value as MarkStatus)}>
            {(['PRESENT', 'LATE', 'ABSENT', 'EXCUSED'] as MarkStatus[]).map((m) => (
              <option key={m} value={m}>
                {STATUS_LABEL[m]}
              </option>
            ))}
          </select>
        </label>
        <label className="block text-sm">
          <span className="mb-1 block font-medium text-gray-700">Note</span>
          <textarea className={inputClass} rows={3} value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="Medical leave, on duty…" />
        </label>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={() => onSave(status, note.trim())}>Save</Button>
        </div>
      </div>
    </div>
  )
}
