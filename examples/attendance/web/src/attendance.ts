// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Core's attendance API (server/routers/attendance.py), as the page uses
// it. The same shapes as OpenNVR's own Attendance page
// (app/src/services/attendanceService.ts): both read one API.

import { call, download, fetchBlob } from './api'

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
  has_photo: boolean
  group_ids: number[]
  created_at: string | null
  removed_at: string | null
}

export type Policy = {
  bucket_minutes: number
  present_ratio: number
  late_grace_minutes: number
  match_threshold: number
  attempt_interval_s: number
  finalize_grace_minutes: number
  min_coverage_ratio: number
}

/** Core's limits for each rule (server services/attendance_policy.py
 * _BOUNDS): a value it would refuse is caught in the form, under its
 * own field, instead of after Save in core's words. */
export const POLICY_LIMITS: Record<keyof Policy, { min: number; max: number; whole: boolean }> = {
  present_ratio: { min: 0.05, max: 1, whole: false },
  late_grace_minutes: { min: 0, max: 240, whole: true },
  match_threshold: { min: 0.3, max: 0.95, whole: false },
  bucket_minutes: { min: 1, max: 60, whole: true },
  attempt_interval_s: { min: 30, max: 1800, whole: true },
  finalize_grace_minutes: { min: 1, max: 120, whole: true },
  min_coverage_ratio: { min: 0, max: 1, whole: false },
}

/** What is wrong with each rule as typed. An empty field is not wrong
 * (a group's own rules leave it to the site's). */
