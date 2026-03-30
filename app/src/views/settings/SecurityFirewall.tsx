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
import { apiService } from '../../lib/apiService'
import { useAuth } from '../../auth/AuthContext'

type Rule = {
  id: number
  name: string
  direction: 'inbound' | 'outbound'
  protocol: 'tcp' | 'udp' | 'any'
  port_from?: number | null
  port_to?: number | null
  sources?: string | null
  action: 'allow' | 'deny'
  enabled: boolean
  priority: number
  hit_count: number
}

export function SecurityFirewall() {
  const { user: me } = useAuth()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [rules, setRules] = useState<Rule[]>([])
  const [showCreateDialog, setShowCreateDialog] = useState(false)
  const [showEditDialog, setShowEditDialog] = useState(false)
  const [editing, setEditing] = useState<Rule | null>(null)
  const [form, setForm] = useState<Partial<Rule>>({ direction: 'inbound', protocol: 'tcp', action: 'allow', enabled: true, priority: 100 })

  const canAdmin = !!me?.is_superuser

  const load = async () => {
    try {
      setLoading(true)
      setError(null)
      const res = await apiService.listFirewallRules()
      const list = (res.data && (res.data as any).rules) ? (res.data as any).rules : []
      setRules(list)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to load rules')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { if (canAdmin) load() }, [canAdmin])

  const reset = () => setForm({ direction: 'inbound', protocol: 'tcp', action: 'allow', enabled: true, priority: 100 })

  const onCreate = async (e: React.FormEvent) => {
    e.preventDefault()
    try {
      setLoading(true)
      setError(null)
      await apiService.createFirewallRule({
        name: form.name,
        direction: form.direction,
        protocol: form.protocol,
        port_from: form.port_from || null,
        port_to: form.port_to || null,
        sources: form.sources || null,
        action: form.action,
        enabled: !!form.enabled,
        priority: Number(form.priority || 100),
      })
      setShowCreateDialog(false)
      reset()
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to create rule')
    } finally {
      setLoading(false)
    }
  }

  const onUpdate = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editing) return
    try {
      setLoading(true)
      setError(null)
      await apiService.updateFirewallRule(editing.id, {
        name: form.name,
        direction: form.direction,
        protocol: form.protocol,
        port_from: form.port_from ?? null,
        port_to: form.port_to ?? null,
        sources: form.sources ?? null,
        action: form.action,
        enabled: form.enabled,
        priority: form.priority,
      })
      setShowEditDialog(false)
      setEditing(null)
      reset()
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to update rule')
    } finally {
      setLoading(false)
    }
  }

  const onDelete = async (r: Rule) => {
    if (!confirm(`Delete rule "${r.name}"?`)) return
    try {
      setLoading(true)
      setError(null)
      await apiService.deleteFirewallRule(r.id)
      await load()
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to delete rule')
    } finally {
      setLoading(false)
    }
  }

  if (!canAdmin) return <div className="text-sm text-amber-400">Admin only.</div>

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <h2 className="text-base font-semibold">Firewall</h2>
        <button className="ml-auto px-2 py-1 bg-[var(--accent)] text-white rounded" onClick={() => { setShowCreateDialog(true); setEditing(null); reset() }}>Add Rule</button>
      </div>
      {error && <div className="text-sm text-red-400">{error}</div>}

      {/* Create Rule Dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-2xl w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Add Firewall Rule</h3>
            <form onSubmit={onCreate} className="grid grid-cols-3 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.name || ''} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Direction</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value as any })}>
                  <option value="inbound">inbound</option>
                  <option value="outbound">outbound</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Protocol</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value as any })}>
                  <option value="tcp">tcp</option>
                  <option value="udp">udp</option>
                  <option value="any">any</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Port From</span>
                <input type="number" min={1} max={65535} className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.port_from ?? ''} onChange={(e) => setForm({ ...form, port_from: e.target.value ? Number(e.target.value) : undefined })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Port To</span>
                <input type="number" min={1} max={65535} className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.port_to ?? ''} onChange={(e) => setForm({ ...form, port_to: e.target.value ? Number(e.target.value) : undefined })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Source CIDRs</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" placeholder="e.g. 0.0.0.0/0,10.0.0.0/8" value={form.sources || ''} onChange={(e) => setForm({ ...form, sources: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Action</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value as any })}>
                  <option value="allow">allow</option>
                  <option value="deny">deny</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Priority</span>
                <input type="number" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.priority ?? 100} onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })} />
              </label>
              <label className="flex items-center gap-2 mt-6">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Enabled
              </label>
              <div className="col-span-3 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowCreateDialog(false); reset() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Creating...' : 'Create Rule'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* Edit Rule Dialog */}
      {showEditDialog && editing && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="bg-[var(--panel)] border border-neutral-700 p-6 max-w-2xl w-full mx-4 rounded-lg">
            <h3 className="text-lg font-medium mb-4">Edit Rule: {editing.name}</h3>
            <form onSubmit={onUpdate} className="grid grid-cols-3 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Name</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.name || ''} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Direction</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.direction} onChange={(e) => setForm({ ...form, direction: e.target.value as any })}>
                  <option value="inbound">inbound</option>
                  <option value="outbound">outbound</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Protocol</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.protocol} onChange={(e) => setForm({ ...form, protocol: e.target.value as any })}>
                  <option value="tcp">tcp</option>
                  <option value="udp">udp</option>
                  <option value="any">any</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Port From</span>
                <input type="number" min={1} max={65535} className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.port_from ?? ''} onChange={(e) => setForm({ ...form, port_from: e.target.value ? Number(e.target.value) : undefined })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Port To</span>
                <input type="number" min={1} max={65535} className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.port_to ?? ''} onChange={(e) => setForm({ ...form, port_to: e.target.value ? Number(e.target.value) : undefined })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Source CIDRs</span>
                <input className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" placeholder="e.g. 0.0.0.0/0,10.0.0.0/8" value={form.sources || ''} onChange={(e) => setForm({ ...form, sources: e.target.value })} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Action</span>
                <select className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value as any })}>
                  <option value="allow">allow</option>
                  <option value="deny">deny</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">Priority</span>
                <input type="number" className="bg-[var(--panel-2)] border border-neutral-700 px-3 py-2 rounded" value={form.priority ?? 100} onChange={(e) => setForm({ ...form, priority: Number(e.target.value) })} />
              </label>
              <label className="flex items-center gap-2 mt-6">
                <input type="checkbox" className="accent-[var(--accent)]" checked={!!form.enabled} onChange={(e) => setForm({ ...form, enabled: e.target.checked })} /> Enabled
              </label>
              <div className="col-span-3 flex justify-end gap-2 mt-4">
                <button type="button" className="px-4 py-2 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setShowEditDialog(false); setEditing(null); reset() }}>Cancel</button>
                <button type="submit" className="px-4 py-2 bg-[var(--accent)] text-white rounded" disabled={loading}>{loading ? 'Updating...' : 'Update Rule'}</button>
              </div>
            </form>
          </div>
        </div>
      )}

      <div className="overflow-auto border border-neutral-700">
        <table className="w-full text-sm">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="p-2">Name</th>
              <th className="p-2">Dir</th>
              <th className="p-2">Proto</th>
              <th className="p-2">Port</th>
              <th className="p-2">Sources</th>
              <th className="p-2">Action</th>
              <th className="p-2">Enabled</th>
              <th className="p-2">Priority</th>
              <th className="p-2">Hits</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rules.map(r => (
              <tr key={r.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)]">
                <td className="p-2">{r.name}</td>
                <td className="p-2">{r.direction}</td>
                <td className="p-2">{r.protocol}</td>
                <td className="p-2">{r.port_from ? `${r.port_from}${r.port_to && r.port_to !== r.port_from ? `-${r.port_to}` : ''}` : 'any'}</td>
                <td className="p-2">{r.sources || 'any'}</td>
                <td className="p-2">{r.action}</td>
                <td className="p-2">{r.enabled ? 'Yes' : 'No'}</td>
                <td className="p-2">{r.priority}</td>
                <td className="p-2">{r.hit_count}</td>
                <td className="p-2 space-x-2">
                  <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => { setEditing(r); setShowCreateDialog(false); setForm(r); setShowEditDialog(true) }}>Edit</button>
                  <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)] rounded" onClick={() => onDelete(r)}>Delete</button>
                </td>
              </tr>
            ))}
            {rules.length === 0 && (
              <tr>
                <td colSpan={10} className="p-3 text-center text-[var(--text-dim)]">No rules</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  )
}


