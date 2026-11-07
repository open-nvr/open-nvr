// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's report: one group over a period, a person per row and a
// session per column, with totals. Excel and CSV come from core (the same
// files a partner gets with an API token); PDF is the browser's print.

import { useEffect, useState } from 'react'
import { BarChart3, ChevronDown, FileSpreadsheet, FileText, Printer } from 'lucide-react'
import { errorText } from '../api'
import { STATUS_LABEL, addDays, api, isoDate, longDate, pct, type MarkStatus } from '../attendance'
import { useFeedback } from '../components/feedback'
import { Button, Card, Empty, ErrorNote, Field, Loading, PageTitle, Table, inputClass } from '../components/ui'
import { useGroups, useLoad } from '../hooks'

const LETTER: Record<MarkStatus, string> = { PRESENT: 'P', LATE: 'L', ABSENT: 'A', EXCUSED: 'E' }
const CELL: Record<MarkStatus, string> = {
  PRESENT: 'bg-green-50 text-green-800',
  LATE: 'bg-amber-50 text-amber-800',
  ABSENT: 'bg-red-50 text-red-800',
  EXCUSED: 'bg-blue-50 text-blue-800',
}

export default function Reports() {
  const { toast } = useFeedback()
  const groups = useGroups()
  const today = isoDate(new Date())
  const [groupId, setGroupId] = useState<number | null>(null)
  const [from, setFrom] = useState(addDays(today, -6))
  const [to, setTo] = useState(today)
  const [menu, setMenu] = useState(false)

  useEffect(() => {
    if (groupId === null && groups.data?.length) setGroupId(groups.data[0].id)
  }, [groups.data, groupId])

  const valid = groupId !== null && from <= to
  const report = useLoad(() => (valid ? api.report(groupId!, from, to) : Promise.resolve(null)), [groupId, from, to])

  const file = async (format: 'csv' | 'xlsx') => {
    setMenu(false)
    if (groupId === null) return
    try {
      await api.reportFile(groupId, from, to, format)
    } catch (e) {
      toast('error', errorText(e))
    }
  }

  const r = report.data

  return (
    <>
      <div className="print:hidden">
        <PageTitle
          title="Reports"
          subtitle="A group's attendance over a period."
          actions={
            <div className="relative">
              <Button variant="secondary" onClick={() => setMenu((m) => !m)} disabled={!r?.people.length}>
                Export <ChevronDown className="h-4 w-4" />
              </Button>
              {menu && (
                <div className="absolute right-0 z-20 mt-1 w-44 overflow-hidden rounded-md border bg-white shadow-lg">
                  <button className="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50" onClick={() => file('xlsx')}>
                    <FileSpreadsheet className="h-4 w-4" /> Excel
                  </button>
                  <button className="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50" onClick={() => file('csv')}>
                    <FileText className="h-4 w-4" /> CSV
                  </button>
                  <button
                    className="flex w-full items-center gap-2 px-3 py-2 text-sm hover:bg-gray-50"
                    onClick={() => {
                      setMenu(false)
                      window.setTimeout(() => window.print(), 50)
                    }}
                  >
                    <Printer className="h-4 w-4" /> PDF (print)
                  </button>
                </div>
              )}
            </div>
          }
        />
        <Card className="mb-4 grid gap-3 p-4 sm:grid-cols-4">
          <div className="sm:col-span-2">
            <Field label="Group">
              <select className={inputClass} value={groupId ?? ''} onChange={(e) => setGroupId(Number(e.target.value))}>
                {(groups.data ?? []).map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.name}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <Field label="From">
            <input type="date" className={inputClass} value={from} max={to} onChange={(e) => e.target.value && setFrom(e.target.value)} />
          </Field>
          <Field label="To" error={from > to ? 'Before the start' : undefined}>
            <input type="date" className={inputClass} value={to} onChange={(e) => e.target.value && setTo(e.target.value)} />
          </Field>
        </Card>
      </div>

      {groups.data && !groups.data.length ? (
        <Card>
          <Empty icon={<BarChart3 className="h-10 w-10" />} title="No groups yet" />
        </Card>
      ) : report.error ? (
        <ErrorNote message={report.error} onRetry={report.reload} />
      ) : !r ? (
        <Loading />
      ) : (
        <Card className="print-sheet">
          <div className="border-b px-4 py-3">
            <h2 className="text-lg font-semibold text-gray-900">
              {r.group.name}
              {r.group.code ? ` (${r.group.code})` : ''}
            </h2>
            <p className="text-sm text-gray-600">
              {longDate(r.from)} – {longDate(r.to)} · {r.sessions.length} session{r.sessions.length === 1 ? '' : 's'} ·{' '}
              {r.people.length} {r.people.length === 1 ? 'person' : 'people'}
            </p>
          </div>
          {!r.sessions.length ? (
            <Empty title="No sessions in this period" />
          ) : (
            <Table>
              <thead className="bg-gray-50">
                <tr>
                  <th className="sticky left-0 bg-gray-50 px-3 py-2 text-left text-xs font-medium uppercase text-gray-500">Person</th>
                  {r.sessions.map((s) => (
                    <th key={s.id} className="px-1.5 py-2 text-center text-[11px] font-medium text-gray-500" title={s.no_coverage ? 'No camera coverage' : undefined}>
                      <div>{s.local_date.slice(5)}</div>
                      <div className="font-normal">{s.start}</div>
                      {s.no_coverage && <div className="text-amber-600">⚠</div>}
                    </th>
                  ))}
                  <th className="px-2 py-2 text-center text-xs font-medium uppercase text-gray-500">P</th>
                  <th className="px-2 py-2 text-center text-xs font-medium uppercase text-gray-500">L</th>
                  <th className="px-2 py-2 text-center text-xs font-medium uppercase text-gray-500">A</th>
                  <th className="px-2 py-2 text-center text-xs font-medium uppercase text-gray-500">E</th>
                  <th className="px-3 py-2 text-right text-xs font-medium uppercase text-gray-500">Rate</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 bg-white">
                {r.people.map((p) => (
                  <tr key={p.person_id}>
                    <td className="sticky left-0 whitespace-nowrap bg-white px-3 py-2">
                      <div className="font-medium text-gray-900">{p.display_name}</div>
                      <div className="text-xs text-gray-500">
                        {p.external_ref ?? ''}
                        {!p.member && ' · left the group'}
                      </div>
                    </td>
                    {r.sessions.map((s) => {
                      const m = p.marks[String(s.id)]
                      return (
                        <td key={s.id} className={`px-1.5 py-2 text-center text-xs font-semibold ${m ? CELL[m] : 'text-gray-300'}`} title={m ? STATUS_LABEL[m] : 'No mark'}>
                          {m ? LETTER[m] : '·'}
                        </td>
                      )
                    })}
                    <td className="px-2 py-2 text-center">{p.present}</td>
                    <td className="px-2 py-2 text-center">{p.late}</td>
                    <td className="px-2 py-2 text-center">{p.absent}</td>
                    <td className="px-2 py-2 text-center">{p.excused}</td>
                    <td className={`px-3 py-2 text-right font-semibold ${p.rate !== null && p.rate < 0.75 ? 'text-red-700' : 'text-gray-900'}`}>{pct(p.rate)}</td>
                  </tr>
                ))}
              </tbody>
            </Table>
          )}
          <p className="border-t px-4 py-2 text-xs text-gray-500">
            P present · L late · A absent · E excused · ⚠ the cameras gave no frame in that session. Rate counts late as present.
          </p>
        </Card>
      )}
    </>
  )
}
