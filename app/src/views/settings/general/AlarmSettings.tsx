/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 * 
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

import { useEffect, useState } from 'react'
import { apiService } from '../../../lib/apiService'

export function AlarmSettings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ motion_alarm_enabled: false, motion_sensitivity: 3, tamper_alarm_enabled: false, notify_email: '' })

  useEffect(() => { let m = true; (async () => { setLoading(true); try { const { data } = await apiService.getGeneralAlarm(); if (!m) return; setForm({ ...data, notify_email: data.notify_email || '' }) } finally { setLoading(false) } })(); return () => { m = false } }, [])
  const onChange = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))
  const onSave = async () => { setSaving(true); try { await apiService.updateGeneralAlarm({ ...form, motion_sensitivity: Number(form.motion_sensitivity), notify_email: form.notify_email || null }) } finally { setSaving(false) } }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>
  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Alarm</h2>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.motion_alarm_enabled} onChange={e => onChange('motion_alarm_enabled', e.target.checked)} /><span>Motion Alarm</span></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Motion Sensitivity</span><input type="number" value={form.motion_sensitivity} onChange={e => onChange('motion_sensitivity', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.tamper_alarm_enabled} onChange={e => onChange('tamper_alarm_enabled', e.target.checked)} /><span>Tamper Alarm</span></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Notify Email</span><input value={form.notify_email} onChange={e => onChange('notify_email', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
      </div>
      <div className="flex justify-end"><button disabled={saving} onClick={onSave} className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50">{saving? 'Saving…':'Save'}</button></div>
    </div>
  )
}
