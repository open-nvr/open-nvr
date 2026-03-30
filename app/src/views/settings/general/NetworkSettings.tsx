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

export function NetworkSettings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [form, setForm] = useState({ dhcp_enabled: true, ipv4_address: '', ipv4_subnet_mask: '', ipv4_gateway: '', preferred_dns: '', alternate_dns: '', mtu: 1500 })

  useEffect(() => {
    let mounted = true
    ;(async () => { setLoading(true); try { const { data } = await apiService.getGeneralNetwork(); if (!mounted) return; setForm({ ...data, ipv4_address: data.ipv4_address || '', ipv4_subnet_mask: data.ipv4_subnet_mask || '', ipv4_gateway: data.ipv4_gateway || '', preferred_dns: data.preferred_dns || '', alternate_dns: data.alternate_dns || '' }) } finally { setLoading(false) } })()
    return () => { mounted = false }
  }, [])

  const onChange = (k: string, v: any) => setForm(f => ({ ...f, [k]: v }))
  const onSave = async () => { setSaving(true); try { const payload = { ...form, ipv4_address: form.ipv4_address || null, ipv4_subnet_mask: form.ipv4_subnet_mask || null, ipv4_gateway: form.ipv4_gateway || null, preferred_dns: form.preferred_dns || null, alternate_dns: form.alternate_dns || null, mtu: Number(form.mtu) }; await apiService.updateGeneralNetwork(payload) } finally { setSaving(false) } }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>
  return (
    <div className="space-y-4">
      <h2 className="text-base font-semibold">Network</h2>
      <div className="grid grid-cols-2 gap-4 text-sm">
        <label className="flex items-center gap-2"><input type="checkbox" checked={form.dhcp_enabled} onChange={e => onChange('dhcp_enabled', e.target.checked)} /><span>Enable DHCP</span></label>
        <div />
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">IPv4 Address</span><input value={form.ipv4_address} onChange={e => onChange('ipv4_address', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">IPv4 Subnet Mask</span><input value={form.ipv4_subnet_mask} onChange={e => onChange('ipv4_subnet_mask', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">IPv4 Gateway</span><input value={form.ipv4_gateway} onChange={e => onChange('ipv4_gateway', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Preferred DNS</span><input value={form.preferred_dns} onChange={e => onChange('preferred_dns', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">Alternate DNS</span><input value={form.alternate_dns} onChange={e => onChange('alternate_dns', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
        <label className="flex flex-col gap-1"><span className="text-[var(--text-dim)]">MTU (Bytes)</span><input type="number" value={form.mtu} onChange={e => onChange('mtu', e.target.value)} className="bg-[var(--panel-2)] border border-neutral-700 px-2 py-1"/></label>
      </div>
      <div className="flex justify-end"><button disabled={saving} onClick={onSave} className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50">{saving? 'Saving…':'Save'}</button></div>
    </div>
  )
}
