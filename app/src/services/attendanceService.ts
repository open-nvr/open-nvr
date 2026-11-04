/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

import { api } from '../lib/api'

// Attendance: who was in the room, from the cameras. Core keeps the
// people, groups, timetable, sessions and marks (server/routers/
// attendance.py); the attendance app only reports what each look saw.

export type MarkStatus = 'PRESENT' | 'LATE' | 'ABSENT' | 'EXCUSED'

export type Person = {
  id: number
  uuid: string
  display_name: string
  external_ref: string | null
  email: string | null
  active: boolean
  consent_at: string | null
  face_samples: number
  group_ids: number[]
  created_at: string | null
  removed_at: string | null
}

export type Group = {
  id: number
  name: string
  code: string | null
  kind_label: string | null
  timezone: string
  policy: Partial<Policy>
  active: boolean
  members: number
  created_at: string | null
}

export type Slot = {
  id: number
  group_id: number
  weekday: number
  start_time: string
  end_time: string
  camera_ids: number[]
  valid_from: string | null
  valid_to: string | null
  active: boolean
}

export type SlotIn = Omit<Slot, 'id'>

export type Policy = {
  bucket_minutes: number
  present_ratio: number
  late_grace_minutes: number
  match_threshold: number
  attempt_interval_s: number
  finalize_grace_minutes: number
}

export type SessionStatus = 'scheduled' | 'running' | 'finalizing' | 'final' | 'cancelled'

export type Session = {
  id: number
  group_id: number
  slot_id: number | null
  local_date: string
  start_at: string
  end_at: string
  camera_ids: number[]
  status: SessionStatus
  finalized_at: string | null
  total_buckets: number | null
  counted_buckets: number | null
  no_coverage: boolean
  counts: Partial<Record<MarkStatus, number>>
}

export type SessionPerson = {
  person_id: number
  display_name: string
  external_ref: string | null
  status: MarkStatus | null
  source: 'auto' | 'manual' | null
  buckets_present: number | null
  buckets_total: number | null
  first_seen_at: string | null
  note: string | null
  sightings: number
}

export type SessionDetail = Session & {
  people: SessionPerson[]
  attempts_ok: number
  faces_seen: number
}

export type Report = {
  group: { id: number; name: string; code: string | null; timezone: string }
  from: string
  to: string
  sessions: { id: number; local_date: string; start: string; end: string; no_coverage: boolean }[]
  people: {
    person_id: number
    display_name: string
    external_ref: string | null
    email: string | null
    member: boolean
    marks: Record<string, MarkStatus | null>
    present: number
    late: number
    absent: number
    excused: number
    rate: number | null
  }[]
}

export type EnrolResult = {
  person: Person
  enrolled: number
  results: { index: number; ok: boolean; error?: string; duplicate_of?: number | null }[]
}

export const STATUS_LABEL: Record<MarkStatus, string> = {
  PRESENT: 'Present',
  LATE: 'Late',
  ABSENT: 'Absent',
  EXCUSED: 'Excused',
}

export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

const A = '/api/v1/attendance'

export const attendanceService = {
  // People
  listPeople: (params: { q?: string; group_id?: number; limit?: number; offset?: number } = {}) =>
    api.get(`${A}/people`, { params }).then((r) => r.data as { people: Person[]; total: number }),
  createPerson: (body: { display_name: string; external_ref?: string | null; email?: string | null }) =>
    api.post(`${A}/people`, body).then((r) => r.data as Person),
  updatePerson: (id: number, body: Partial<Pick<Person, 'display_name' | 'external_ref' | 'email' | 'active'>>) =>
    api.patch(`${A}/people/${id}`, body).then((r) => r.data as Person),
  removePerson: (id: number) => api.delete(`${A}/people/${id}`),
  setConsent: (id: number, given: boolean) =>
    api.post(`${A}/people/${id}/consent`, { given }).then((r) => r.data as Person),
  enrol: (id: number, photos: Blob[]) => {
    const form = new FormData()
    photos.forEach((p, i) => form.append('photos', p, `photo-${i + 1}.jpg`))
    return api.post(`${A}/people/${id}/faces`, form).then((r) => r.data as EnrolResult)
  },
  forgetFaces: (id: number) => api.delete(`${A}/people/${id}/faces`).then((r) => r.data as Person),

  // Groups
  listGroups: () => api.get(`${A}/groups`).then((r) => (r.data as { groups: Group[] }).groups),
  createGroup: (body: Partial<Group> & { name: string; timezone: string }) =>
    api.post(`${A}/groups`, body).then((r) => r.data as Group),
  updateGroup: (id: number, body: Partial<Group>) =>
    api.patch(`${A}/groups/${id}`, body).then((r) => r.data as Group),
  deleteGroup: (id: number) => api.delete(`${A}/groups/${id}`),
  members: (id: number) =>
    api.get(`${A}/groups/${id}/members`).then((r) => (r.data as { people: Person[] }).people),
  setMembers: (id: number, personIds: number[]) =>
    api.put(`${A}/groups/${id}/members`, { person_ids: personIds }),

  // Timetable
  listSlots: (groupId?: number) =>
    api.get(`${A}/timetable`, { params: groupId ? { group_id: groupId } : {} })
      .then((r) => (r.data as { slots: Slot[] }).slots),
  createSlot: (body: SlotIn) => api.post(`${A}/timetable`, body).then((r) => r.data as Slot),
  updateSlot: (id: number, body: SlotIn) => api.put(`${A}/timetable/${id}`, body).then((r) => r.data as Slot),
  deleteSlot: (id: number) => api.delete(`${A}/timetable/${id}`),

  // Sessions and marks
  listSessions: (params: { from?: string; to?: string; group_id?: number; status?: string } = {}) =>
    api.get(`${A}/sessions`, { params }).then((r) => (r.data as { sessions: Session[] }).sessions),
  session: (id: number) => api.get(`${A}/sessions/${id}`).then((r) => r.data as SessionDetail),
  setMark: (sessionId: number, personId: number, status: MarkStatus | 'AUTO', note?: string) =>
    api.patch(`${A}/sessions/${sessionId}/marks/${personId}`, { status, note }),
  refinalize: (sessionId: number) => api.post(`${A}/sessions/${sessionId}/refinalize`),

  // Report
  report: (groupId: number, from: string, to: string) =>
    api.get(`${A}/report`, { params: { group_id: groupId, from, to } }).then((r) => r.data as Report),
  reportFile: (groupId: number, from: string, to: string, format: 'csv' | 'xlsx') =>
    api.get(`${A}/report`, { params: { group_id: groupId, from, to, format }, responseType: 'blob' }),

  // Policy
  policy: () => api.get(`${A}/policy`).then((r) => r.data as { policy: Policy; defaults: Policy }),
  setPolicy: (changes: Partial<Policy>) =>
    api.put(`${A}/policy`, changes).then((r) => (r.data as { policy: Policy }).policy),
}
