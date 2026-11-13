// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The Cameras page's spreadsheet round trip (server: services/camera_io.py).
 *
 * Import: pick a CSV or Excel file, see what it would do row by row (new,
 * update, unchanged, duplicate, error, and why), choose whether matched
 * cameras are updated, then watch it run and download what happened. An
 * NVR/DVR row with channels becomes one camera per channel.
 *
 * Export: the cameras the operator manages, filtered as the page is, as CSV
 * or Excel. Passwords, and the logins inside RTSP URLs, stay out unless
 * asked for, which takes the operator's MFA code.
 */

import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, Download, FileSpreadsheet, Upload } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../../i18n'
import { apiService } from '../../lib/apiService'
import { extractApiError } from '../../lib/apiError'
import { saveBlob } from '../../lib/clipDownload'
import { Modal } from '../Modal'
import { Badge, Button, type BadgeVariant } from '../ui'
import { Checkbox } from '../ui/form'

type RowStatus = 'new' | 'update' | 'unchanged' | 'duplicate' | 'error'
type PreviewRow = {
  row: number
  channel: number | null
  status: RowStatus
  name: string | null
  location: string | null
  ip_address: string | null
  port: number | null
  rtsp_url: string | null
  camera_id: number | null
  changes: string[]
  errors: string[]
}
type Preview = { filename: string; summary: Record<RowStatus | 'total', number>; rows: PreviewRow[] }
type Outcome = 'created' | 'updated' | 'unchanged' | 'skipped' | 'failed'
type JobResult = { row: number; channel: number | null; name: string | null; outcome: Outcome; message: string; camera_id: number | null }
type Job = { id: string; state: 'running' | 'done'; total: number; done: number; counts: Partial<Record<Outcome, number>>; results: JobResult[] }

const STATUS_VARIANT: Record<RowStatus, BadgeVariant> = {
  new: 'success', update: 'info', unchanged: 'neutral', duplicate: 'warning', error: 'destructive',
}
const OUTCOME_VARIANT: Record<Outcome, BadgeVariant> = {
  created: 'success', updated: 'info', unchanged: 'neutral', skipped: 'warning', failed: 'destructive',
}
const STATUSES: RowStatus[] = ['new', 'update', 'unchanged', 'duplicate', 'error']

async function downloadTemplate(format: 'csv' | 'xlsx') {
  const { data } = await apiService.getCameraImportTemplate(format)
  saveBlob(data as Blob, `camera-import-template.${format}`)
}

// ── Import ────────────────────────────────────────────────────────────