export function policyErrors(policy: Record<string, string>): Partial<Record<keyof Policy, string>> {
  const out: Partial<Record<keyof Policy, string>> = {}
  for (const [key, lim] of Object.entries(POLICY_LIMITS) as [keyof Policy, (typeof POLICY_LIMITS)[keyof Policy]][]) {
    const raw = (policy[key] ?? '').trim()
    if (!raw) continue
    const v = Number(raw)
    if (!Number.isFinite(v)) out[key] = 'Not a number'
    else if (lim.whole && !Number.isInteger(v)) out[key] = 'A whole number'
    else if (v < lim.min || v > lim.max) out[key] = `Between ${lim.min} and ${lim.max}`
  }
  const slice = Number(policy.bucket_minutes)
  const look = Number(policy.attempt_interval_s)
  if (!out.attempt_interval_s && !out.bucket_minutes && policy.bucket_minutes?.trim() && policy.attempt_interval_s?.trim() && look * 2 > slice * 60) {
    out.attempt_interval_s = `At most ${(slice * 60) / 2} s: half the ${slice}-minute slice`
  }
  return out
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

/** ``cancelled``: the timetable dropped it. ``off``: a person called it
 * off (a holiday); not watched, not marked, not in reports. */
export type SessionStatus = 'scheduled' | 'running' | 'finalizing' | 'final' | 'cancelled' | 'off'

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

export type SessionDetail = Session & { people: SessionPerson[]; attempts_ok: number; faces_seen: number }

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

/** Somebody else already enrolled with this face. ``id`` is null when the
 * face service knows them but OpenNVR no longer does. */
export type Duplicate = {
  id: number | null
  display_name: string | null
  external_ref: string | null
  email: string | null
  similarity: number
}

export type EnrolResult = {
  person: Person
  enrolled: number
  results: { index: number; ok: boolean; error?: string; duplicate_of?: number | null; bbox?: number[] | null }[]
}

/** One entry per enrolled sample, oldest first. ``kept`` is false for a
 * sample enrolled before photos were kept: it counts, but has no image. */
export type EnrolledPhotos = {
  samples: number
  photos: { index: number; kept: boolean; enrolled_at: string | null }[]
}

/** Where the faces are in a webcam frame (pixels of the frame sent). */
export type FaceCheck = {
  /** ``landmarks``: eyes, nose, mouth corners (when the adapter gives them). */
  faces: { bbox: number[]; confidence: number | null; landmarks?: number[][] | null }[]
  too_small: number
  frame: { w?: number; h?: number }
}

export type Camera = { id: number; name: string; location?: string | null }

export const STATUS_LABEL: Record<MarkStatus, string> = {
  PRESENT: 'Present',
  LATE: 'Late',
  ABSENT: 'Absent',
  EXCUSED: 'Excused',
}

export const WEEKDAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

const A = 'attendance'

export const api = {
  // People
  listPeople: (query: { q?: string; group_id?: number; limit?: number; offset?: number } = {}) =>
    call<{ people: Person[]; total: number }>('GET', `${A}/people`, { query }),
  person: (id: number) => call<Person>('GET', `${A}/people/${id}`),
  createPerson: (body: { display_name: string; external_ref?: string | null; email?: string | null }) =>
    call<Person>('POST', `${A}/people`, { json: body }),
  updatePerson: (id: number, body: Partial<Pick<Person, 'display_name' | 'external_ref' | 'email' | 'active'>>) =>
    call<Person>('PATCH', `${A}/people/${id}`, { json: body }),
  removePerson: (id: number) => call<unknown>('DELETE', `${A}/people/${id}`),
  setConsent: (id: number, given: boolean) => call<Person>('POST', `${A}/people/${id}/consent`, { json: { given } }),
  enrol: (id: number, photos: Blob[]) => {
    const form = new FormData()
    photos.forEach((p, i) => form.append('photos', p, `photo-${i + 1}.jpg`))
    return call<EnrolResult>('POST', `${A}/people/${id}/faces`, { form })
  },
  forgetFaces: (id: number) => call<Person>('DELETE', `${A}/people/${id}/faces`),
  photo: (id: number) => fetchBlob(`${A}/people/${id}/photo`),
  /** The camera's current view, one JPEG (core checks the user may see it). */
  cameraStill: (id: number) => fetchBlob(`cameras/${id}/snapshot`),
  enrolledPhotos: (id: number) => call<EnrolledPhotos>('GET', `${A}/people/${id}/faces`),
  enrolledPhoto: (id: number, index: number) => fetchBlob(`${A}/people/${id}/faces/${index}/image`),
  forgetFace: (id: number, index: number) => call<Person>('DELETE', `${A}/people/${id}/faces/${index}`),
  setPhoto: (id: number, jpeg: Blob) => call<Person>('PUT', `${A}/people/${id}/photo`, { raw: jpeg }),
  /** Whether a photo is of somebody else already enrolled; nothing is
   * enrolled by asking. */
  checkDuplicate: (id: number, photo: Blob) => {
    const form = new FormData()
    form.append('photo', photo, 'photo.jpg')
    return call<{ duplicate: Duplicate | null }>('POST', `${A}/people/${id}/faces/duplicate`, { form })
  },
  checkFace: (frame: Blob) => {
    const form = new FormData()
    form.append('frame', frame, 'frame.jpg')
    return call<FaceCheck>('POST', `${A}/faces/check`, { form })
  },

  // Groups
  listGroups: () => call<{ groups: Group[] }>('GET', `${A}/groups`).then((r) => r.groups),
  createGroup: (body: Partial<Group> & { name: string; timezone: string }) =>
    call<Group>('POST', `${A}/groups`, { json: body }),
  updateGroup: (id: number, body: Partial<Group>) => call<Group>('PATCH', `${A}/groups/${id}`, { json: body }),
  deleteGroup: (id: number) => call<unknown>('DELETE', `${A}/groups/${id}`),
  members: (id: number) => call<{ people: Person[] }>('GET', `${A}/groups/${id}/members`).then((r) => r.people),
  setMembers: (id: number, personIds: number[]) =>
    call<unknown>('PUT', `${A}/groups/${id}/members`, { json: { person_ids: personIds } }),
  // One at a time: never a whole roster sent from a list that may be stale.
  addMembers: (id: number, personIds: number[]) =>
    call<unknown>('POST', `${A}/groups/${id}/members`, { json: { person_ids: personIds } }),
  removeMember: (id: number, personId: number) => call<unknown>('DELETE', `${A}/groups/${id}/members/${personId}`),

  // Timetable
  listSlots: (groupId?: number) =>
    call<{ slots: Slot[] }>('GET', `${A}/timetable`, { query: { group_id: groupId } }).then((r) => r.slots),
  createSlot: (body: SlotIn) => call<Slot>('POST', `${A}/timetable`, { json: body }),
  updateSlot: (id: number, body: SlotIn) => call<Slot>('PUT', `${A}/timetable/${id}`, { json: body }),
  deleteSlot: (id: number) => call<unknown>('DELETE', `${A}/timetable/${id}`),

  // Sessions and marks
  listSessions: (query: { from?: string; to?: string; group_id?: number; status?: string } = {}) =>
    call<{ sessions: Session[] }>('GET', `${A}/sessions`, { query }).then((r) => r.sessions),
  session: (id: number) => call<SessionDetail>('GET', `${A}/sessions/${id}`),
  setMark: (sessionId: number, personId: number, status: MarkStatus | 'AUTO', note?: string) =>
    call<unknown>('PATCH', `${A}/sessions/${sessionId}/marks/${personId}`, { json: { status, note } }),
  refinalize: (sessionId: number) => call<unknown>('POST', `${A}/sessions/${sessionId}/refinalize`),
  callOff: (sessionId: number, reason?: string) =>
    call<Session>('POST', `${A}/sessions/${sessionId}/off`, { json: { reason: reason || null } }),
  restore: (sessionId: number) => call<Session>('POST', `${A}/sessions/${sessionId}/restore`),
  addSession: (body: { group_id: number; local_date: string; start_time: string; end_time: string; camera_ids: number[] }) =>
    call<Session>('POST', `${A}/sessions`, { json: body }),
  erasePerson: (id: number) => call<unknown>('DELETE', `${A}/people/${id}`, { query: { erase: true } }),

  // Report
  report: (groupId: number, from: string, to: string) =>
    call<Report>('GET', `${A}/report`, { query: { group_id: groupId, from, to } }),
  reportFile: (groupId: number, from: string, to: string, format: 'csv' | 'xlsx') =>
    download(`${A}/report`, { group_id: groupId, from, to, format }, `attendance-${from}-${to}.${format}`),

  // Policy
  policy: () => call<{ policy: Policy; defaults: Policy }>('GET', `${A}/policy`),
  setPolicy: (changes: Partial<Policy>) =>
    call<{ policy: Policy }>('PUT', `${A}/policy`, { json: changes }).then((r) => r.policy),

  // From the rest of OpenNVR: the cameras (for the timetable) and who I am.
  cameras: () =>
    call<Camera[] | { cameras: Camera[] }>('GET', 'cameras').then((r) => (Array.isArray(r) ? r : r.cameras ?? [])),
  me: () => call<{ username: string; email?: string | null; first_name?: string | null; is_superuser?: boolean }>('GET', 'auth/me'),
  permissions: () => call<{ permissions: string[]; is_superuser: boolean }>('GET', 'users/me/permissions'),
}

/** Local calendar date (YYYY-MM-DD), in the browser's zone. */
export function isoDate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + days)
  return isoDate(d)
}

/** "09:00" for an instant, in the group's zone. */
export function clock(isoInstant: string | null | undefined, timeZone?: string): string {
  if (!isoInstant) return '—'
  try {
    return new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone })
      .format(new Date(isoInstant))
  } catch {
    return isoInstant.slice(11, 16)
  }
}

export function longDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })
      .format(new Date(`${iso}T12:00:00`))
  } catch {
    return iso
  }
}

export function pct(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? '—' : `${Math.round(rate * 100)}%`
}

export function cameraNames(cameras: Camera[] | undefined, ids: number[]): string {
  if (!ids.length) return 'No cameras'
  return ids.map((id) => cameras?.find((c) => c.id === id)?.name ?? `Camera ${id}`).join(', ')
}

export function groupName(groups: Group[] | undefined, id: number): string {
  return groups?.find((g) => g.id === id)?.name ?? `Group ${id}`
}
