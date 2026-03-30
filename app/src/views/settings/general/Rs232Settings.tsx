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

export function Rs232Settings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ baud_rate: 9600, data_bits: 8, stop_bits: 1, parity: 'none' as 'none'|'even'|'odd' })

  useEffect(() => { let m = true; (async () => { setLoading(true); try { const { data } = await apiService.getGeneralRs232(); if (!m) return; setForm(data) } finally { setLoading(false) } })(); return () => { m = false } }, [])
  const onChange = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))
  const onSave = async () => { setSaving(true); try { await apiService.updateGeneralRs232({ ...form, baud_rate: Number(form.baud_rate), data_bits: Number(form.data_bits), stop_bits: Number(form.stop_bits) }) } finally { setSaving(false) } }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>
  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">RS-232</h2>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Baud Rate</span><input type="number" value={form.baud_rate} onChange={e => onChange('baud_rate', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Data Bits</span><select value={form.data_bits} onChange={e => onChange('data_bits', Number(e.target.value))} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"><option>5</option><option>6</option><option>7</option><option>8</option></select></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Stop Bits</span><select value={form.stop_bits} onChange={e => onChange('stop_bits', Number(e.target.value))} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"><option>1</option><option>2</option></select></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Parity</span><select value={form.parity} onChange={e => onChange('parity', e.target.value as any)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"><option value="none">None</option><option value="even">Even</option><option value="odd">Odd</option></select></label>
      </div>
      <div className="flex justify-end"><button disabled={saving} onClick={onSave} className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50">{saving? 'Saving…':'Save'}</button></div>
    </div>
  )
}
