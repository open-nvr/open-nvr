// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's dashboard: three numbers, what is running now, and a card
// per job.

import { Link } from 'react-router-dom'
import {
  BarChart3,
  BookOpen,
  CalendarDays,
  ClipboardList,
  Database,
  Radio,
  Settings as SettingsIcon,
  UserPlus,
  Users,
  type LucideIcon,
} from 'lucide-react'
import { useAuth } from '../auth'
import { api, clock, groupName, isoDate } from '../attendance'
import { useGroups, useLoad } from '../hooks'
import { Badge, Card, ErrorNote, StatTile } from '../components/ui'

function JobCard({ to, title, desc, icon: Icon }: { to: string; title: string; desc: string; icon: LucideIcon }) {
  return (
    <Link
      to={to}
      className="group block rounded-xl border border-gray-200 bg-white p-5 shadow-sm transition-all duration-200 hover:-translate-y-0.5 hover:border-indigo-200 hover:bg-gradient-to-b hover:from-white hover:to-indigo-50/40 hover:shadow-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-300"
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Icon className="h-5 w-5 text-gray-600 transition-colors group-hover:text-indigo-600" />
          <h3 className="text-base font-semibold text-gray-900">{title}</h3>
        </div>
        <span className="text-indigo-600 opacity-0 transition-all group-hover:translate-x-1 group-hover:opacity-100">→</span>
      </div>
      <p className="mt-2 text-sm text-gray-600">{desc}</p>
    </Link>
  )
}

export default function Dashboard() {
  const { me, can } = useAuth()
  const today = isoDate(new Date())
  const groups = useGroups()
  const people = useLoad(() => api.listPeople({ limit: 1 }), [])
  const enrolled = useLoad(() => api.listPeople({ limit: 500 }), [])
  const sessions = useLoad(() => api.listSessions({ from: today, to: today }), [today], 30000)

  // Counted on the first 500 (core's page size); "+" says there are more.
  const withFaces = enrolled.data?.people.filter((p) => p.face_samples > 0).length
  const more = (enrolled.data?.total ?? 0) > (enrolled.data?.people.length ?? 0) ? '+' : ''
  const running = (sessions.data ?? []).filter((s) => s.status === 'running')
  const tz = (id: number) => groups.data?.find((g) => g.id === id)?.timezone

  return (
    <>
      <div className="mb-4">
        <h1 className="text-2xl font-bold tracking-tight text-gray-900">Dashboard</h1>
        <p className="mt-1 text-gray-600">Welcome{me?.first_name ? `, ${me.first_name}` : ''} — attendance from your cameras.</p>
      </div>

      {(people.error || sessions.error) && (
        <div className="mb-4">
          <ErrorNote message={people.error ?? sessions.error ?? ''} />
        </div>
      )}

      <Card className="mb-6 p-2">
        <div className="grid gap-3 md:grid-cols-3">
          <StatTile
            tone="blue"
            icon={<Users className="h-6 w-6" />}
            value={withFaces === undefined ? '—' : `${withFaces}${more} / ${people.data?.total ?? '—'}`}
            label="People with faces enrolled"
          />
          <StatTile tone="green" icon={<BookOpen className="h-6 w-6" />} value={groups.data?.length ?? '—'} label="Groups" />
          <StatTile
            tone="purple"
            icon={<BarChart3 className="h-6 w-6" />}
            value={sessions.data ? `${running.length} / ${sessions.data.length}` : '—'}
            label="Sessions running now / today"
          />
        </div>
      </Card>

      {running.length > 0 && (
        <Card className="mb-6">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <Radio className="h-4 w-4 animate-pulse text-green-600" />
            <h2 className="text-sm font-semibold text-gray-900">Running now</h2>
          </div>
          <ul className="divide-y">
            {running.map((s) => (
              <li key={s.id}>
                <Link to={`/attendance/${s.id}`} className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 hover:bg-gray-50">
                  <span className="font-medium text-gray-900">{groupName(groups.data, s.group_id)}</span>
                  <span className="text-sm text-gray-600">
                    {clock(s.start_at, tz(s.group_id))}–{clock(s.end_at, tz(s.group_id))}
                  </span>
                  <Badge tone="green">Watching</Badge>
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {can.enroll && <JobCard to="/register" title="Register" desc="Add a person and enrol their face from photos or the webcam" icon={UserPlus} />}
        <JobCard to="/people" title="People" desc="Everyone enrolled: details, consent, face samples" icon={Database} />
        <JobCard to="/groups" title="Groups" desc="Classes, sections or shifts, and who is in each" icon={BookOpen} />
        <JobCard to="/schedule" title="Schedule" desc="The weekly timetable and the cameras that see each room" icon={CalendarDays} />
        <JobCard to="/attendance" title="Attendance" desc="Each session's marks, with a person's say over the cameras'" icon={ClipboardList} />
        <JobCard to="/reports" title="Reports" desc="A group over a period: export to Excel, CSV or PDF" icon={BarChart3} />
        {can.manage && <JobCard to="/settings" title="Settings" desc="How presence is decided: thresholds, grace, intervals" icon={SettingsIcon} />}
      </div>
    </>
  )
}