export function CameraImportDialog({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const { t } = useTranslation()
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<Preview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [mode, setMode] = useState<'add' | 'add_update'>('add')
  const [probe, setProbe] = useState(false)
  const [show, setShow] = useState<RowStatus | 'all'>('all')
  const [job, setJob] = useState<Job | null>(null)
  const input = useRef<HTMLInputElement>(null)
  const [dragging, setDragging] = useState(false)

  const pick = async (f: File | undefined | null) => {
    if (!f) return
    setFile(f)
    setPreview(null)
    setError(null)
    setBusy(true)
    try {
      const { data } = await apiService.previewCameraImport(f)
      setPreview(data as Preview)
      setShow('all')
      setMode((data as Preview).summary.update > 0 ? 'add_update' : 'add')
    } catch (e) {
      setError(extractApiError(e, t('cameraIo.previewFailed')))
    } finally {
      setBusy(false)
    }
  }

  const willApply = preview ? preview.summary.new + (mode === 'add_update' ? preview.summary.update : 0) : 0

  const start = async () => {
    if (!file) return
    setBusy(true)
    setError(null)
    try {
      const { data } = await apiService.startCameraImport(file, mode, probe)
      setJob(data as Job)
    } catch (e) {
      setError(extractApiError(e, t('cameraIo.importFailed')))
    } finally {
      setBusy(false)
    }
  }

  // Poll the running job; the page's list refreshes once it is done.
  useEffect(() => {
    if (!job || job.state === 'done') return
    const id = window.setTimeout(async () => {
      try {
        const { data } = await apiService.getCameraImportJob(job.id)
        setJob(data as Job)
        if ((data as Job).state === 'done') onImported()
      } catch {
        // A blip: the next poll tries again.
        setJob({ ...job })
      }
    }, 1000)
    return () => window.clearTimeout(id)
  }, [job, onImported])

  const rows = useMemo(
    () => (preview ? (show === 'all' ? preview.rows : preview.rows.filter((r) => r.status === show)) : []),
    [preview, show],
  )

  const downloadResults = () => {
    if (!job) return
    const esc = (v: unknown) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const lines = [['row', 'channel', 'name', 'outcome', 'message', 'camera_id'].join(',')]
    for (const r of job.results) lines.push([r.row, r.channel, r.name, r.outcome, r.message, r.camera_id].map(esc).join(','))
    saveBlob(new Blob(['﻿' + lines.join('\r\n')], { type: 'text/csv' }), 'camera-import-results.csv')
  }

  const title = job ? (job.state === 'done' ? t('cameraIo.doneTitle') : t('cameraIo.runningTitle')) : t('cameraIo.importTitle')
  const footer = job ? (
    <div className="flex items-center justify-end gap-2">
      {job.state === 'done' && <Button variant="outline" onClick={downloadResults}><Download size={14} /> {t('cameraIo.downloadResults')}</Button>}
      <Button variant={job.state === 'done' ? 'primary' : 'ghost'} onClick={onClose}>
        {job.state === 'done' ? t('cameraIo.close') : t('cameraIo.closeKeepRunning')}
      </Button>
    </div>
  ) : (
    <div className="flex flex-wrap items-center justify-end gap-2">
      {preview && <Button variant="ghost" onClick={() => input.current?.click()} disabled={busy}>{t('cameraIo.otherFile')}</Button>}
      <Button variant="ghost" onClick={onClose}>{t('cameraIo.cancel')}</Button>
      {preview && (
        <Button variant="primary" onClick={start} disabled={busy || willApply === 0}>
          {busy ? t('cameraIo.starting') : t('cameraIo.importN', { count: willApply })}
        </Button>
      )}
    </div>
  )

  return (
    <Modal open title={title} onClose={onClose} closeOnBackdrop={false} footer={footer}
      widthClassName="w-[calc(100vw-2rem)] max-w-5xl" data-testid="camera-import">
      <input ref={input} type="file" accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        className="hidden" onChange={(e) => { void pick(e.target.files?.[0]); e.target.value = '' }} data-testid="camera-import-file" />
      {error && (
        <div role="alert" className="mb-3 flex items-start gap-2 border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] p-2 text-sm text-[var(--danger)]">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" /> <span>{error}</span>
        </div>
      )}

      {job ? (
        <JobView job={job} />
      ) : !preview ? (
        <div className="space-y-4">
          <button
            type="button"
            onClick={() => input.current?.click()}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); void pick(e.dataTransfer.files?.[0]) }}
            className={clsx('flex w-full flex-col items-center justify-center gap-2 border-2 border-dashed px-4 py-10 text-sm transition-colors',
              dragging ? 'border-[var(--accent)] bg-[color-mix(in_oklab,var(--accent)_8%,transparent)]' : 'border-[var(--border)] hover:border-[var(--accent)]')}
            disabled={busy}
          >
            <Upload size={22} className="text-[var(--accent)]" />
            <span className="font-medium">{busy ? t('cameraIo.reading', { name: file?.name ?? '' }) : t('cameraIo.dropHere')}</span>
            <span className="text-xs text-[var(--text-dim)]">{t('cameraIo.formats')}</span>
          </button>
          <div className="grid gap-3 text-xs text-[var(--text-dim)] sm:grid-cols-2">
            <div className="space-y-1">
              <div className="text-sm font-medium text-[var(--text)]">{t('cameraIo.oneRowTitle')}</div>
              <p>{t('cameraIo.oneRowHelp')}</p>
            </div>
            <div className="space-y-1">
              <div className="text-sm font-medium text-[var(--text)]">{t('cameraIo.nvrTitle')}</div>
              <p>{t('cameraIo.nvrHelp')}</p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            <FileSpreadsheet size={14} className="text-[var(--text-dim)]" />
            <span className="text-[var(--text-dim)]">{t('cameraIo.template')}</span>
            <Button size="sm" variant="outline" onClick={() => void downloadTemplate('xlsx')}>Excel</Button>
            <Button size="sm" variant="outline" onClick={() => void downloadTemplate('csv')}>CSV</Button>
            <span className="text-[var(--text-dim)]">{t('cameraIo.safeNote')}</span>
          </div>
        </div>
      ) : (
        <div className="space-y-3" data-testid="camera-import-preview">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium truncate max-w-[20rem]" title={preview.filename}>{preview.filename}</span>
            <span className="text-[var(--text-dim)]">{t('cameraIo.camerasInFile', { count: preview.summary.total })}</span>
            <span className="flex flex-wrap gap-1 ml-auto">
              <FilterChip active={show === 'all'} onClick={() => setShow('all')} label={t('cameraIo.all')} count={preview.summary.total} />
              {STATUSES.filter((s) => preview.summary[s] > 0).map((s) => (
                <FilterChip key={s} active={show === s} onClick={() => setShow(s)} label={t(`cameraIo.status.${s}`)} count={preview.summary[s]} variant={STATUS_VARIANT[s]} />
              ))}
            </span>
          </div>

          <div className="max-h-[calc(100vh-24rem)] min-h-40 overflow-auto border border-[var(--border)]">
            <table className="w-full table-fixed text-xs">
              <thead className="sticky top-0 z-10 bg-[var(--bg-2)] text-left text-[var(--text-dim)] shadow-[0_1px_0_var(--border)]">
                <tr>
                  <th className="w-14 px-2 py-1.5 font-medium">{t('cameraIo.row')}</th>
                  <th className="px-2 py-1.5 font-medium">{t('cameraIo.name')}</th>
                  <th className="w-32 px-2 py-1.5 font-medium">{t('camera.location')}</th>
                  <th className="w-56 px-2 py-1.5 font-medium">{t('cameraIo.stream')}</th>
                  <th className="w-24 px-2 py-1.5 font-medium">{t('cameraIo.what')}</th>
                  <th className="w-64 px-2 py-1.5 font-medium">{t('cameraIo.details')}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[var(--border)]">
                {rows.map((r, i) => (
                  <tr key={`${r.row}-${r.channel ?? 0}-${i}`} data-status={r.status}>
                    <td className="px-2 py-1.5 font-mono tabular-nums text-[var(--text-dim)]">{r.row}{r.channel != null && <span>·{r.channel}</span>}</td>
                    <td className="truncate px-2 py-1.5" title={r.name ?? ''}>{r.name || '—'}</td>
                    <td className="truncate px-2 py-1.5" title={r.location ?? ''}>{r.location || <span className="text-[var(--text-dim)]">—</span>}</td>
                    <td className="truncate px-2 py-1.5 font-mono text-[11px]" title={r.rtsp_url ?? r.ip_address ?? ''}>
                      {r.rtsp_url || (r.ip_address ? `${r.ip_address}${r.port ? `:${r.port}` : ''} · ${t('cameraIo.byOnvif')}` : '—')}
                    </td>
                    <td className="px-2 py-1.5"><Badge variant={STATUS_VARIANT[r.status]}>{t(`cameraIo.status.${r.status}`)}</Badge></td>
                    <td className="px-2 py-1.5 text-[var(--text-dim)]">
                      {r.errors.length > 0 ? <span className="text-[var(--danger)]">{r.errors.join('; ')}</span>
                        : r.changes.length > 0 ? t('cameraIo.changes', { fields: r.changes.join(', ') }) : ''}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="flex flex-col gap-2 text-sm sm:flex-row sm:items-start sm:gap-6">
            {preview.summary.update > 0 && (
              <fieldset className="space-y-1">
                <legend className="mb-1 text-xs font-medium text-[var(--text-dim)]">{t('cameraIo.matched', { count: preview.summary.update })}</legend>
                <label className="flex items-center gap-2"><input type="radio" checked={mode === 'add'} onChange={() => setMode('add')} /> {t('cameraIo.modeAdd')}</label>
                <label className="flex items-center gap-2"><input type="radio" checked={mode === 'add_update'} onChange={() => setMode('add_update')} /> {t('cameraIo.modeUpdate')}</label>
              </fieldset>
            )}
            <div className="space-y-1">
              <Checkbox label={t('cameraIo.probe')} checked={probe} onChange={(e) => setProbe(e.target.checked)} className="!items-center !text-sm" />
              <p className="text-xs text-[var(--text-dim)] max-w-md">{t('cameraIo.probeHint')}</p>
            </div>
            {(preview.summary.error + preview.summary.duplicate) > 0 && (
              <p className="text-xs text-[var(--text-dim)] sm:ml-auto sm:max-w-xs">{t('cameraIo.skipNote', { count: preview.summary.error + preview.summary.duplicate })}</p>
            )}
          </div>
        </div>
      )}
    </Modal>
  )
}

function FilterChip({ active, onClick, label, count, variant }: { active: boolean; onClick: () => void; label: string; count: number; variant?: BadgeVariant }) {
  return (
    <button type="button" onClick={onClick} aria-pressed={active}
      className={clsx('inline-flex items-center gap-1 border px-2 py-0.5 text-xs',
        active ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)]')}>
      {variant && <span className={clsx('h-1.5 w-1.5 rounded-full', {
        'bg-[var(--success,#16a34a)]': variant === 'success', 'bg-[var(--accent)]': variant === 'info',
        'bg-[var(--text-dim)]': variant === 'neutral', 'bg-[var(--warn)]': variant === 'warning', 'bg-[var(--danger)]': variant === 'destructive',
      })} aria-hidden="true" />}
      {label} <span className="font-mono tabular-nums">{count}</span>
    </button>
  )
}

function JobView({ job }: { job: Job }) {
  const { t } = useTranslation()
  const pct = job.total ? Math.round((job.done / job.total) * 100) : 100
  const problems = job.results.filter((r) => r.outcome === 'failed' || r.outcome === 'skipped')
  return (
    <div className="space-y-3" data-testid="camera-import-job">
      <div className="flex items-center gap-3 text-sm">
        {job.state === 'done' ? <CheckCircle2 size={18} className="text-[var(--accent)]" /> : <span className="h-4 w-4 animate-spin rounded-full border-2 border-[var(--border)] border-t-[var(--accent)]" aria-hidden="true" />}
        <span>{t('cameraIo.progress', { done: job.done, total: job.total })}</span>
        <span className="ml-auto flex flex-wrap gap-1">
          {(['created', 'updated', 'unchanged', 'skipped', 'failed'] as Outcome[]).filter((o) => job.counts[o]).map((o) => (
            <Badge key={o} variant={OUTCOME_VARIANT[o]}>{t(`cameraIo.outcome.${o}`)} {job.counts[o]}</Badge>
          ))}
        </span>
      </div>
      <div className="h-1.5 w-full bg-[var(--bg-2)]" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
        <div className="h-full bg-[var(--accent)] transition-[width]" style={{ width: `${pct}%` }} />
      </div>
      {job.state !== 'done' && <p className="text-xs text-[var(--text-dim)]">{t('cameraIo.runningHint')}</p>}
      {job.state === 'done' && problems.length > 0 && (
        <div className="max-h-[calc(100vh-24rem)] overflow-auto border border-[var(--border)]">
          <table className="w-full table-fixed text-xs">
            <thead className="sticky top-0 bg-[var(--bg-2)] text-left text-[var(--text-dim)]">
              <tr>
                <th className="w-14 px-2 py-1.5 font-medium">{t('cameraIo.row')}</th>
                <th className="w-64 px-2 py-1.5 font-medium">{t('cameraIo.name')}</th>
                <th className="w-24 px-2 py-1.5 font-medium">{t('cameraIo.result')}</th>
                <th className="px-2 py-1.5 font-medium">{t('cameraIo.details')}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[var(--border)]">
              {problems.map((r, i) => (
                <tr key={`${r.row}-${r.channel ?? 0}-${i}`}>
                  <td className="px-2 py-1.5 font-mono tabular-nums text-[var(--text-dim)]">{r.row}{r.channel != null && <span>·{r.channel}</span>}</td>
                  <td className="truncate px-2 py-1.5">{r.name || '—'}</td>
                  <td className="px-2 py-1.5"><Badge variant={OUTCOME_VARIANT[r.outcome]}>{t(`cameraIo.outcome.${r.outcome}`)}</Badge></td>
                  <td className="px-2 py-1.5 text-[var(--text-dim)]">{r.message}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

// ── Export ────────────────────────────────────────────────────────────

export function CameraExportDialog({ filters, onClose }: {
  /** The Cameras page's filters: the export lists what the page lists. */
  filters: { q?: string; location?: string; no_location?: boolean; active_only: boolean }
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [format, setFormat] = useState<'xlsx' | 'csv'>('xlsx')
  const [withPasswords, setWithPasswords] = useState(false)
  const [mfa, setMfa] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const filtered = !!(filters.q || filters.location || filters.no_location || filters.active_only)

  const run = async () => {
    setBusy(true)
    setError(null)
    try {
      const params = { ...filters, format, include_passwords: withPasswords }
      const { data } = await apiService.exportCameras(params, withPasswords ? mfa.trim() : undefined)
      const day = new Date().toISOString().slice(0, 10).replace(/-/g, '')
      saveBlob(data as Blob, `cameras-${day}.${format}`)
      onClose()
    } catch (e) {
      setError(extractApiError(e, t('cameraIo.exportFailed')))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal open title={t('cameraIo.exportTitle')} onClose={onClose} widthClassName="w-[calc(100vw-2rem)] max-w-md" data-testid="camera-export"
      footer={(
        <div className="flex justify-end gap-2">
          <Button variant="ghost" onClick={onClose}>{t('cameraIo.cancel')}</Button>
          <Button variant="primary" onClick={run} disabled={busy || (withPasswords && !mfa.trim())}>
            <Download size={14} /> {busy ? t('cameraIo.exporting') : t('cameraIo.export')}
          </Button>
        </div>
      )}>
      <div className="space-y-4 text-sm">
        <p className="text-[var(--text-dim)]">{filtered ? t('cameraIo.exportFiltered') : t('cameraIo.exportAll')}</p>
        <fieldset className="flex gap-4">
          <legend className="mb-1 text-xs font-medium text-[var(--text-dim)]">{t('cameraIo.format')}</legend>
          <label className="flex items-center gap-2"><input type="radio" checked={format === 'xlsx'} onChange={() => setFormat('xlsx')} /> Excel (.xlsx)</label>
          <label className="flex items-center gap-2"><input type="radio" checked={format === 'csv'} onChange={() => setFormat('csv')} /> CSV</label>
        </fieldset>
        <div className="space-y-2">
          <Checkbox label={t('cameraIo.withPasswords')} checked={withPasswords} onChange={(e) => setWithPasswords(e.target.checked)} className="!items-center !text-sm" />
          <p className="text-xs text-[var(--text-dim)]">{withPasswords ? t('cameraIo.passwordsWarn') : t('cameraIo.passwordsOff')}</p>
          {withPasswords && (
            <label className="block">
              <span className="mb-1 block text-xs text-[var(--text-dim)]">{t('cameraIo.mfaCode')}</span>
              <input value={mfa} onChange={(e) => setMfa(e.target.value)} placeholder="123456" inputMode="numeric" autoComplete="one-time-code" autoFocus
                className="h-9 w-40 border border-[var(--border)] bg-[var(--bg-2)] px-2.5 text-[13px] outline-none focus:border-[var(--accent)]" />
            </label>
          )}
        </div>
        {error && <p role="alert" className="text-xs text-[var(--danger)]">{error}</p>}
      </div>
    </Modal>
  )
}
