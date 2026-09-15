/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

import { api } from '../lib/api'

// Entry-screening compliance: every screening the guard-scan app ruled
// on, the clean ones included. That is what makes the percentage mean
// something — complete scans over ALL screenings, not a complaint count.

export type Verdict = 'compliant' | 'partial' | 'incomplete' | 'no_scan'

export type Screening = {
  id: number
  session_id: string
  camera_id: number | null
  started_at: string | null
  ended_at: string | null
  verdict: Verdict | string
  score: number
  coverage: number | null
  order_score: number | null
  steps_done: string[]
  steps_missing: string[]
  flagged: boolean
  ended_by: string | null
  duration_s: number | null
  engaged_s: number | null
  guard_key: string | null
  guard_name: string | null
  /** The alarm this screening raised, when it raised one. */
  alert_id: string | null
  // Names of the photos on this screening ('face', 'body', 'scene',
  // 'guard_face'); the bytes come from screeningImage() below.
  images: string[]
}

export type Tally = {
  label?: string
  /** Axis-sized form of `label` ("Sep", "W37", "Sun 13"); the server
   *  builds it, because only it knows what the period means. */
  short?: string
  key?: string
  camera_id?: number
  screenings: number
  flagged: number
  compliant: number
  partial: number
  incomplete: number
  no_scan: number
  // null, not 0, when nothing was screened: no one walked past the
  // camera, and "100%" over an empty period is a lie.
  compliance: number | null
  mean_score: number | null
}

export type ComplianceReport = {
  days: number
  period: 'day' | 'week' | 'month'
  tz_offset_minutes: number
  generated_at: string
  totals: Tally
  buckets: Tally[]
  guards: Tally[]
  cameras: Tally[]
  /** How often each surface was skipped, worst first. */
  missed: { step: string; count: number }[]
  /** Hour of the operator's local day; only hours that saw somebody. */
  hours: (Tally & { hour: number })[]
}

export const VERDICT_LABEL: Record<string, string> = {
  compliant: 'Complete',
  partial: 'Partial',
  incomplete: 'Incomplete',
  no_scan: 'No scan',
}

export const guardScanService = {
  getReport: (params: {
    days?: number
    period?: 'day' | 'week' | 'month'
    camera_id?: number
    tz_offset_minutes?: number
  }) => api.get('/api/v1/guardscan/report', { params }),

  listScreenings: (params?: {
    camera_id?: number
    verdict?: string
    // 'problems' is everything that was not a complete scan — the rows
    // an operator opens this page for.
    outcome?: 'problems' | 'compliant'

    guard?: string
    flagged?: boolean
    from?: string
    to?: string
    skip?: number
    limit?: number
  }) => api.get('/api/v1/guardscan/screenings', { params }),

  // Through the JWT api client (hence AuthedImage, not a bare <img>):
  // a photo of a customer is as camera-scoped as the screening it
  // belongs to.
  screeningImage: (id: number, name: string, signal?: AbortSignal) =>
    api.get(`/api/v1/guardscan/screenings/${id}/images/${encodeURIComponent(name)}`,
            { responseType: 'blob', signal }),

  exportUrl: (days: number) => `/api/v1/guardscan/export?days=${days}`,
  exportCsv: (days: number) =>
    api.get(`/api/v1/guardscan/export`, { params: { days }, responseType: 'blob' }),
}
