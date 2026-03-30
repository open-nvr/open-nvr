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
import { useNavigate } from 'react-router-dom'
import { apiService } from '../../../lib/apiService'

export function LiveViewSettings() {
  const navigate = useNavigate()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ default_layout: '2x2', show_osd: true, low_latency_mode: false })

  useEffect(() => { let m = true; (async () => { setLoading(true); try { const { data } = await apiService.getGeneralLiveView(); if (!m) return; setForm(data) } finally { setLoading(false) } })(); return () => { m = false } }, [])
  const onChange = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))
  const onSave = async () => { setSaving(true); try { await apiService.updateGeneralLiveView(form) } finally { setSaving(false) } }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>
  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Live View</h2>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-[var(--text-dim)]">Default Layout</span>
          <select value={form.default_layout} onChange={e => onChange('default_layout', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1">
            <optgroup label="Standard Grids">
              <option value="1x1">1×1</option>
              <option value="2x2">2×2</option>
              <option value="3x3">3×3</option>
              <option value="4x4">4×4</option>
            </optgroup>
            <optgroup label="Custom Divisions">
              <option value="1+5">1+5 (1 large + 5 small)</option>
              <option value="1+7">1+7 (1 large + 7 small)</option>
              <option value="2+8">2+8 (2 large + 8 small)</option>
              <option value="1+12">1+12 (1 large + 12 small)</option>
              <option value="4+9">4+9 (4 medium + 9 small)</option>
              <option value="1+1+10">1+1+10 (2 large + 10 small)</option>
            </optgroup>
          </select>
        </label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.show_osd} onChange={e => onChange('show_osd', e.target.checked)} /><span>Show OSD</span></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.low_latency_mode} onChange={e => onChange('low_latency_mode', e.target.checked)} /><span>Low Latency Mode</span></label>
      </div>
      <div className="pt-2 border-t border-neutral-700">
        <button 
          className="text-sm text-[var(--accent)] hover:underline"
          onClick={() => navigate('/settings/more-settings/window-settings')}
        >
          ⚙ Configure Window Division Layouts...
        </button>
        <p className="text-xs text-[var(--text-dim)] mt-1">
          Enable/disable layouts, create custom window divisions (1+7, 2+6, etc.)
        </p>
      </div>
      <div className="flex justify-end"><button disabled={saving} onClick={onSave} className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50">{saving? 'Saving…':'Save'}</button></div>
    </div>
  )
}
