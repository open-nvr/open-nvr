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

export function UserSettings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ session_timeout_minutes: 30, password_expiry_days: 90 })

  useEffect(() => { let m=true; (async () => { setLoading(true); try { const { data } = await apiService.getGeneralUser(); if (!m) return; setForm(data) } finally { setLoading(false) } })(); return () => { m=false } }, [])
  const onChange = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))
  const onSave = async () => { setSaving(true); try { await apiService.updateGeneralUser({ ...form, session_timeout_minutes: Number(form.session_timeout_minutes), password_expiry_days: Number(form.password_expiry_days) }) } finally { setSaving(false) } }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>
  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">User Settings</h2>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Session Timeout (min)</span><input type="number" value={form.session_timeout_minutes} onChange={e => onChange('session_timeout_minutes', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Password Expiry (days)</span><input type="number" value={form.password_expiry_days} onChange={e => onChange('password_expiry_days', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
      </div>
      <div className="flex justify-end"><button disabled={saving} onClick={onSave} className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50">{saving? 'Saving…':'Save'}</button></div>
    </div>
  )
}
