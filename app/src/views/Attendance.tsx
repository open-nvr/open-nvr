/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Attendance: who was in the room during each class or shift, from the
// cameras. OpenNVR keeps the people, the groups, the timetable and the
// marks; the Attendance app only looks. A slice of a session nobody could
// watch is left out of everyone's count rather than marked absent, and a
// person can correct any mark once the session is final.

import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { Tabs } from '../components/ui/Tabs'
import { apiService } from '../lib/apiService'
import { APP_VERTICALS, manifestProvides } from '../lib/appVerticals'
import { type RegisteredApp } from './AppCatalog'
import { AppPageHeader } from './apps/AppSetup'
import { GroupsTab } from './attendance/GroupsTab'
import { PeopleTab } from './attendance/PeopleTab'
import { ReportTab } from './attendance/ReportTab'
import { SessionsTab } from './attendance/SessionsTab'
import { SettingsTab } from './attendance/SettingsTab'
import { TimetableTab } from './attendance/TimetableTab'

export const ATTENDANCE_CAPABILITY = 'attendance'

const VERTICAL = APP_VERTICALS.find((v) => v.capability === ATTENDANCE_CAPABILITY)!

const TABS = [
  { key: 'today', label: 'Today' },
  { key: 'sessions', label: 'Sessions' },
  { key: 'report', label: 'Report' },
  { key: 'people', label: 'People' },
  { key: 'groups', label: 'Groups' },
  { key: 'timetable', label: 'Timetable' },
  { key: 'settings', label: 'Rules' },
] as const

type TabKey = (typeof TABS)[number]['key']

export function Attendance() {
  const [params, setParams] = useSearchParams()
  const raw = params.get('tab')
  const tab: TabKey = TABS.some((t) => t.key === raw) ? (raw as TabKey) : 'today'

  // Which app owns this page, so the header can say when it is not
  // installed or has no cameras picked.
  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      const list = Array.isArray(data) ? data : (data as any)?.apps
      return (Array.isArray(list) ? list : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = (appsQuery.data ?? []).find((a) => manifestProvides(a.manifest, VERTICAL))

  return (
    <div>
      <AppPageHeader
        app={app}
        title="Attendance"
        description="Who was in the room during each class or shift, marked from the cameras.">
        <Tabs className="px-3" tabs={TABS.map((t) => ({ key: t.key, label: t.label }))} active={tab}
              onChange={(key) => setParams((p) => { p.set('tab', key); return p }, { replace: true })} />
      </AppPageHeader>
      <div className="px-1">
        {tab === 'today' && <SessionsTab today />}
        {tab === 'sessions' && <SessionsTab />}
        {tab === 'report' && <ReportTab />}
        {tab === 'people' && <PeopleTab />}
        {tab === 'groups' && <GroupsTab />}
        {tab === 'timetable' && <TimetableTab />}
        {tab === 'settings' && <SettingsTab />}
      </div>
    </div>
  )
}
