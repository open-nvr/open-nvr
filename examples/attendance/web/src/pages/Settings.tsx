// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The site's rules for deciding a mark. A group can override any of them
// (Groups → Edit → This group's own rules).

import { useEffect, useState } from 'react'
import { Settings as SettingsIcon } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { api, type Policy } from '../attendance'
import { useFeedback } from '../components/feedback'
import { Button, Card, Empty, ErrorNote, Field, Loading, PageTitle, inputClass } from '../components/ui'
import { useLoad } from '../hooks'

const FIELDS: { key: keyof Policy; label: string; hint: string; step: number; unit?: string }[] = [
  { key: 'present_ratio', label: 'Present when seen in at least', hint: 'Share of the session’s slices, 0.05–1. 0.6 = 60%.', step: 0.05 },
  { key: 'late_grace_minutes', label: 'Late after', hint: 'Seen first later than this after the start = LATE.', step: 1, unit: 'minutes' },
  { key: 'match_threshold', label: 'Face match threshold', hint: '0.3–0.95. Higher is stricter: fewer wrong matches, more missed ones.', step: 0.05 },
  { key: 'bucket_minutes', label: 'Slice length', hint: 'A session is counted in slices of this length.', step: 1, unit: 'minutes' },
  { key: 'attempt_interval_s', label: 'Look at each camera every', hint: 'At most half a slice, so every slice gets two chances.', step: 10, unit: 'seconds' },
  { key: 'finalize_grace_minutes', label: 'Decide the marks after the end', hint: 'Lets the last looks arrive. At least 1.', step: 1, unit: 'minutes' },
]

export default function Settings() {
  const { can } = useAuth()
  const { toast } = useFeedback()
  const policy = useLoad(api.policy)
  const [form, setForm] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (policy.data) setForm(Object.fromEntries(Object.entries(policy.data.policy).map(([k, v]) => [k, String(v)])))
  }, [policy.data])

  if (!can.manage) {
    return (
      <Card>
        <Empty icon={<SettingsIcon className="h-10 w-10" />} title="Settings need the attendance.manage permission" />
      </Card>
    )
  }

  const changed = policy.data
    ? FIELDS.filter((f) => form[f.key] !== undefined && Number(form[f.key]) !== policy.data!.policy[f.key])
    : []

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      await api.setPolicy(Object.fromEntries(changed.map((f) => [f.key, Number(form[f.key])])) as Partial<Policy>)
      toast('success', 'Rules saved. They apply to sessions decided from now on.')
      policy.reload()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <PageTitle title="Settings" subtitle="How OpenNVR decides present, late and absent, for every group without its own rules." />
      {policy.error ? (
        <ErrorNote message={policy.error} onRetry={policy.reload} />
      ) : !policy.data ? (
        <Loading />
      ) : (
        <Card className="p-5">
          <div className="grid gap-5 md:grid-cols-2">
            {FIELDS.map((f) => (
              <Field key={f.key} label={f.label + (f.unit ? ` (${f.unit})` : '')} hint={`${f.hint} Default ${policy.data!.defaults[f.key]}.`}>
                <input
                  type="number"
                  step={f.step}
                  className={inputClass}
                  value={form[f.key] ?? ''}
                  onChange={(e) => setForm((s) => ({ ...s, [f.key]: e.target.value }))}
                />
              </Field>
            ))}
          </div>
          {error && (
            <div className="mt-4">
              <ErrorNote message={error} />
            </div>
          )}
          <div className="mt-6 flex justify-end gap-2 border-t pt-4">
            <Button
              variant="secondary"
              onClick={() => setForm(Object.fromEntries(Object.entries(policy.data!.defaults).map(([k, v]) => [k, String(v)])))}
            >
              Back to defaults
            </Button>
            <Button onClick={save} busy={busy} disabled={!changed.length}>
              Save {changed.length ? `(${changed.length})` : ''}
            </Button>
          </div>
        </Card>
      )}
    </>
  )
}
