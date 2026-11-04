/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Sessions: Today (what is running and what is next) and a dated list.

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Badge, EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { Field, Input, Select } from '../../components/ui/form'
import { extractApiError } from '../../lib/apiError'
import { attendanceService, type Session } from '../../services/attendanceService'
import { SessionDetail } from './SessionDetail'
import { useDateFormat } from '../../i18n'
import { addDays, cameraNames, clock, groupName, isoDate, useCameras, useGroups } from './shared'

const STATUS_BADGE = {
  scheduled: 'neutral', running: 'info', finalizing: 'warning', final: 'success', cancelled: 'neutral',
} as const

export function SessionsTab({ today = false }: { today?: boolean }) {
  const groups = useGroups()
  const cameras = useCameras()
  const fmt = useDateFormat()
  const now = isoDate(new Date())
  const [from, setFrom] = useState(today ? addDays(now, -1) : addDays(now, -6))
  const [to, setTo] = useState(today ? addDays(now, 1) : now)
  const [groupId, setGroupId] = useState<number | ''>('')
  const [open, setOpen] = useState<Session | null>(null)

  const q = useQuery({
    queryKey: ['attendance', 'sessions', from, to, groupId],
    queryFn: () => attendanceService.listSessions({
      from, to, ...(groupId === '' ? {} : { group_id: groupId }),
    }),
    refetchInterval: today ? 15_000 : false,
  })
  // Today: the group's own local "today" can be yesterday or tomorrow in
  // the browser's zone, so the range is ±1 day and filtered by status.
  const rows = (q.data ?? []).filter((s) =>
    !today || s.status === 'running' || s.status === 'finalizing' || s.status === 'scheduled'
    || s.local_date === now)
  const tzOf = (s: Session) => groups.data?.find((g) => g.id === s.group_id)?.timezone

  return (
    <div className="space-y-3">
      {!today && (
        <div className="flex flex-wrap items-end gap-3">
          <Field label="From"><Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
          <Field label="To"><Input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
          <Field label="Group">
            <Select value={groupId} onChange={(e) => setGroupId(e.target.value ? Number(e.target.value) : '')}>
              <option value="">All groups</option>
              {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            </Select>
          </Field>
        </div>
      )}
      {q.isLoading && <Skeleton className="h-32" />}
      {q.isError && <ErrorCard message={extractApiError(q.error, 'Could not load sessions')} onRetry={() => q.refetch()} />}
      {q.isSuccess && rows.length === 0 && (
        <EmptyState
          title={today ? 'Nothing scheduled today' : 'No sessions in this range'}
          description="Sessions come from the Timetable: each weekly period becomes a session on its day." />
      )}
      {rows.length > 0 && (
        <table className="w-full text-sm [&_th]:px-2 [&_td]:px-2">
          <thead className="text-left text-[var(--text-dim)]">
            <tr>
              <th className="py-1">Date</th><th>Time</th><th>Group</th><th>Cameras</th>
              <th>Status</th><th>Present</th><th>Late</th><th>Absent</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.id} className="cursor-pointer border-t border-[var(--border)] hover:bg-[var(--panel-2)]"
                  onClick={() => setOpen(s)}>
                <td className="py-1">{s.local_date}</td>
                <td>{clock(fmt, s.start_at, tzOf(s))}–{clock(fmt, s.end_at, tzOf(s))}</td>
                <td>{groupName(groups.data, s.group_id)}</td>
                <td className="max-w-48 truncate">{cameraNames(cameras.data, s.camera_ids)}</td>
                <td>
                  <Badge variant={STATUS_BADGE[s.status]}>{s.status}</Badge>
                  {s.no_coverage && <Badge variant="destructive" className="ml-1">not watched</Badge>}
                </td>
                <td>{s.counts.PRESENT ?? '—'}</td>
                <td>{s.counts.LATE ?? '—'}</td>
                <td>{s.counts.ABSENT ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {open && (
        <SessionDetail sessionId={open.id} groupName={groupName(groups.data, open.group_id)}
                       timeZone={tzOf(open)} onClose={() => setOpen(null)} />
      )}
    </div>
  )
}
