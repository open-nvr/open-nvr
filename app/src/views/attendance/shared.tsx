/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Pieces every attendance tab uses: the groups and cameras (cached once),
// how a mark looks, and who may change what.

import { useQuery } from '@tanstack/react-query'
import { Badge, type BadgeVariant } from '../../components/ui'
import { usePermissions } from '../../hooks/usePermissions'
import type { DateFormatters } from '../../i18n'
import { apiService } from '../../lib/apiService'
import { attendanceService, STATUS_LABEL, type Group, type MarkStatus } from '../../services/attendanceService'

export type CameraRow = { id: number; name: string; location?: string | null }

export function useGroups() {
  return useQuery({
    queryKey: ['attendance', 'groups'],
    queryFn: attendanceService.listGroups,
    retry: 0,
  })
}

export function useCameras() {
  return useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as CameraRow[]
    },
    retry: 0,
  })
}

export function groupName(groups: Group[] | undefined, id: number): string {
  return groups?.find((g) => g.id === id)?.name ?? `Group ${id}`
}

export function cameraNames(cameras: CameraRow[] | undefined, ids: number[]): string {
  if (!ids.length) return 'No cameras'
  return ids.map((id) => cameras?.find((c) => c.id === id)?.name ?? `Camera ${id}`).join(', ')
}

// A mark is STATUS, so it wears the status palette, always labelled.
const MARK_BADGE: Record<MarkStatus, BadgeVariant> = {
  PRESENT: 'success',
  LATE: 'warning',
  ABSENT: 'destructive',
  EXCUSED: 'info',
}

export function MarkBadge({ status, manual }: { status: MarkStatus | null; manual?: boolean }) {
  if (!status) return <span className="text-[var(--text-dim)]">—</span>
  return (
    <Badge variant={MARK_BADGE[status]} title={manual ? 'Set by a person' : 'From the cameras'}>
      {STATUS_LABEL[status]}{manual ? ' ✎' : ''}
    </Badge>
  )
}

/** Local calendar date (YYYY-MM-DD) of a Date, in the browser's zone. */
export function isoDate(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + days)
  return isoDate(d)
}

/** "09:00" from an ISO instant, in the group's timezone and the
 *  operator's language: the caller passes the component's formatters. */
export function clock(fmt: DateFormatters, isoInstant: string, timeZone?: string): string {
  try {
    return fmt.time(isoInstant, { hour: '2-digit', minute: '2-digit', hour12: false, timeZone })
  } catch {
    return isoInstant.slice(11, 16)   // an unknown timeZone throws
  }
}

export function pct(rate: number | null | undefined): string {
  return rate === null || rate === undefined ? '—' : `${Math.round(rate * 100)}%`
}

export function useAttendancePermissions() {
  const { hasPermission } = usePermissions()
  return {
    canManage: hasPermission('attendance.manage'),
    canEnroll: hasPermission('attendance.enroll'),
  }
}

/** Save a Blob response as a download. */
export function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
