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

export function ExceptionsSettings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ email_on_motion: false, email_on_stream_failure: true, webhook_url: '' })

  useEffect(() => { let m=true; (async () => { setLoading(true); try { const { data } = await apiService.getGeneralExceptions(); if (!m) return; setForm({ ...data, webhook_url: data.webhook_url || '' }) } finally { setLoading(false) } })(); return () => { m=false } }, [])
  const onChange = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))
  const onSave = async () => { setSaving(true); try { await apiService.updateGeneralExceptions({ ...form, webhook_url: form.webhook_url || null }) } finally { setSaving(false) } }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>
  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Exceptions</h2>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.email_on_motion} onChange={e => onChange('email_on_motion', e.target.checked)} /><span>Email on Motion</span></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.email_on_stream_failure} onChange={e => onChange('email_on_stream_failure', e.target.checked)} /><span>Email on Stream Failure</span></label>
        <label className="flex flex-col gap-1 col-span-2"><span className="text-[var(--text-dim)]">Webhook URL</span><input value={form.webhook_url} onChange={e => onChange('webhook_url', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
      </div>
      <div className="flex justify-end"><button disabled={saving} onClick={onSave} className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50">{saving? 'Saving…':'Save'}</button></div>
    </div>
  )
}
