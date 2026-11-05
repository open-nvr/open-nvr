/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// One session: who was marked what, how much of it the cameras actually
// watched, and the corrections a person can make once it is final.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, RefreshCw, RotateCcw } from 'lucide-react'
import { Modal } from '../../components/Modal'
import { Button, ErrorCard, Skeleton } from '../../components/ui'
import { Input, Select } from '../../components/ui/form'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import {
  attendanceService, STATUS_LABEL, type MarkStatus, type SessionPerson,
} from '../../services/attendanceService'
import { useDateFormat } from '../../i18n'
import { MarkBadge, clock, useAttendancePermissions } from './shared'

const STATUSES: MarkStatus[] = ['PRESENT', 'LATE', 'ABSENT', 'EXCUSED']

export function SessionDetail({ sessionId, groupName, timeZone, onClose }: {
  sessionId: number
  groupName: string
  timeZone?: string
  onClose: () => void
}) {
  const qc = useQueryClient()
  const { showError, showSuccess } = useSnackbar()
  const { canManage } = useAttendancePermissions()
  const fmt = useDateFormat()
  const q = useQuery({
    queryKey: ['attendance', 'session', sessionId],
    queryFn: () => attendanceService.session(sessionId),
    refetchInterval: (query) =>
      query.state.data && !['final', 'cancelled', 'off'].includes(query.state.data.status)
        ? 15_000 : false,
  })
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ['attendance', 'session', sessionId] })
    qc.invalidateQueries({ queryKey: ['attendance', 'sessions'] })
  }
  const refin = useMutation({
    mutationFn: () => attendanceService.refinalize(sessionId),
    onSuccess: () => { invalidate(); showSuccess('Marks recomputed from the cameras') },
    onError: (e) => showError(extractApiError(e, 'Could not recompute the marks')),
  })
  // A holiday or a class that did not happen: not watched, not marked, in
  // no report. A decided session keeps its marks, hidden, for a restore.
  const off = useMutation({
    mutationFn: () => attendanceService.callOff(sessionId),
    onSuccess: () => { invalidate(); showSuccess('Session called off') },
    onError: (e) => showError(extractApiError(e, 'Could not call the session off')),
  })
  const restore = useMutation({
    mutationFn: () => attendanceService.restore(sessionId),
    onSuccess: () => { invalidate(); showSuccess('Session restored') },
    onError: (e) => showError(extractApiError(e, 'Could not restore the session')),
  })

  const s = q.data
  const final = s?.status === 'final'
  const watched = s && s.total_buckets
    ? `${s.counted_buckets ?? 0} of ${s.total_buckets} slices of the session were watched`
    : null

  return (
    <Modal open title={`${groupName} · ${s ? s.local_date : ''}`} onClose={onClose}
           widthClassName="max-w-4xl">
      {q.isLoading && <Skeleton className="h-48" />}
      {q.isError && <ErrorCard message={extractApiError(q.error, 'Could not load the session')} />}
      {s && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
            <span>{clock(fmt, s.start_at, timeZone)}–{clock(fmt, s.end_at, timeZone)}</span>
            <span className="capitalize">{s.status === 'off' ? 'called off' : s.status}</span>
            {watched && <span className="text-[var(--text-dim)]">{watched}</span>}
            {s.no_coverage && (
              <span className="text-[var(--danger)]">
                Too little was watched (the camera or the attendance app was down), so these
                marks are left out of every rate.
              </span>
            )}
            <span className="ml-auto flex gap-2">
              {final && canManage && (
                <Button size="sm" variant="outline" disabled={refin.isPending}
                        onClick={() => refin.mutate()}>
                  <RefreshCw size={14} /> Recompute
                </Button>
              )}
              {canManage && s.status !== 'off' && s.status !== 'cancelled' && (
                <Button size="sm" variant="outline" disabled={off.isPending}
                        onClick={() => off.mutate()}>
                  <Ban size={14} /> Call off
                </Button>
              )}
              {canManage && s.status === 'off' && (
                <Button size="sm" variant="outline" disabled={restore.isPending}
                        onClick={() => restore.mutate()}>
                  <RotateCcw size={14} /> Restore
                </Button>
              )}
            </span>
          </div>
          {!final && s.status !== 'cancelled' && s.status !== 'off' && (
            <p className="text-sm text-[var(--text-dim)]">
              Marks are decided a few minutes after the session ends. Until then this shows
              how often each person has been seen so far.
            </p>
          )}
          <table className="w-full text-sm [&_th]:px-2 [&_td]:px-2">
            <thead className="text-left text-[var(--text-dim)]">
              <tr>
                <th className="py-1">Name</th>
                <th>Reference</th>
                <th>Mark</th>
                <th>Seen in</th>
                <th>First seen</th>
                {final && canManage && <th>Correct</th>}
              </tr>
            </thead>
            <tbody>
              {s.people.map((p) => (
                <PersonRow key={p.person_id} p={p} sessionId={sessionId} timeZone={timeZone}
                           editable={final && canManage} onChanged={invalidate} />
              ))}
            </tbody>
          </table>
          {s.people.length === 0 && (
            <p className="text-sm text-[var(--text-dim)]">This group has no members.</p>
          )}
        </div>
      )}
    </Modal>
  )
}

function PersonRow({ p, sessionId, timeZone, editable, onChanged }: {
  p: SessionPerson
  sessionId: number
  timeZone?: string
  editable: boolean
  onChanged: () => void
}) {
  const { showError } = useSnackbar()
  const fmt = useDateFormat()
  const [note, setNote] = useState(p.note ?? '')
  const set = useMutation({
    mutationFn: (status: MarkStatus | 'AUTO') =>
      attendanceService.setMark(sessionId, p.person_id, status, note || undefined),
    onSuccess: onChanged,
    onError: (e) => showError(extractApiError(e, 'Could not change the mark')),
  })
  const seen = p.buckets_total
    ? `${p.buckets_present ?? 0} / ${p.buckets_total}`
    : p.sightings ? `${p.sightings} sightings` : '—'
  return (
    <tr className="border-t border-[var(--border)]">
      <td className="py-1">{p.display_name}</td>
      <td>{p.external_ref ?? ''}</td>
      <td><MarkBadge status={p.status} manual={p.source === 'manual'} /></td>
      <td>{seen}</td>
      <td>{p.first_seen_at ? clock(fmt, p.first_seen_at, timeZone) : '—'}</td>
      {editable && (
        <td className="flex gap-1 py-1">
          <Select aria-label={`Mark for ${p.display_name}`} value={p.source === 'manual' ? p.status ?? '' : ''}
                  disabled={set.isPending}
                  onChange={(e) => set.mutate((e.target.value || 'AUTO') as MarkStatus | 'AUTO')}>
            <option value="">From the cameras</option>
            {STATUSES.map((st) => <option key={st} value={st}>{STATUS_LABEL[st]}</option>)}
          </Select>
          <Input aria-label="Note" placeholder="Note" value={note} maxLength={500}
                 onChange={(e) => setNote(e.target.value)} className="w-32" />
        </td>
      )}
    </tr>
  )
}
