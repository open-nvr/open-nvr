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

import { useEffect, useMemo, useState } from 'react'
import { apiService } from '../lib/apiService'
import { Modal } from '../components/Modal'

type LogItem = {
  id: number
  timestamp: string
  action: string
  entity_type?: string | null
  entity_id?: string | null
  user_id?: number | null
  username?: string | null
  details?: any
  ip?: string | null
  user_agent?: string | null
}

export function Events() {
  const [logs, setLogs] = useState<LogItem[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<LogItem | null>(null)

  // Filters
  const [action, setAction] = useState('')
  const [entityType, setEntityType] = useState('')
  const [userId, setUserId] = useState('')

  const skip = useMemo(() => (page - 1) * pageSize, [page, pageSize])

  useEffect(() => {
    let cancelled = false
    async function fetchLogs() {
      setLoading(true)
      setError(null)
      try {
        const params: Record<string, any> = { skip, limit: pageSize }
        if (action) params.action = action
        if (entityType) params.entity_type = entityType
        if (userId) params.user_id = Number(userId)
        const { data } = await apiService.getAuditLogs(params)
        if (!cancelled) {
          setLogs(data.logs || [])
          setTotal(data.total || 0)
        }
      } catch (e: any) {
        if (!cancelled) setError(e?.response?.data?.detail || e.message || 'Failed to load logs')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchLogs()
    return () => {
      cancelled = true
    }
  }, [skip, pageSize, action, entityType, userId])

  const totalPages = Math.max(1, Math.ceil(total / pageSize))
  const goto = (p: number) => setPage(Math.min(Math.max(1, p), totalPages))

  // Mapping noisy action identifiers to friendlier labels
  const friendlyAction = (raw?: string) => {
    if (!raw) return '-'
    const map: Record<string, string> = {
      'camera_config.update': 'Camera settings updated',
      'camera.create': 'Camera created',
      'camera.update': 'Camera updated',
      'camera.delete': 'Camera deleted',
      'login': 'Login',
      'logout': 'Logout',
      'user.create': 'User created',
      'user.update': 'User updated',
      'user.delete': 'User deleted',
      'camera_config.create': 'Camera settings created',
      'camera_config.delete': 'Camera settings deleted',
    }
    if (map[raw]) return map[raw]
    // Fallback: make it a bit nicer without relying on replaceAll
    return raw.split('_').join(' ').split('.').join(' • ')
  }

  return (
    <section className="space-y-4">
      <h1 className="text-lg font-semibold">Audit Logs</h1>

      {/* Filters */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-2 text-sm">
        <input
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          placeholder="Action (e.g., login, camera.update)"
          value={action}
          onChange={(e) => { setPage(1); setAction(e.target.value) }}
        />
        <input
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          placeholder="Entity type (user, camera, ...)"
          value={entityType}
          onChange={(e) => { setPage(1); setEntityType(e.target.value) }}
        />
        <input
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          placeholder="User ID"
          value={userId}
          onChange={(e) => { setPage(1); setUserId(e.target.value) }}
        />
        <select
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          value={pageSize}
          onChange={(e) => { setPage(1); setPageSize(Number(e.target.value)) }}
        >
          {[10, 25, 50, 100].map((n) => (
            <option key={n} value={n}>{n} / page</option>
          ))}
        </select>
      </div>

      <div className="overflow-auto border border-neutral-700">
        <table className="w-full text-sm table-fixed">
          <thead className="bg-[var(--panel-2)] text-left">
            <tr>
              <th className="p-2 w-[180px]">Time</th>
              <th className="p-2 w-[140px]">User</th>
              <th className="p-2 w-[220px]">Action</th>
              <th className="p-2 w-[260px]">Entity</th>
              <th className="p-2 w-[100px]">Details</th>
              <th className="p-2 w-[120px]">IP</th>
            </tr>
          </thead>
          <tbody>
            {loading && (
              <tr><td className="p-3" colSpan={6}>Loading…</td></tr>
            )}
            {!loading && logs.length === 0 && (
              <tr><td className="p-3 text-center text-[var(--text-dim)]" colSpan={6}>No logs</td></tr>
            )}
            {!loading && logs.map((log) => (
              <tr key={log.id} className="odd:bg-[var(--bg-2)] even:bg-[var(--panel)] align-top">
                <td className="p-2 whitespace-nowrap">{new Date(log.timestamp).toLocaleString()}</td>
                <td className="p-2 whitespace-nowrap" title={String(log.username || log.user_id || '-')}>{log.username || log.user_id || '-'}</td>
                <td className="p-2 truncate" title={log.action}>{friendlyAction(log.action)}</td>
                <td className="p-2 truncate" title={`${log.entity_type || '-'}${log.entity_id ? `:${log.entity_id}` : ''}`}>{log.entity_type || '-'}{log.entity_id ? `:${log.entity_id}` : ''}</td>
                <td className="p-2">
                  <button
                    className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)] text-xs"
                    onClick={() => setSelected(log)}
                  >View</button>
                </td>
                <td className="p-2">{log.ip || '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      <div className="flex items-center gap-2 text-sm mt-2">
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={page <= 1} onClick={() => goto(page - 1)}>Prev</button>
        <span>Page {page} / {totalPages} • {total} total</span>
        <button className="px-2 py-1 border border-neutral-700 bg-[var(--panel-2)]" disabled={page >= totalPages} onClick={() => goto(page + 1)}>Next</button>
      </div>

      {error && <div className="text-red-400 text-sm">{error}</div>}

      {/* Details modal */}
  <Modal open={!!selected} onClose={() => setSelected(null)} title="Audit log details" widthClassName="w-[800px]">
        {selected && (
          <div className="space-y-2 text-sm">
            <div className="grid grid-cols-2 gap-2">
              <div><span className="text-[var(--text-dim)]">Time: </span>{new Date(selected.timestamp).toLocaleString()}</div>
              <div><span className="text-[var(--text-dim)]">User: </span>{selected.username || selected.user_id || '-'}</div>
              <div><span className="text-[var(--text-dim)]">Action: </span>{friendlyAction(selected.action)}</div>
              <div><span className="text-[var(--text-dim)]">IP: </span>{selected.ip || '-'}</div>
              <div className="col-span-2"><span className="text-[var(--text-dim)]">Entity: </span>{selected.entity_type || '-'}{selected.entity_id ? `:${selected.entity_id}` : ''}</div>
            </div>
            <div className="border border-neutral-700 rounded overflow-hidden">
              <div className="flex items-center justify-between bg-[var(--panel-1)] px-3 py-2 text-[var(--text-dim)]">
                <span>Raw JSON</span>
                <button
                  className="px-2 py-0.5 border border-neutral-700 rounded hover:bg-neutral-800 text-xs"
                  onClick={() => {
                    const text = typeof selected.details === 'string' ? selected.details : JSON.stringify(selected.details, null, 2)
                    navigator.clipboard?.writeText(text)
                  }}
                >Copy</button>
              </div>
              <pre className="p-3 text-xs leading-snug overflow-auto">
                {typeof selected.details === 'string' ? selected.details : JSON.stringify(selected.details, null, 2)}
              </pre>
            </div>
          </div>
        )}
      </Modal>
    </section>
  )
}
