/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The report: one group, a range of its dates, person × session, with
// each person's rate. What a teacher prints and an office downloads.

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Printer } from 'lucide-react'
import { Button, EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { Field, Input, Select } from '../../components/ui/form'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import { attendanceService, type MarkStatus, type Report } from '../../services/attendanceService'
import { ReportPrint } from './ReportPrint'
import { addDays, isoDate, pct, saveBlob, useGroups } from './shared'

const LETTER: Record<MarkStatus, string> = { PRESENT: 'P', LATE: 'L', ABSENT: 'A', EXCUSED: 'E' }

export function ReportTab() {
  const groups = useGroups()
  const { showError } = useSnackbar()
  const today = isoDate(new Date())
  const [groupId, setGroupId] = useState<number | ''>('')
  const [from, setFrom] = useState(addDays(today, -30))
  const [to, setTo] = useState(today)
  const [printing, setPrinting] = useState(false)
  const gid = groupId === '' ? groups.data?.[0]?.id : groupId

  const q = useQuery({
    queryKey: ['attendance', 'report', gid, from, to],
    queryFn: () => attendanceService.report(gid as number, from, to),
    enabled: gid !== undefined && from <= to,
  })

  async function download(format: 'csv' | 'xlsx') {
    if (gid === undefined) return
    try {
      const res = await attendanceService.reportFile(gid, from, to, format)
      const name = groups.data?.find((g) => g.id === gid)?.code
        || groups.data?.find((g) => g.id === gid)?.name || `group-${gid}`
      saveBlob(res.data as Blob, `attendance-${name}-${from}-to-${to}.${format}`)
    } catch (e) {
      showError(extractApiError(e, 'Could not download the report'))
    }
  }

  if (groups.isSuccess && groups.data.length === 0) {
    return <EmptyState title="No groups yet" description="Create a group under Groups, then give it a timetable." />
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Group">
          <Select value={gid ?? ''} onChange={(e) => setGroupId(Number(e.target.value))}>
            {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </Select>
        </Field>
        <Field label="From"><Input type="date" value={from} onChange={(e) => setFrom(e.target.value)} /></Field>
        <Field label="To"><Input type="date" value={to} onChange={(e) => setTo(e.target.value)} /></Field>
        <div className="ml-auto flex gap-2">
          <Button variant="outline" onClick={() => download('csv')} disabled={!q.data}>
            <Download size={14} /> CSV
          </Button>
          <Button variant="outline" onClick={() => download('xlsx')} disabled={!q.data}>
            <Download size={14} /> Excel
          </Button>
          <Button variant="outline" onClick={() => setPrinting(true)} disabled={!q.data}>
            <Printer size={14} /> Print
          </Button>
        </div>
      </div>
      {from > to && <p className="text-sm text-[var(--danger)]">"From" is after "To".</p>}
      {q.isLoading && <Skeleton className="h-40" />}
      {q.isError && <ErrorCard message={extractApiError(q.error, 'Could not build the report')} />}
      {q.data && <ReportTable report={q.data} />}
      {printing && q.data && <ReportPrint report={q.data} onClose={() => setPrinting(false)} />}
    </div>
  )
}

function ReportTable({ report }: { report: Report }) {
  if (report.sessions.length === 0) {
    return <EmptyState title="No finished sessions in this range" />
  }
  const cell = 'border-t border-[var(--border)] px-2 py-0.5'
  return (
    <div className="overflow-x-auto">
      <table className="text-sm">
        <thead>
          <tr className="text-left">
            <th className={cell}>Name</th>
            <th className={cell}>Ref.</th>
            {report.sessions.map((s) => (
              <th key={s.id} className={`${cell} whitespace-nowrap text-center`}
                  title={s.no_coverage ? 'Nothing was watched: these marks are not counted' : undefined}>
                {s.local_date.slice(5)}<br />{s.start}{s.no_coverage ? ' *' : ''}
              </th>
            ))}
            <th className={cell}>P</th><th className={cell}>L</th>
            <th className={cell}>A</th><th className={cell}>E</th><th className={cell}>Rate</th>
          </tr>
        </thead>
        <tbody>
          {report.people.map((p) => (
            <tr key={p.person_id}>
              <td className={`${cell} whitespace-nowrap`}>{p.display_name}{p.member ? '' : ' (left)'}</td>
              <td className={cell}>{p.external_ref ?? ''}</td>
              {report.sessions.map((s) => {
                const m = p.marks[String(s.id)]
                return <td key={s.id} className={`${cell} text-center`}>{m ? LETTER[m] : ''}</td>
              })}
              <td className={cell}>{p.present}</td><td className={cell}>{p.late}</td>
              <td className={cell}>{p.absent}</td><td className={cell}>{p.excused}</td>
              <td className={cell}>{pct(p.rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.sessions.some((s) => s.no_coverage) && (
        <p className="mt-2 text-xs">
          * Nothing was watched in this session (camera or app down); its automatic marks are
          not counted in the totals.
        </p>
      )}
    </div>
  )
}
