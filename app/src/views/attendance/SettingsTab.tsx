/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The site's attendance rules. A group can override any of them.

import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Button, ErrorCard, Skeleton } from '../../components/ui'
import { Field, Input } from '../../components/ui/form'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import { attendanceService, type Policy } from '../../services/attendanceService'
import { useAttendancePermissions } from './shared'

const FIELDS: { key: keyof Policy; label: string; hint: string; step: number; percent?: boolean }[] = [
  { key: 'bucket_minutes', label: 'Slice length (minutes)', step: 1,
    hint: 'A session is cut into slices; a person is counted per slice they were seen in.' },
  { key: 'attempt_interval_s', label: 'Look at each camera every (seconds)', step: 10,
    hint: 'At most half a slice, so every slice gets two chances. A crowded frame costs the face adapter seconds.' },
  { key: 'present_ratio', label: 'Present when seen in at least (%)', step: 5, percent: true,
    hint: 'Of the slices that were actually watched. A camera outage is left out, not counted as absence.' },
  { key: 'late_grace_minutes', label: 'Late after (minutes)', step: 1,
    hint: 'Present, but first seen this long after the start.' },
  { key: 'match_threshold', label: 'Face match threshold', step: 0.01,
    hint: 'Higher is stricter. 0.5 suits most cameras; raise it if strangers are matched.' },
  { key: 'finalize_grace_minutes', label: 'Decide marks after the end (minutes)', step: 1,
    hint: 'Waits for the last looks to arrive before deciding.' },
  { key: 'min_coverage_ratio', label: 'Count a session when watched for at least (%)', step: 5, percent: true,
    hint: 'Of its slices. Below this the session is shown but left out of every rate: a dead camera is not an empty room.' },
]

export function SettingsTab() {
  const qc = useQueryClient()
  const { canManage } = useAttendancePermissions()
  const { showError, showSuccess } = useSnackbar()
  const q = useQuery({ queryKey: ['attendance', 'policy'], queryFn: attendanceService.policy })
  const [draft, setDraft] = useState<Policy | null>(null)
  useEffect(() => { if (q.data && !draft) setDraft(q.data.policy) }, [q.data, draft])
  const save = useMutation({
    mutationFn: () => attendanceService.setPolicy(draft!),
    onSuccess: (p) => { setDraft(p); qc.invalidateQueries({ queryKey: ['attendance', 'policy'] }); showSuccess('Rules saved') },
    onError: (e) => showError(extractApiError(e, 'Could not save the rules')),
  })
  if (q.isLoading || !draft) return <Skeleton className="h-48" />
  if (q.isError) return <ErrorCard message={extractApiError(q.error, 'Could not load the rules')} />
  return (
    <div className="max-w-xl space-y-3">
      {FIELDS.map((f) => (
        <Field key={f.key} label={f.label} hint={f.hint}>
          <Input type="number" step={f.step} disabled={!canManage}
                 value={f.percent ? Math.round(draft[f.key] * 100) : draft[f.key]}
                 onChange={(e) => setDraft({ ...draft, [f.key]: f.percent ? Number(e.target.value) / 100 : Number(e.target.value) })} />
        </Field>
      ))}
      {canManage && (
        <div className="flex gap-2">
          <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>Save</Button>
          <Button variant="ghost" onClick={() => setDraft(q.data!.defaults)}>Back to the defaults</Button>
        </div>
      )}
    </div>
  )
}
