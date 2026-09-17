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
import { FileSearch } from 'lucide-react'
import { apiService } from '../lib/apiService'
import { Modal } from '../components/Modal'
import { Button, EmptyState, PageHeader, Table, THead, TBody, TR, TH, TD, Skeleton } from '../components/ui'
import { extractApiError } from '../lib/apiError'
import { useTranslation } from '../i18n'

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
  const { t } = useTranslation()
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
        if (!cancelled) setError(extractApiError(e, t('admin.failedLogs')))
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
      <PageHeader title={t('admin.auditLogs')} description={t('admin.auditDescription')} />

      {/* Filters */}
      <div className="grid grid-cols-1 sm:grid-cols-4 gap-2 text-sm">
        <input
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          placeholder={t('admin.actionPlaceholder')}
          value={action}
          onChange={(e) => { setPage(1); setAction(e.target.value) }}
        />
        <input
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          placeholder={t('admin.entityPlaceholder')}
          value={entityType}
          onChange={(e) => { setPage(1); setEntityType(e.target.value) }}
        />
        <input
          className="border border-neutral-700 bg-[var(--panel-2)] px-2 py-1 rounded"
          placeholder={t('admin.userId')}
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

      {loading ? (
        <div className="space-y-2">
          {Array.from({ length: 8 }).map((_, i) => (
            <Skeleton key={i} className="h-9" />
          ))}
        </div>
      ) : logs.length === 0 ? (
        <EmptyState
          icon={<FileSearch size={28} />}
          title={t('admin.noAuditEntries')}
          description={action || entityType || userId ? t('admin.tryClear') : t('admin.eventsWillAppear')}
        />
      ) : (
        <Table className="table-fixed">
          <THead>
            <TR>
              <TH className="w-[180px]">{t('admin.time')}</TH>
              <TH className="w-[140px]">{t('admin.username')}</TH>
              <TH className="w-[220px]">{t('admin.action')}</TH>
              <TH className="w-[260px]">{t('admin.entity')}</TH>
              <TH className="w-[100px]">{t('admin.details')}</TH>
              <TH className="w-[120px]">{t('admin.ip')}</TH>
            </TR>
          </THead>
          <TBody striped>
            {logs.map((log) => (
              <TR key={log.id} className="align-top">
                <TD className="whitespace-nowrap">{new Date(log.timestamp).toLocaleString()}</TD>
                <TD className="whitespace-nowrap" title={String(log.username || log.user_id || '-')}>{log.username || log.user_id || '-'}</TD>
                <TD className="truncate" title={log.action}>{friendlyAction(log.action)}</TD>
                <TD className="truncate" title={`${log.entity_type || '-'}${log.entity_id ? `:${log.entity_id}` : ''}`}>{log.entity_type || '-'}{log.entity_id ? `:${log.entity_id}` : ''}</TD>
                <TD>
                  <Button className="text-xs px-2 py-1" onClick={() => setSelected(log)}>{t('admin.view')}</Button>
                </TD>
                <TD>{log.ip || '-'}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
      )}

      {/* Pagination */}
      <div className="flex items-center gap-2 text-sm mt-2">
        <Button disabled={page <= 1} onClick={() => goto(page - 1)}>{t('admin.previous')}</Button>
        <span>Page {page} / {totalPages} • {total} {t('admin.total')}</span>
        <Button disabled={page >= totalPages} onClick={() => goto(page + 1)}>{t('admin.next')}</Button>
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
