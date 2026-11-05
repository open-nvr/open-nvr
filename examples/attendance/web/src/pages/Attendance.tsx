// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's Attendance Records: the sessions of a day, each with its
// count of present, late and absent, and a way into its marks.

import { Link, useNavigate } from 'react-router-dom'
import { useState } from 'react'
import { AlertTriangle, ChevronLeft, ChevronRight, ClipboardList } from 'lucide-react'
import { addDays, api, clock, groupName, isoDate, longDate, type Session, type SessionStatus } from '../attendance'
import { Badge, Button, Card, Empty, ErrorNote, Loading, PageTitle, Table, ViewSwitch, inlineInputClass, td, th, useViewMode, type Tone } from '../components/ui'
import { useGroups, useLoad } from '../hooks'

export const SESSION_STATUS: Record<SessionStatus, { label: string; tone: Tone }> = {
  scheduled: { label: 'Scheduled', tone: 'gray' },
  running: { label: 'Running', tone: 'green' },
  finalizing: { label: 'Deciding', tone: 'amber' },
  final: { label: 'Final', tone: 'indigo' },
  cancelled: { label: 'Cancelled', tone: 'gray' },
}

export default function Attendance() {
  const groups = useGroups()
  const [day, setDay] = useState(isoDate(new Date()))
  const [groupId, setGroupId] = useState('')
  const sessions = useLoad(
    () => api.listSessions({ from: day, to: day, group_id: groupId ? Number(groupId) : undefined }),
    [day, groupId],
    30000,
  )
  const tz = (s: Session) => groups.data?.find((g) => g.id === s.group_id)?.timezone
  const [view, setView] = useViewMode('sessions', 'table')
  const navigate = useNavigate()
  const decided = (s: Session) => s.status === 'final' || s.status === 'finalizing'

  return (
    <>
      <PageTitle title="Attendance" subtitle="Each session's marks. Open one to see who was seen, and to correct a mark." />
      <Card className="mb-4 flex flex-wrap items-center gap-3 p-3">
        <div className="flex items-center gap-1">
          <Button variant="ghost" size="sm" onClick={() => setDay(addDays(day, -1))} aria-label="Previous day">
            <ChevronLeft className="h-4 w-4" />
          </Button>
          <input type="date" className={inlineInputClass} value={day} onChange={(e) => e.target.value && setDay(e.target.value)} />
          <Button variant="ghost" size="sm" onClick={() => setDay(addDays(day, 1))} aria-label="Next day">
            <ChevronRight className="h-4 w-4" />
          </Button>
          <Button variant="secondary" size="sm" onClick={() => setDay(isoDate(new Date()))}>
            Today
          </Button>
        </div>
        <select className={`${inlineInputClass} min-w-[12rem]`} value={groupId} onChange={(e) => setGroupId(e.target.value)} aria-label="Group">
          <option value="">Every group</option>
          {(groups.data ?? []).map((g) => (
            <option key={g.id} value={g.id}>
              {g.name}
            </option>
          ))}
        </select>
        <span className="text-sm text-gray-500">{longDate(day)}</span>
        <div className="ml-auto">
          <ViewSwitch value={view} onChange={setView} />
        </div>
      </Card>

      {sessions.error ? (
        <ErrorNote message={sessions.error} onRetry={sessions.reload} />
      ) : !sessions.data ? (
        <Loading />
      ) : !sessions.data.length ? (
        <Card>
          <Empty icon={<ClipboardList className="h-10 w-10" />} title="No sessions on this day">
            Sessions come from the schedule. Add periods there and they appear here on their days.
          </Empty>
        </Card>
      ) : view === 'table' ? (
        <Card>
          <Table>
            <thead>
              <tr>
                <th className={th}>Group</th>
                <th className={th}>Time</th>
                <th className={th}>Status</th>
                <th className={`${th} text-right`}>Present</th>
                <th className={`${th} text-right`}>Late</th>
                <th className={`${th} text-right`}>Absent</th>
                <th className={`${th} text-right`}>Excused</th>
                <th className={th}>Cameras</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 bg-white">
              {sessions.data.map((s) => {
                const st = SESSION_STATUS[s.status]
                const n = (k: keyof Session['counts']) => (decided(s) ? (s.counts[k] ?? 0) : '—')
                return (
                  <tr key={s.id} className="cursor-pointer hover:bg-gray-50" onClick={() => navigate(`/attendance/${s.id}`)}>
                    <td className={td}>
                      <Link to={`/attendance/${s.id}`} className="font-medium text-gray-900 hover:text-indigo-700" onClick={(e) => e.stopPropagation()}>
                        {groupName(groups.data, s.group_id)}
                      </Link>
                      {!s.slot_id && <span className="ml-2 text-xs text-gray-400">(one-off)</span>}
                    </td>
                    <td className={`${td} whitespace-nowrap`}>
                      {clock(s.start_at, tz(s))}–{clock(s.end_at, tz(s))}
                    </td>
                    <td className={td}>
                      <Badge tone={st.tone}>{st.label}</Badge>
                    </td>
                    <td className={`${td} text-right tabular-nums text-green-700`}>{n('PRESENT')}</td>
                    <td className={`${td} text-right tabular-nums text-amber-700`}>{n('LATE')}</td>
                    <td className={`${td} text-right tabular-nums text-red-700`}>{n('ABSENT')}</td>
                    <td className={`${td} text-right tabular-nums text-blue-700`}>{n('EXCUSED')}</td>
                    <td className={td}>
                      {s.no_coverage ? (
                        <span className="inline-flex items-center gap-1 text-xs text-amber-700" title="The cameras gave no frame during this session">
                          <AlertTriangle className="h-3.5 w-3.5" /> No frames
                        </span>
                      ) : (
                        <span className="text-xs text-gray-400">OK</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </Table>
        </Card>
      ) : (
        <div className="grid gap-3 md:grid-cols-2">
          {sessions.data.map((s) => {
            const st = SESSION_STATUS[s.status]
            const c = s.counts
            return (
              <Link
                key={s.id}
                to={`/attendance/${s.id}`}
                className="group rounded-xl border border-gray-200 bg-white p-4 shadow-sm transition hover:border-indigo-200 hover:shadow-md"
              >
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <div className="font-semibold text-gray-900 group-hover:text-indigo-700">{groupName(groups.data, s.group_id)}</div>
                    <div className="text-sm text-gray-600">
                      {clock(s.start_at, tz(s))}–{clock(s.end_at, tz(s))}
                      {!s.slot_id && <span className="ml-2 text-xs text-gray-400">(one-off)</span>}
                    </div>
                  </div>
                  <Badge tone={st.tone}>{st.label}</Badge>
                </div>
                {s.status === 'final' || s.status === 'finalizing' ? (
                  <div className="mt-3 flex flex-wrap gap-2 text-sm">
                    <Badge tone="green">{c.PRESENT ?? 0} present</Badge>
                    <Badge tone="amber">{c.LATE ?? 0} late</Badge>
                    <Badge tone="red">{c.ABSENT ?? 0} absent</Badge>
                    {(c.EXCUSED ?? 0) > 0 && <Badge tone="blue">{c.EXCUSED} excused</Badge>}
                  </div>
                ) : (
                  <div className="mt-3 text-sm text-gray-500">
                    {s.status === 'running' ? 'Watching the cameras now.' : s.status === 'scheduled' ? 'Not started yet.' : ''}
                  </div>
                )}
                {s.no_coverage && (
                  <div className="mt-3 flex items-center gap-1.5 text-xs text-amber-700">
                    <AlertTriangle className="h-3.5 w-3.5" /> The cameras gave no frame during this session — nobody could be seen.
                  </div>
                )}
              </Link>
            )
          })}
        </div>
      )}
    </>
  )
}
