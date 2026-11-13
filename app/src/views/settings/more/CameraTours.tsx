// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Settings › More Settings › Camera Tours: the tours a display board cycles
 * through (views/TourView.tsx runs them, views/TourEditor.tsx edits them,
 * routers/tours.py stores them). Each card runs its tour on a display, copies
 * the display link, switches it on or off, or opens it in the editor.
 */

import { useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { BellRing, Copy, CopyPlus, ExternalLink, LayoutGrid, List, Lock, Pencil, Plus, Route, Search, Trash2 } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../../../i18n'
import { apiService } from '../../../lib/apiService'
import { resolveLayout, type WindowLayoutSettings } from '../../../lib/liveLayouts'
import { buildPages, camerasOf, cycleSeconds, formatDuration, pageGroups } from '../../../lib/tourPlan'
import type { Tour } from '../../../services/tourService'
import { usePermissions } from '../../../hooks/usePermissions'
import { useConfirm } from '../../../components/ui/ConfirmDialog'
import { useSnackbar } from '../../../components/Snackbar'
import { Button, EmptyState, ErrorCard, Skeleton } from '../../../components/ui'
import { useAvailableHeight } from '../../../components/ui/DataTable'
import { Input } from '../../../components/ui/form'
import { Switch } from '../../../components/ui/Switch'
import { IconAction } from '../../../components/live/IconAction'

/** The bare display route: the tour with no app shell around it (main.tsx). */
const displayPath = (id: number) => `/display/tour/${id}`
const displayUrl = (id: number) => `${window.location.origin}${displayPath(id)}`

type TourListView = 'cards' | 'table'
const VIEW_KEY = 'opennvr.tours.view'
const readView = (): TourListView => {
  try { return localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'cards' } catch { return 'cards' }
}

export function CameraTours() {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const { showSuccess, showError } = useSnackbar()
  const { hasPermission } = usePermissions()
  const canManage = hasPermission('cameras.manage')

  const tours = useQuery({
    queryKey: ['tours'],
    queryFn: async () => ((await apiService.getTours()).data?.tours ?? []) as Tour[],
  })
  const windowSettings = useQuery({
    queryKey: ['window-settings'],
    queryFn: async () => (await apiService.getWindowSettings()).data as WindowLayoutSettings,
    staleTime: 5 * 60_000,
  })
  const resolve = (lid: string) => resolveLayout(lid, windowSettings.data)

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['tours'] })
    qc.invalidateQueries({ queryKey: ['tour'] })
  }

  const toggleEnabled = async (tour: Tour) => {
    try {
      // PATCH, not a re-save of this filtered copy: that would fail on a
      // deleted layout and drop the cameras now in the bin.
      await apiService.setTourEnabled(tour.id, { enabled: !tour.enabled, expected_revision: tour.revision })
      refresh()
    } catch (e: any) {
      showError(e?.status === 409 ? t('tours.conflict') : e?.message || t('tours.saveFailed'))
      refresh()
    }
  }

  const remove = async (tour: Tour) => {
    const ok = await confirm({
      title: t('tours.deleteTitle', { name: tour.name }),
      message: t('tours.deleteMessage'),
      confirmLabel: t('tours.delete'),
      danger: true,
    })
    if (!ok) return
    try {
      await apiService.deleteTour(tour.id)
      showSuccess(t('tours.deleted', { name: tour.name }))
      refresh()
    } catch (e: any) {
      showError(e?.message || t('tours.deleteFailed'))
    }
  }

  const copyUrl = async (tour: Tour) => {
    try {
      await navigator.clipboard.writeText(displayUrl(tour.id))
      showSuccess(t('tours.urlCopied'))
    } catch {
      showError(t('tours.urlCopyFailed', { url: displayUrl(tour.id) }))
    }
  }

  const newTour = () => navigate('/tours/new')

  const [view, setView] = useState<TourListView>(readView)
  const chooseView = (v: TourListView) => {
    setView(v)
    try { localStorage.setItem(VIEW_KEY, v) } catch { /* a remembered view is a nicety */ }
  }
  const [query, setQuery] = useState('')
  const shown = useMemo(() => {
    const all = tours.data ?? []
    const q = query.trim().toLowerCase()
    if (!q) return all
    return all.filter((x) => [x.name, x.description ?? '', ...x.pages.map((s) => s.label ?? '')].join(' ').toLowerCase().includes(q))
  }, [tours.data, query])

  // Only the list scrolls: the heading, search and view switch stay put.
  const listed = !!tours.data?.length
  const scrollerRef = useRef<HTMLDivElement>(null)
  const noFooter = useRef<HTMLDivElement>(null)
  const maxHeight = useAvailableHeight(listed, scrollerRef, noFooter, 280)

  const actions = (tour: Tour, compact: boolean) => {
    const locked = !tour.editable
    return (
      <>
        <Button size="sm" variant="outline" onClick={() => window.open(displayPath(tour.id), '_blank', 'noopener')} disabled={!tour.enabled}
          title={compact ? t('tours.run') : undefined} aria-label={compact ? t('tours.run') : undefined}>
          <ExternalLink size={12} />{!compact && <> {t('tours.run')}</>}
        </Button>
        <Button size="sm" variant="ghost" onClick={() => copyUrl(tour)} title={displayUrl(tour.id)} aria-label={compact ? t('tours.copyUrl') : undefined}>
          <Copy size={12} />{!compact && <> {t('tours.copyUrl')}</>}
        </Button>
        {canManage && (
          <span className="ml-auto flex items-center gap-1">
            <IconAction label={t('tours.edit')} onClick={() => navigate(`/tours/${tour.id}/edit`)} disabled={locked}><Pencil size={13} /></IconAction>
            <IconAction label={t('tours.duplicate')} onClick={() => navigate(`/tours/new?from=${tour.id}`)}><CopyPlus size={13} /></IconAction>
            <IconAction label={t('tours.delete')} onClick={() => remove(tour)} disabled={locked} danger><Trash2 size={13} /></IconAction>
          </span>
        )}
      </>
    )
  }

  const enabledSwitch = (tour: Tour) => canManage && (
    <Switch
      checked={tour.enabled}
      onChange={() => toggleEnabled(tour)}
      disabled={!tour.editable}
      label={tour.enabled ? t('tours.turnOff', { name: tour.name }) : t('tours.turnOn', { name: tour.name })}
    />
  )

  const pageChips = (tour: Tour) => (
    <>
      {pageGroups(tour.pages).map((g) => (
        <span key={g.first} className={clsx('text-[10px] px-1.5 py-0.5 border whitespace-nowrap', g.layoutMissing ? 'border-[var(--warn)] text-[var(--warn)]' : 'border-[var(--border)] text-[var(--text-dim)]')}
          title={g.layoutMissing ? t('tours.layoutMissingHint') : undefined}>
          {g.label || t('tours.pageN', { n: g.first + 1 })}{g.count > 1 && ` ×${g.count}`} · {g.cameras}
        </span>
      ))}
      {tour.settings?.alerts.enabled && (
        <span className="inline-flex items-center gap-1 text-[10px] px-1.5 py-0.5 border whitespace-nowrap border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] text-[var(--danger)]">
          <BellRing size={10} aria-hidden="true" /> {t('tours.alertsOnChip')}
        </span>
      )}
    </>
  )

  return (
    <div className="space-y-3" data-testid="camera-tours">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-base font-semibold flex items-center gap-2"><Route size={18} /> {t('settings.cameraTours')}</h2>
          <p className="mt-1 text-xs text-[var(--text-dim)] max-w-2xl">{t('tours.description')}</p>
        </div>
        {canManage && (
          <Button variant="primary" size="sm" onClick={newTour}>
            <Plus size={14} /> {t('tours.new')}
          </Button>
        )}
      </div>

      {listed && (
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-full sm:w-72">
            <Search size={13} aria-hidden="true" className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" />
            <Input
              type="search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t('tours.searchTours')}
              aria-label={t('tours.searchTours')}
              data-testid="tours-search"
              className="pl-7"
            />
          </div>
          <span className="text-xs text-[var(--text-dim)] tabular-nums">
            {query.trim() ? t('tours.shownOf', { shown: shown.length, total: tours.data!.length }) : t('tours.count', { count: tours.data!.length })}
          </span>
          <div role="group" aria-label={t('tours.viewAs')} className="ml-auto inline-flex border border-[var(--border)]">
            {([['cards', LayoutGrid, t('tours.viewCards')], ['table', List, t('tours.viewTable')]] as const).map(([v, Icon, label]) => (
              <button
                key={v}
                type="button"
                aria-pressed={view === v}
                title={label}
                data-testid={`tours-view-${v}`}
                onClick={() => chooseView(v)}
                className={clsx('h-7 px-2 inline-flex items-center gap-1 text-xs',
                  view === v ? 'bg-[var(--accent)] text-white' : 'text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]')}
              >
                <Icon size={13} aria-hidden="true" /> <span className="hidden sm:inline">{label}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {tours.isPending ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-36" />)}</div>
      ) : tours.isError ? (
        <ErrorCard message={(tours.error as any)?.message || t('tours.loadFailed')} onRetry={() => tours.refetch()} />
      ) : tours.data.length === 0 ? (
        <div className="border border-[var(--border)] bg-[var(--panel-2)]">
          <EmptyState
            icon={<Route size={28} />}
            title={t('tours.emptyTitle')}
            description={t('tours.emptyDescription')}
            action={canManage ? (
              <Button variant="primary" size="sm" onClick={newTour}>
                <Plus size={14} /> {t('tours.new')}
              </Button>
            ) : undefined}
          />
        </div>
      ) : (
        <div
          ref={scrollerRef}
          data-testid="tours-list"
          className="thin-scroll overflow-auto overscroll-contain"
          style={{ maxHeight }}
        >
          {shown.length === 0 ? (
            <div className="border border-[var(--border)] bg-[var(--panel-2)] px-3 py-6 text-center text-xs text-[var(--text-dim)]">{t('tours.noToursMatch')}</div>
          ) : view === 'cards' ? (
            // Cards at least 22rem wide, so the action row fits on one line.
            <div className="grid gap-3 grid-cols-[repeat(auto-fill,minmax(min(100%,22rem),1fr))]">
              {shown.map((tour) => {
                const pages = buildPages(tour.pages, resolve)
                return (
                  <div
                    key={tour.id}
                    data-testid="tour-card"
                    className={clsx('border border-[var(--border)] bg-[var(--panel-2)] p-3 flex flex-col gap-2', !tour.enabled && 'opacity-70')}
                  >
                    <div className="flex items-start gap-2">
                      <div className="min-w-0 flex-1">
                        <div className="text-sm font-semibold truncate">{tour.name}</div>
                        <div className="text-[11px] text-[var(--text-dim)] font-mono tabular-nums">
                          {t('tours.summary', { pages: tour.pages.length, cycle: formatDuration(cycleSeconds(pages)) })}
                        </div>
                      </div>
                      {enabledSwitch(tour)}
                    </div>
                    <div className="flex flex-wrap gap-1">{pageChips(tour)}</div>
                    {!tour.editable && (
                      <div className="flex items-center gap-1 text-[11px] text-[var(--text-dim)]"><Lock size={11} /> {t('tours.readOnly')}</div>
                    )}
                    <div className="mt-auto flex flex-wrap items-center gap-1 pt-1">{actions(tour, false)}</div>
                  </div>
                )
              })}
            </div>
          ) : (
            <table className="w-full text-sm border-separate border-spacing-0 border border-[var(--border)]" data-testid="tours-table">
              {/* Sticky inside the scroller: the rows move under it. Separate borders and a
                  background on each cell: with collapsed borders Chrome lets rows show through. */}
              <thead className="sticky top-0 z-10 text-[var(--text-dim)] text-left text-xs [&_th]:bg-[var(--bg-2)] [&_th]:border-b [&_th]:border-[var(--border)]">
                <tr>
                  <th scope="col" className="px-3 py-2 font-medium">{t('tours.colName')}</th>
                  <th scope="col" className="px-3 py-2 font-medium">{t('tours.colPages')}</th>
                  <th scope="col" className="px-3 py-2 font-medium text-right">{t('tours.colCameras')}</th>
                  <th scope="col" className="px-3 py-2 font-medium text-right">{t('tours.colCycle')}</th>
                  {canManage && <th scope="col" className="px-3 py-2 font-medium">{t('tours.colOn')}</th>}
                  <th scope="col" className="px-3 py-2 font-medium"><span className="sr-only">{t('tours.colActions')}</span></th>
                </tr>
              </thead>
              <tbody className="bg-[var(--panel-2)] [&>tr+tr>td]:border-t [&>tr+tr>td]:border-[var(--border)]">
                {shown.map((tour) => {
                  const pages = buildPages(tour.pages, resolve)
                  const cameras = new Set(tour.pages.flatMap((s) => camerasOf(s.camera_ids))).size
                  return (
                    <tr key={tour.id} data-testid="tour-row" className={clsx('hover:bg-[var(--panel)]', !tour.enabled && 'opacity-70')}>
                      <td className="px-3 py-1.5 max-w-[16rem]">
                        <div className="flex items-center gap-1 font-medium truncate" title={tour.description || tour.name}>
                          {!tour.editable && <Lock size={11} className="shrink-0 text-[var(--text-dim)]" aria-label={t('tours.readOnly')} />}
                          <span className="truncate">{tour.name}</span>
                        </div>
                      </td>
                      <td className="px-3 py-1.5"><div className="flex flex-wrap gap-1">{pageChips(tour)}</div></td>
                      <td className="px-3 py-1.5 text-right font-mono tabular-nums text-xs">{cameras}</td>
                      <td className="px-3 py-1.5 text-right font-mono tabular-nums text-xs whitespace-nowrap">{formatDuration(cycleSeconds(pages))}</td>
                      {canManage && <td className="px-3 py-1.5">{enabledSwitch(tour)}</td>}
                      <td className="px-3 py-1.5"><div className="flex items-center gap-1 justify-end whitespace-nowrap">{actions(tour, true)}</div></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  )
}
