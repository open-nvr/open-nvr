// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * The camera-tour editor, a page of its own (/tours/new, /tours/:id/edit;
 * opened from Settings › Camera Tours or from a running wall's Edit button,
 * where ?return= brings it back on save).
 *
 * Pages on the left, dragged into order; the selected page on the right:
 * its layout, how long it shows, a preview of it built from camera stills
 * (tiles dragged into order, sorted, counted by location) and a picker that
 * shows each camera's picture. A page holds what its layout holds: cameras
 * added past that, or left over when the layout is made smaller, go onto
 * new pages right after it, and the editor says so. Below the pages, the
 * tour's own settings: Fit or Fill, and the alarm takeover.
 */

import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { useBlocker, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  DndContext, DragOverlay, KeyboardSensor, PointerSensor, closestCenter, pointerWithin, useDraggable, useDroppable, useSensor, useSensors,
  type CollisionDetection, type DragEndEvent, type DragStartEvent,
} from '@dnd-kit/core'
import {
  SortableContext, arrayMove, rectSortingStrategy, sortableKeyboardCoordinates, useSortable, verticalListSortingStrategy,
} from '@dnd-kit/sortable'
import { CSS } from '@dnd-kit/utilities'
import { ArrowLeft, BellRing, ChevronDown, ChevronLeft, ChevronRight, CircleDashed, CopyPlus, GripVertical, PanelLeftClose, PanelLeftOpen, Play, Plus, Route, Rows3, Search, SlidersHorizontal, Trash2, TriangleAlert, X } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../i18n'
import { apiService } from '../lib/apiService'
import { useCameras, type CameraItem } from '../lib/queries'
import { PLAIN_GRIDS, PREDEFINED_LAYOUTS, parseGridId, resolveLayout, type LayoutDefinition, type WindowLayoutSettings } from '../lib/liveLayouts'
import { AUTO_GRID_MAX, buildPages, camerasOf, cycleSeconds, formatDuration, type Slot } from '../lib/tourPlan'
import { DEFAULT_TOUR_SETTINGS, type AlertSeverity, type AlertSource, type Tour, type TourInput, type TourSettings } from '../services/tourService'
import { useConfirm } from '../components/ui/ConfirmDialog'
import { useSnackbar } from '../components/Snackbar'
import { Button, ErrorCard, Skeleton } from '../components/ui'
import { FormSection, Input, Select } from '../components/ui/form'
import { InfoTip } from '../components/ui/InfoTip'
import { Switch } from '../components/ui/Switch'
import { CameraThumb } from '../components/live/CameraThumb'
import { LivePeek } from '../components/live/LivePeek'
import { Modal } from '../components/Modal'
import { IconAction } from '../components/live/IconAction'
import { addCamera, chunk, closeGaps, fill, firstEmpty, fit, moveTo, placeAt, placeMessage, removeAt, tidy } from '../lib/tourArrange'

export const TOURS_LIST_PATH = '/settings/more-settings/camera-tours'

const DURATION_PRESETS = [30, 60, 120, 300]
const MIN_DURATION = 10
const MAX_DURATION = 3600

/** camera_ids in tile order; null is a tile left empty. */
type DraftPage = { uid: string; label: string; layout_id: string; duration_s: number; camera_ids: Slot[] }
// `description` has no field in the editor; it rides along so a save keeps it.
type Draft = {
  id: number | null
  revision?: number
  name: string
  description?: string | null
  enabled: boolean
  settings: TourSettings
  pages: DraftPage[]
}

let uidSeq = 0
const uid = () => `p${++uidSeq}`
const newPage = (layout_id = 'grid:5x4'): DraftPage => ({ uid: uid(), label: '', layout_id, duration_s: 60, camera_ids: [] })
const blankDraft = (): Draft => ({ id: null, name: '', enabled: true, settings: DEFAULT_TOUR_SETTINGS, pages: [newPage()] })

function toInput(d: Draft): TourInput {
  return {
    name: d.name.trim(),
    description: d.description ?? null,
    enabled: d.enabled,
    settings: d.settings,
    pages: d.pages.map((s) => ({ label: s.label.trim(), layout_id: s.layout_id, duration_s: s.duration_s, camera_ids: tidy(s.camera_ids) })),
    ...(d.id != null && d.revision != null ? { expected_revision: d.revision } : {}),
  }
}

function fromTour(tour: Tour, copy = false): Draft {
  return {
    id: copy ? null : tour.id,
    revision: copy ? undefined : tour.revision,
    name: copy ? `${tour.name} (copy)` : tour.name,
    description: tour.description,
    enabled: tour.enabled,
    settings: { ...DEFAULT_TOUR_SETTINGS, ...tour.settings, alerts: { ...DEFAULT_TOUR_SETTINGS.alerts, ...tour.settings?.alerts } },
    pages: tour.pages.map((s) => ({ uid: uid(), label: s.label, layout_id: s.layout_id, duration_s: s.duration_s, camera_ids: [...s.camera_ids] })),
  }
}

export function TourEditor() {
  const { t } = useTranslation()
  const { tourId } = useParams()
  const [params] = useSearchParams()
  const editingId = tourId ? Number(tourId) : null
  const copyFrom = !editingId && params.get('from') ? Number(params.get('from')) : null
  const sourceId = editingId ?? copyFrom
  const back = params.get('return') || TOURS_LIST_PATH

  const source = useQuery({
    queryKey: ['tour', sourceId],
    enabled: sourceId != null && Number.isInteger(sourceId),
    queryFn: async () => (await apiService.getTour(sourceId!)).data as Tour,
    staleTime: 0,
  })
  const windowSettings = useQuery({
    queryKey: ['window-settings'],
    queryFn: async () => (await apiService.getWindowSettings()).data as WindowLayoutSettings,
    staleTime: 5 * 60_000,
  })
  const cams = useCameras({ active_only: false })

  if (sourceId != null && source.isPending) return <div className="p-4"><Skeleton className="h-96 w-full" /></div>
  if (sourceId != null && source.isError) {
    return <ErrorCard message={(source.error as any)?.message || t('tours.loadFailed')} onRetry={() => source.refetch()} />
  }
  const initial = source.data ? fromTour(source.data, copyFrom != null) : blankDraft()
  return (
    <EditorBody
      key={`${sourceId ?? 'new'}:${source.data?.revision ?? 0}`}
      initial={initial}
      readOnly={!!source.data && editingId != null && !source.data.editable}
      startPage={editingId != null && params.get('page') != null ? Number(params.get('page')) : null}
      cameras={cams.data?.cameras ?? []}
      windowSettings={windowSettings.data}
      back={back}
      onReload={() => source.refetch()}
    />
  )
}

/** A yes/no editor preference kept in this browser (panel folded or not). */
function usePref(key: string, initial: boolean): [boolean, (v: boolean) => void] {
  const [value, setValue] = useState(() => {
    try {
      const v = localStorage.getItem(key)
      return v == null ? initial : v === '1'
    } catch {
      return initial
    }
  })
  const set = (v: boolean) => {
    setValue(v)
    try { localStorage.setItem(key, v ? '1' : '0') } catch { /* private mode */ }
  }
  return [value, set]
}

function EditorBody({ initial, readOnly, startPage, cameras, windowSettings, back, onReload }: {
  initial: Draft
  readOnly: boolean
  /** Opened from a wall: just this page (its place in the tour), until asked for the rest. */
  startPage: number | null
  cameras: CameraItem[]
  windowSettings: WindowLayoutSettings | undefined
  back: string
  onReload: () => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const confirm = useConfirm()
  const { showSuccess } = useSnackbar()
  const [draft, setDraft] = useState<Draft>(initial)
  const [selected, setSelected] = useState((startPage != null ? initial.pages[startPage] : undefined)?.uid ?? initial.pages[0]?.uid)
  const [pageOnly, setPageOnly] = useState(startPage != null && !!initial.pages[startPage])
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<{ text: string; conflict?: boolean } | null>(null)
  const [badCameras, setBadCameras] = useState<Set<number>>(new Set())
  const [badLayouts, setBadLayouts] = useState<Set<string>>(new Set())
  const [touched, setTouched] = useState(false)

  const camById = useMemo(() => new Map(cameras.map((c) => [c.id, c])), [cameras])
  const [rail, setRail] = usePref('opennvr.tourEditor.rail', false)
  const [pagesOpen, setPagesOpen] = usePref('opennvr.tourEditor.pages', true)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const dirty = JSON.stringify(toInput(draft)) !== JSON.stringify(toInput(initial))
  const resolve = (lid: string) => resolveLayout(lid, windowSettings)
  const shown = buildPages(draft.pages, resolve)
  const page = draft.pages.find((s) => s.uid === selected) ?? draft.pages[0]
  const emptyPages = draft.pages.filter((s) => camerasOf(s.camera_ids).length === 0).length
  const canSave = !readOnly && !!draft.name.trim() && draft.pages.length > 0 && emptyPages === 0 && !saving
  const nameError = touched && !draft.name.trim()
  // Every page each camera is on. Counted against the camera list, so one this
  // user can't see never skews "x of y".
  const pagesOf = new Map<number, number[]>()
  draft.pages.forEach((s, i) => camerasOf(s.camera_ids).forEach((id) => pagesOf.set(id, [...(pagesOf.get(id) ?? []), i + 1])))
  const notOnTour = cameras.filter((c) => !pagesOf.has(c.id))
  const notOnTourText = (() => {
    const shownNames = notOnTour.slice(0, 3).map((c) => c.name).join(', ')
    const names = notOnTour.length > 3 ? t('tours.namesAndMore', { names: shownNames, count: notOnTour.length - 3 }) : shownNames
    return notOnTour.length === 1
      ? t('tours.notOnTourOne', { total: cameras.length, names })
      : t('tours.notOnTourMany', { count: notOnTour.length, total: cameras.length, names })
  })()

  // Leaving with unsaved changes asks first: in-app links, and the tab itself.
  const blocker = useBlocker(dirty && !saved && !saving)
  useEffect(() => {
    if (blocker.state !== 'blocked') return
    confirm({ title: t('tours.discardTitle'), message: t('tours.discardMessage'), confirmLabel: t('tours.discard'), danger: true })
      .then((ok) => (ok ? blocker.proceed() : blocker.reset()))
  }, [blocker, confirm, t])
  useEffect(() => {
    if (!dirty || saved) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty, saved])

  const setPage = (key: string, patch: Partial<DraftPage>) =>
    setDraft((d) => ({ ...d, pages: d.pages.map((s) => (s.uid === key ? { ...s, ...patch } : s)) }))
  const setSettings = (patch: Partial<TourSettings>) => setDraft((d) => ({ ...d, settings: { ...d.settings, ...patch } }))
  const setAlerts = (patch: Partial<TourSettings['alerts']>) =>
    setDraft((d) => ({ ...d, settings: { ...d.settings, alerts: { ...d.settings.alerts, ...patch } } }))

  // Opened on one page: the arrows move to the page before or after it.
  const step = (delta: number) => {
    const at = draft.pages.findIndex((s) => s.uid === page?.uid)
    const to = draft.pages[(at + delta + draft.pages.length) % draft.pages.length]
    if (to) setSelected(to.uid)
  }
  const addPage = () => {
    const s = newPage(draft.pages[draft.pages.length - 1]?.layout_id)
    setDraft((d) => ({ ...d, pages: [...d.pages, s] }))
    setSelected(s.uid)
  }
  const duplicatePage = (key: string) => {
    const i = draft.pages.findIndex((s) => s.uid === key)
    if (i < 0) return
    const copy = { ...draft.pages[i], uid: uid(), camera_ids: [...draft.pages[i].camera_ids] }
    setDraft((d) => { const pages = [...d.pages]; pages.splice(i + 1, 0, copy); return { ...d, pages } })
    setSelected(copy.uid)
  }
  // A page with cameras on it asks first; an empty one has nothing to lose.
  const removePage = async (key: string) => {
    const i = draft.pages.findIndex((s) => s.uid === key)
    const count = camerasOf(draft.pages[i].camera_ids).length
    if (count > 0) {
      const name = draft.pages[i].label || t('tours.pageN', { n: i + 1 })
      const ok = await confirm({
        title: t('tours.removePageTitle'),
        message: count === 1 ? t('tours.removePageOne', { name }) : t('tours.removePageMany', { name, count }),
        confirmLabel: t('tours.removePage'),
        danger: true,
      })
      if (!ok) return
    }
    setDraft((d) => ({ ...d, pages: d.pages.filter((s) => s.uid !== key) }))
    const next = draft.pages[i + 1] ?? draft.pages[i - 1]
    if (next) setSelected(next.uid)
  }
  // Cameras that did not fit on page `key` go onto new pages right after it,
  // with its label, layout and time. The page being edited stays selected.
  const spill = (key: string, cams: number[], tiles: number) => {
    if (!cams.length) return
    setDraft((d) => {
      const i = d.pages.findIndex((s) => s.uid === key)
      if (i < 0) return d
      const from = d.pages[i]
      const added = chunk(cams, tiles).map((ids) => ({ ...from, uid: uid(), camera_ids: ids }))
      const pages = [...d.pages]
      pages.splice(i + 1, 0, ...added)
      return { ...d, pages }
    })
  }

  const save = async () => {
    setTouched(true)
    if (!canSave) return
    if (notOnTour.length > 0) {
      const ok = await confirm({
        title: t('tours.saveWarnTitle'),
        message: notOnTourText,
        confirmLabel: t('tours.saveAnyway'),
        cancelLabel: t('tours.keepEditing'),
      })
      if (!ok) return
    }
    setSaving(true)
    setError(null)
    setBadCameras(new Set())
    setBadLayouts(new Set())
    try {
      if (draft.id == null) await apiService.createTour(toInput(draft))
      else await apiService.updateTour(draft.id, toInput(draft))
      setSaved(true)
      qc.invalidateQueries({ queryKey: ['tours'] })
      qc.invalidateQueries({ queryKey: ['tour'] })
      showSuccess(t('tours.saved', { name: draft.name.trim() }))
      navigate(back)
    } catch (e: any) {
      const detail = e?.data?.detail
      if (e?.status === 409) setError({ text: t('tours.conflict'), conflict: true })
      else if (e?.status === 422 && Array.isArray(detail?.camera_ids)) {
        setBadCameras(new Set(detail.camera_ids))
        setError({ text: t('tours.badCameras', { names: detail.camera_ids.map((id: number) => camById.get(id)?.name ?? `#${id}`).join(', ') }) })
      } else if (e?.status === 422 && Array.isArray(detail?.layout_ids)) {
        setBadLayouts(new Set(detail.layout_ids))
        setError({ text: t('tours.badLayouts') })
      } else setError({ text: typeof detail === 'string' ? detail : e?.message || t('tours.saveFailed') })
    } finally {
      setSaving(false)
    }
  }

  const pagesRef = useRef<HTMLOListElement>(null)
  useEffect(() => {
    const row = pagesRef.current?.querySelector('[aria-current]')?.closest('li')
    if (pagesRef.current && row) scrollWithin(pagesRef.current, row)
  }, [page?.uid])

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )
  const onPageDrag = ({ active, over }: DragEndEvent) => {
    if (!over || active.id === over.id) return
    setDraft((d) => {
      const from = d.pages.findIndex((s) => s.uid === active.id)
      const to = d.pages.findIndex((s) => s.uid === over.id)
      return from < 0 || to < 0 ? d : { ...d, pages: arrayMove(d.pages, from, to) }
    })
  }

  return (
    <div className="flex flex-col gap-2 xl:h-[calc(100dvh-5rem)]" data-testid="tour-editor">
      {/* The tour: its name (the title), on/off, the cycle summary, settings
          and the two actions, always in reach. Settings are set once and rarely
          touched, so they open in a dialog and the screen stays on the pages. */}
      <h1 className="sr-only">{draft.id == null ? t('tours.newTitle') : t('tours.editTitle', { name: initial.name })}</h1>
      <div className="sticky top-0 z-20 flex flex-wrap items-center gap-2 p-2 border border-[var(--border)] bg-[var(--panel-2)]" data-testid="tour-toolbar">
        <IconAction label={t('tours.backToList')} onClick={() => navigate(back)}><ArrowLeft size={16} /></IconAction>
        <Route size={16} className="shrink-0 text-[var(--accent)]" aria-hidden="true" />
        {pageOnly && page ? (
          <>
            <div className="flex min-w-0 flex-1 items-center gap-2 text-sm" data-testid="tour-page-only">
              <span className="truncate font-semibold">{draft.name}</span>
              <span className="shrink-0 flex items-center gap-1 text-xs tabular-nums text-[var(--text-dim)]">
                <IconAction label={t('tours.previous')} onClick={() => step(-1)} disabled={draft.pages.length < 2}><ChevronLeft size={15} /></IconAction>
                {t('tours.pageOf', { n: draft.pages.indexOf(page) + 1, total: draft.pages.length })}
                <IconAction label={t('tours.next')} onClick={() => step(1)} disabled={draft.pages.length < 2}><ChevronRight size={15} /></IconAction>
              </span>
            </div>
            <Button variant="outline" onClick={() => setPageOnly(false)} data-testid="tour-edit-whole">
              <SlidersHorizontal size={14} aria-hidden="true" /> {t('tours.editWhole')}
            </Button>
          </>
        ) : (
          <>
            <div className="min-w-[12rem] flex-1">
              <Input id="tour-name" value={draft.name} maxLength={100} autoFocus={draft.id == null}
                placeholder={t('tours.namePlaceholder')} aria-label={t('tours.name')} title={t('tours.name')}
                aria-invalid={nameError || undefined}
                className={clsx('font-semibold', nameError && '!border-[var(--danger)]')}
                onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} onBlur={() => setTouched(true)} />
            </div>
            <label className="flex items-center gap-2 text-sm">
              <Switch checked={draft.enabled} onChange={(v) => setDraft((d) => ({ ...d, enabled: v }))} label={t('tours.enabled')} />
              {t('tours.enabled')}
            </label>
            <div className="flex items-center gap-1.5 border-l border-[var(--border)] px-3 text-xs leading-4 whitespace-nowrap tabular-nums text-[var(--text-dim)]" data-testid="tour-summary">
              {cameras.length > 0 && (
                <span className={clsx('inline-flex items-center gap-1', notOnTour.length > 0 && 'font-semibold text-[color-mix(in_oklab,var(--warn)_70%,var(--text))]')}
                  title={notOnTour.length > 0 ? notOnTourText : undefined} data-testid="tour-cameras-assigned">
                  {notOnTour.length > 0 && <TriangleAlert size={12} aria-hidden="true" />}
                  {t('tours.camerasAssigned', { count: cameras.length - notOnTour.length, total: cameras.length })}
                </span>
              )}
              <span className="hidden xl:inline" aria-hidden="true">·</span>
              <span className="hidden xl:inline" data-testid="tour-cycle">
                {t('tours.cycleSummary', { pages: draft.pages.length, cycle: formatDuration(cycleSeconds(shown)) })}
              </span>
            </div>
            <button
              type="button"
              onClick={() => setSettingsOpen(true)}
              aria-haspopup="dialog"
              data-testid="tour-settings-toggle"
              className="h-[34px] px-2.5 inline-flex items-center gap-1.5 text-sm border border-[var(--border)] text-[var(--text)] hover:bg-[var(--panel)]"
            >
              <SlidersHorizontal size={14} aria-hidden="true" />
              {t('tours.settings')}
              {draft.settings.alerts.enabled && (
                <span className="inline-flex items-center gap-1 border-l border-[var(--border)] pl-1.5 ml-0.5 text-xs text-[var(--text-dim)]" data-testid="tour-alarms-on">
                  <BellRing size={12} className="text-[var(--danger)]" aria-hidden="true" />
                  {t('tours.alarmsOn')}
                </span>
              )}
            </button>
          </>
        )}
        <Button variant="ghost" onClick={() => navigate(back)}>{t('tours.cancel')}</Button>
        <Button variant="primary" onClick={save} disabled={saving || readOnly || (touched && !canSave)}>
          {saving ? t('tours.saving') : t('tours.save')}
        </Button>
      </div>
      {settingsOpen && (
        <TourSettingsDialog settings={draft.settings} onSettings={setSettings} onAlerts={setAlerts} onClose={() => setSettingsOpen(false)} />
      )}
      {nameError && <p className="text-xs text-[var(--danger)]">{t('tours.nameRequired')}</p>}

      {readOnly && <div className="text-sm text-[var(--text-dim)] border border-[var(--border)] p-2">{t('tours.readOnly')}</div>}
      {error && (
        <div role="alert" className="flex items-center gap-3 text-sm text-[var(--danger)] bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] border border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] p-2">
          <span className="flex-1">{error.text}</span>
          {error.conflict && <Button size="sm" variant="outline" onClick={onReload}>{t('tours.reload')}</Button>}
        </div>
      )}

      {/* The page editor. Two columns from xl up (pages | the selected page);
          below that one column in reading order. From xl the left column can
          fold to a rail of page numbers. From xl it all fits the window: each
          list scrolls inside itself and the page never does. */}
      <div className={clsx('grid gap-4 items-start xl:min-h-0 xl:flex-1 xl:grid-rows-[minmax(0,1fr)]',
        pageOnly ? 'xl:grid-cols-1' : rail ? 'xl:grid-cols-[3rem_minmax(0,1fr)]' : 'xl:grid-cols-[20rem_minmax(0,1fr)]')}>
        {rail && !pageOnly && (
          <nav className="hidden xl:flex xl:max-h-full xl:overflow-y-auto xl:col-start-1 xl:row-start-1 flex-col items-center gap-1 py-2 border border-[var(--border)] bg-[var(--panel-2)]" aria-label={t('tours.pages')} data-testid="tour-pages-rail">
            <IconAction label={t('tours.showPanel')} onClick={() => setRail(false)}><PanelLeftOpen size={15} /></IconAction>
            <span className="w-6 h-px bg-[var(--border)] my-1" aria-hidden="true" />
            {draft.pages.map((s, i) => {
              const name = s.label || t('tours.pageN', { n: i + 1 })
              const filled = camerasOf(s.camera_ids).length
              const tiles = resolve(s.layout_id)?.tiles.length ?? filled
              const summary = filled < tiles ? `${name}, ${t('tours.tilesUsed', { count: filled, tiles })}` : name
              return (
              <button key={s.uid} type="button" onClick={() => setSelected(s.uid)}
                title={summary} aria-label={summary} aria-current={s.uid === page?.uid || undefined}
                className={clsx('relative w-8 h-8 text-xs font-mono border',
                  s.uid === page?.uid ? 'border-[var(--accent)] bg-[var(--accent)] text-white' : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)]',
                  touched && camerasOf(s.camera_ids).length === 0 && 'border-[var(--danger)]')}>
                {i + 1}
                {filled < tiles && <span className="absolute -top-1 -right-1 size-2 rounded-full bg-[var(--warn)]" aria-hidden="true" data-testid="tour-rail-unfilled" />}
              </button>
              )
            })}
            <IconAction label={t('tours.addPage')} onClick={addPage}><Plus size={14} /></IconAction>
            {page && (
              <>
                <IconAction label={t('tours.duplicatePage')} onClick={() => duplicatePage(page.uid)}><CopyPlus size={13} /></IconAction>
                <IconAction label={t('tours.removePage')} onClick={() => removePage(page.uid)} disabled={draft.pages.length === 1} danger><Trash2 size={13} /></IconAction>
              </>
            )}
          </nav>
        )}

        <section className={clsx('flex min-h-0 flex-col border border-[var(--border)] bg-[var(--panel-2)] xl:max-h-full xl:col-start-1 xl:row-start-1', pageOnly && 'hidden', rail && 'xl:hidden')} data-testid="tour-pages">
          <header className="flex min-h-[47px] shrink-0 items-center gap-2 px-1 py-1 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))] text-sm font-medium" data-testid="tour-pages-header">
            {/* From xl the rail button folds this panel, so the title is just a
                title. Below that the list sits above the editor, with no rail,
                so the title collapses it. */}
            <button type="button" onClick={() => setPagesOpen(!pagesOpen)} aria-expanded={pagesOpen} data-testid="tour-pages-collapse"
              className="flex xl:hidden flex-1 min-w-0 items-center gap-1.5 px-2 py-0.5 text-left hover:bg-[var(--panel)]">
              {pagesOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              <span className="flex min-w-0 items-baseline gap-1.5">
                {t('tours.pages')} ({draft.pages.length})
                {pagesOpen && <span className="text-xs font-normal text-[var(--text-dim)] truncate">{t('tours.dragToReorder')}</span>}
              </span>
            </button>
            <div className="hidden xl:flex flex-1 min-w-0 items-baseline gap-1.5 px-2 py-0.5" data-testid="tour-pages-title">
              <span className="shrink-0">{t('tours.pages')} ({draft.pages.length})</span>
              <span className="text-xs font-normal text-[var(--text-dim)] truncate">{t('tours.dragToReorder')}</span>
            </div>
            <span className="hidden xl:inline-flex">
              <IconAction label={t('tours.hidePanel')} onClick={() => setRail(true)}><PanelLeftClose size={15} /></IconAction>
            </span>
          </header>
          <div className={clsx('flex min-h-0 flex-col', !pagesOpen && 'hidden xl:flex')} data-testid="tour-pages-list">
              <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={onPageDrag}>
                <SortableContext items={draft.pages.map((s) => s.uid)} strategy={verticalListSortingStrategy}>
                  <ol ref={pagesRef} className="min-h-0 max-h-64 flex-1 overflow-y-auto divide-y divide-[var(--border)] xl:max-h-none">
                    {draft.pages.map((s, i) => (
                      <PageRow
                        key={s.uid}
                        page={s}
                        index={i}
                        selected={s.uid === page?.uid}
                        layout={resolve(s.layout_id)}
                        invalid={touched && camerasOf(s.camera_ids).length === 0}
                        onSelect={() => setSelected(s.uid)}
                        canRemove={draft.pages.length > 1}
                        onDuplicate={() => duplicatePage(s.uid)}
                        onRemove={() => removePage(s.uid)}
                      />
                    ))}
                  </ol>
                </SortableContext>
              </DndContext>
              <div className="shrink-0 p-2 border-t border-[var(--border)]">
                <Button variant="outline" size="sm" onClick={addPage}><Plus size={14} /> {t('tours.addPage')}</Button>
              </div>
          </div>
        </section>

        {/* The selected page */}
        {page && (
          <div className={clsx('min-w-0 xl:h-full xl:row-start-1', pageOnly ? 'xl:col-start-1' : 'xl:col-start-2')}>
            <PageDetail
              key={page.uid}
              page={page}
              index={draft.pages.indexOf(page)}
              pageCount={draft.pages.length}
              windowSettings={windowSettings}
              cameras={cameras}
              pagesOf={pagesOf}
              camById={camById}
              badCameras={badCameras}
              badLayout={badLayouts.has(page.layout_id)}
              showErrors={touched}
              cover={draft.settings.fit === 'fill'}
              onChange={(patch) => setPage(page.uid, patch)}
              onSpill={(cams, tiles) => spill(page.uid, cams, tiles)}
            />
          </div>
        )}

      </div>
    </div>
  )
}

/** Bring a row into view by scrolling only its list: scrollIntoView would move
 *  the page too, and with it whatever was just clicked. */
function scrollWithin(list: HTMLElement, row: HTMLElement) {
  const top = row.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop
  if (top < list.scrollTop) list.scrollTop = top
  else if (top + row.offsetHeight > list.scrollTop + list.clientHeight) list.scrollTop = top + row.offsetHeight - list.clientHeight
}

/** What empty tiles mean on the wall. An empty tile is allowed: this informs, it doesn't block. */
function tilesEmptyMessage(t: ReturnType<typeof useTranslation>['t'], n: number) {
  return n === 1 ? t('tours.tilesEmptyOne') : t('tours.tilesEmptyMany', { count: n })
}

function PageRow({ page, index, selected, layout, invalid, onSelect, canRemove, onDuplicate, onRemove }: {
  page: DraftPage
  index: number
  selected: boolean
  layout: LayoutDefinition | null
  invalid: boolean
  onSelect: () => void
  canRemove: boolean
  onDuplicate: () => void
  onRemove: () => void
}) {
  const { t } = useTranslation()
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: page.uid })
  const count = camerasOf(page.camera_ids).length
  const tiles = layout?.tiles.length ?? count
  const empty = Math.max(0, tiles - count)
  return (
    <li
      ref={setNodeRef}
      style={{ transform: CSS.Transform.toString(transform), transition }}
      className={clsx('flex items-center gap-2 px-2 py-2', selected ? 'bg-[color-mix(in_oklab,var(--accent)_22%,var(--panel-2))] shadow-[inset_3px_0_0_var(--accent)]' : 'bg-[var(--panel-2)] hover:bg-[var(--panel)]', isDragging && 'relative z-10 shadow-lg')}
      data-testid="tour-page-row"
    >
      <button type="button" className="p-1 text-[var(--text-dim)] cursor-grab active:cursor-grabbing" aria-label={t('tours.dragPage', { n: index + 1 })} {...attributes} {...listeners}>
        <GripVertical size={14} />
      </button>
      <button type="button" onClick={onSelect} className="flex-1 min-w-0 flex items-center gap-2 text-left" aria-current={selected || undefined}>
        <span className={clsx('text-xs font-mono w-4 text-right', selected ? 'font-semibold text-[var(--accent)]' : 'text-[var(--text-dim)]')}>{index + 1}</span>
        {layout && <LayoutPreview layout={layout} small />}
        <span className="min-w-0 flex-1">
          <span className={clsx('block text-sm truncate', selected && 'font-semibold')}>{page.label || t('tours.pageN', { n: index + 1 })}</span>
          <span className="block truncate text-[11px] font-mono tabular-nums text-[var(--text-dim)]">
            {count === 0
              ? <span className={invalid ? 'text-[var(--danger)]' : 'text-[var(--warn)]'}>{t('tours.noCamerasYet')}</span>
              : (
                <>
                  <span className={clsx(empty > 0 && 'font-semibold text-[color-mix(in_oklab,var(--warn)_70%,var(--text))]')} title={empty > 0 ? tilesEmptyMessage(t, empty) : undefined} data-testid={empty > 0 ? 'tour-page-unfilled' : undefined}>
                    {empty > 0 && <CircleDashed size={10} className="inline -mt-0.5 mr-1" aria-hidden="true" />}
                    {t('tours.tilesUsed', { count, tiles })}
                  </span>
                  {` · ${formatDuration(page.duration_s)}`}
                </>
              )}
          </span>
        </span>
      </button>
      {/* On the selected row only, so a delete can't land on the wrong page. */}
      {selected && (
        <span className="shrink-0 flex items-center gap-0.5">
          <IconAction label={t('tours.duplicatePage')} onClick={onDuplicate}><CopyPlus size={13} /></IconAction>
          <IconAction label={t('tours.removePage')} onClick={onRemove} disabled={!canRemove} danger><Trash2 size={13} /></IconAction>
        </span>
      )}
    </li>
  )
}

/** "Which alarms": what each choice sends to the wall. */
type AlertSourceChoice = 'both' | AlertSource
const ALERT_SOURCES: Record<AlertSourceChoice, AlertSource[]> = {
  both: ['app_alert', 'camera_event'],
  app_alert: ['app_alert'],
  camera_event: ['camera_event'],
}

/** A whole number of seconds, saying so inside the field. The arrow keys step
 *  it; an out-of-range value waits for blur, as forcing each keystroke into
 *  range turned "30" into 5 at the "3" and 50 at the "0". */
function SecondsInput({ id, value, min, max, onChange }: {
  id: string
  value: number
  min: number
  max: number
  onChange: (seconds: number) => void
}) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState<string | null>(null)
  return (
    <span className="relative block">
      <Input
        id={id}
        type="number"
        min={min}
        max={max}
        value={draft ?? value}
        className="!pr-16 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
        onBlur={() => {
          if (draft == null) return
          const n = Number(draft)
          onChange(draft.trim() === '' || Number.isNaN(n) ? value : Math.min(max, Math.max(min, Math.round(n))))
          setDraft(null)
        }}
        onChange={(e) => {
          setDraft(e.target.value)
          const n = Number(e.target.value)
          if (e.target.value.trim() !== '' && Number.isInteger(n) && n >= min && n <= max) onChange(n)
        }}
      />
      <span className="pointer-events-none absolute right-2.5 top-1/2 -translate-y-1/2 text-xs text-[var(--text-dim)]" aria-hidden="true">{t('tours.seconds')}</span>
    </span>
  )
}

/** One setting: what it does on the left, its control on the right, so every
 *  control in the dialog lines up in one column. */
function SettingRow({ label, hint, htmlFor, children }: { label: ReactNode; hint?: ReactNode; htmlFor?: string; children: ReactNode }) {
  return (
    <div className="grid gap-x-4 gap-y-1 sm:grid-cols-[minmax(0,1fr)_15rem] sm:items-center">
      <div className="min-w-0">
        <label htmlFor={htmlFor} className="block text-sm">{label}</label>
        {hint && <p className="text-xs text-[var(--text-dim)]">{hint}</p>}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  )
}

function TourSettingsDialog({ settings, onSettings, onAlerts, onClose }: {
  settings: TourSettings
  onSettings: (patch: Partial<TourSettings>) => void
  onAlerts: (patch: Partial<TourSettings['alerts']>) => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const a = settings.alerts
  const source: AlertSourceChoice = a.sources.length > 1 ? 'both' : a.sources[0] ?? 'both'
  return (
    <Modal
      open
      title={t('tours.settings')}
      onClose={onClose}
      widthClassName="w-[600px]"
      data-testid="tour-settings"
      footer={(
        <div className="flex w-full items-center justify-between gap-3">
          <span className="text-xs text-[var(--text-dim)]">{t('tours.settingsApplyOnSave')}</span>
          <Button variant="primary" onClick={onClose} data-testid="tour-settings-done">{t('tours.settingsDone')}</Button>
        </div>
      )}
    >
      <div className="space-y-5">
        <FormSection title={t('tours.settingsPlayback')}>
          <SettingRow label={t('tours.fitLabel')} htmlFor="tour-set-fit">
            <Select id="tour-set-fit" value={settings.fit} onChange={(e) => onSettings({ fit: e.target.value as TourSettings['fit'] })}>
              <option value="fit">{t('tours.fitFit')}</option>
              <option value="fill">{t('tours.fitFill')}</option>
            </Select>
          </SettingRow>
          <SettingRow label={t('tours.transitionLabel')} htmlFor="tour-set-transition">
            <Select id="tour-set-transition" value={settings.transition} onChange={(e) => onSettings({ transition: e.target.value as TourSettings['transition'] })}>
              <option value="fade">{t('tours.transitionFade')}</option>
              <option value="cut">{t('tours.transitionCut')}</option>
            </Select>
          </SettingRow>
          <SettingRow label={t('tours.preload')} hint={t('tours.preloadHint')}>
            <div className="flex justify-end"><Switch checked={settings.preload} onChange={(v) => onSettings({ preload: v })} label={t('tours.preload')} /></div>
          </SettingRow>
        </FormSection>

        <FormSection title={t('tours.settingsAlarms')}>
          <SettingRow label={t('tours.alertsEnable')} hint={t('tours.alertsHint')}>
            <div className="flex justify-end"><Switch checked={a.enabled} onChange={(v) => onAlerts({ enabled: v })} label={t('tours.alertsEnable')} /></div>
          </SettingRow>
          {/* Always shown, greyed while the switch is off: what it would do stays
              readable, and turning it on moves nothing. */}
          <fieldset disabled={!a.enabled} className={clsx('space-y-3 border-l-2 border-[var(--border)] pl-3', !a.enabled && 'opacity-50')} data-testid="tour-alert-options">
            <SettingRow label={t('tours.alertWhichAlarms')} hint={t(`tours.alertSourceHint.${source}`)} htmlFor="tour-set-source">
              <Select id="tour-set-source" value={source} data-testid="tour-alert-source" onChange={(e) => onAlerts({ sources: ALERT_SOURCES[e.target.value as AlertSourceChoice] })}>
                {(Object.keys(ALERT_SOURCES) as AlertSourceChoice[]).map((id) => (
                  <option key={id} value={id}>{t(`tours.alertSourceOption.${id}`)}</option>
                ))}
              </Select>
            </SettingRow>
            <SettingRow
              label={t('tours.alertMinSeverity')}
              hint={source === 'camera_event' ? t('tours.alertSeverityAppOnly') : t('tours.alertSeverityHint')}
              htmlFor="tour-set-severity"
            >
              <Select id="tour-set-severity" value={a.min_severity} disabled={source === 'camera_event'} data-testid="tour-alert-severity" onChange={(e) => onAlerts({ min_severity: e.target.value as AlertSeverity })}>
                {(['low', 'medium', 'high', 'critical'] as AlertSeverity[]).map((s) => (
                  <option key={s} value={s}>{t(`tours.severityFrom.${s}`)}</option>
                ))}
              </Select>
            </SettingRow>
            <SettingRow label={t('tours.alertWhichCameras')} htmlFor="tour-set-scope">
              <Select id="tour-set-scope" value={a.scope} onChange={(e) => onAlerts({ scope: e.target.value as 'tour' | 'visible' })}>
                <option value="tour">{t('tours.alertScopeTour')}</option>
                <option value="visible">{t('tours.alertScopeVisible')}</option>
              </Select>
            </SettingRow>
            <SettingRow label={t('tours.alertHold')} htmlFor="tour-set-hold">
              <SecondsInput id="tour-set-hold" min={5} max={600} value={a.hold_s} onChange={(v) => onAlerts({ hold_s: v })} />
            </SettingRow>
            <SettingRow label={t('tours.alertCooldownShort')} htmlFor="tour-set-cooldown">
              <SecondsInput id="tour-set-cooldown" min={0} max={3600} value={a.cooldown_s} onChange={(v) => onAlerts({ cooldown_s: v })} />
            </SettingRow>
          </fieldset>
        </FormSection>
      </div>
    </Modal>
  )
}

type LayoutOption = { id: string; label: string; tiles: number }

function layoutOptions(ws: WindowLayoutSettings | undefined): { builtIn: LayoutOption[]; grids: LayoutOption[]; custom: LayoutOption[] } {
  const builtIn = Object.entries(PREDEFINED_LAYOUTS)
    .filter(([id]) => ws?.layouts_enabled?.[id] !== false)
    .map(([id, l]) => ({ id, label: l.name, tiles: l.tiles.length }))
  const grids = PLAIN_GRIDS.map(([c, r]) => ({ id: `grid:${c}x${r}`, label: `${c}×${r}`, tiles: c * r }))
  const custom = (ws?.custom_layouts ?? [])
    .filter((l) => l.enabled !== false)
    .map((l) => ({ id: l.id, label: l.name, tiles: resolveLayout(l.id, ws)?.tiles.length ?? 0 }))
  return { builtIn, grids, custom }
}

function PageDetail({ page, index, pageCount, windowSettings, cameras, pagesOf, camById, badCameras, badLayout, showErrors, cover, onChange, onSpill }: {
  page: DraftPage
  index: number
  pageCount: number
  windowSettings: WindowLayoutSettings | undefined
  cameras: CameraItem[]
  /** Every page each camera on the tour is on, numbered from 1. */
  pagesOf: Map<number, number[]>
  camById: Map<number, CameraItem>
  badCameras: Set<number>
  badLayout: boolean
  showErrors: boolean
  cover: boolean
  onChange: (patch: Partial<DraftPage>) => void
  /** Cameras that did not fit: onto new pages of `tiles` right after this one. */
  onSpill: (cams: number[], tiles: number) => void
}) {
  const { t } = useTranslation()
  const confirm = useConfirm()
  const options = useMemo(() => layoutOptions(windowSettings), [windowSettings])
  // Any grid:CxR is valid, offered or not (a tour made through the API may use 2×2).
  const plain = parseGridId(page.layout_id)
  const extraGrid = plain && !options.grids.some((o) => o.id === page.layout_id)
    ? { id: page.layout_id, label: `${plain[0]}×${plain[1]}`, tiles: plain[0] * plain[1] }
    : null
  const known = !!plain || [...options.builtIn, ...options.custom].some((o) => o.id === page.layout_id)
  const custom = !DURATION_PRESETS.includes(page.duration_s)
  // The page as the wall will show it, even with every tile empty: cameras can still be put on it.
  const [shown] = buildPages([page], (lid) => resolveLayout(lid, windowSettings), { keepEmptyPages: true })
  const layout = shown.layout
  const tiles = layout.tiles.length
  const cameraCount = camerasOf(page.camera_ids).length
  const emptyTiles = Math.max(0, tiles - cameraCount)
  const hasGaps = page.camera_ids.includes(null)
  // Live video only when asked for: one camera at a time, in a dialog.
  const [live, setLive] = useState<number | null>(null)
  // As on a running wall's Arrange: a chosen tile that the next camera
  // clicked in the list goes to, and what the last change did, e.g. that
  // the camera was already on another tile or that some went onto new pages.
  const [target, setTarget] = useState<number | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [dragging, setDragging] = useState<number | null>(null)
  const nameOf = (id: number) => camById.get(id)?.name ?? `#${id}`
  // A layout Live View no longer offers stands in as the automatic grid.
  const tilesOf = (lid: string) => resolveLayout(lid, windowSettings)?.tiles.length || AUTO_GRID_MAX
  const spilledNotice = (n: number, per: number) => {
    const pages = Math.ceil(n / per)
    return n === 1 ? t('tours.spilledOneCamera') : pages === 1 ? t('tours.spilledOnePage', { count: n }) : t('tours.spilledMany', { count: n, pages })
  }

  const place = (cam: number, at: number) => {
    setNotice(placeMessage(t, page.camera_ids, cam, at, nameOf, tiles))
    onChange({ camera_ids: placeAt(page.camera_ids, cam, at, tiles) })
    setTarget(null)
  }
  const choose = (at: number) => { setTarget(target === at ? null : at); setNotice(null) }
  // A smaller layout keeps the cameras on its tiles where they are; the
  // rest go onto new pages after this one rather than disappearing.
  const changeLayout = (lid: string) => {
    const per = tilesOf(lid)
    const { ids, rest } = fit(page.camera_ids, per)
    onChange({ layout_id: lid, camera_ids: ids })
    onSpill(rest, per)
    setTarget(null)
    setNotice(rest.length ? spilledNotice(rest.length, per) : null)
  }
  // An unnamed page whose cameras share one location is named after it
  // (and so are the pages its overflow goes onto).
  const withCameras = (ids: Slot[]): Partial<DraftPage> => {
    const locs = new Set(camerasOf(ids).map((id) => camById.get(id)?.location?.trim()).filter(Boolean))
    return !page.label && locs.size === 1 ? { camera_ids: ids, label: [...locs][0] as string } : { camera_ids: ids }
  }
  // Unticked, a camera leaves its tile empty; ticked, it fills the first
  // empty tile, and on a full page nothing changes but the hint.
  const toggle = (cam: number) => {
    setTarget(null)
    const at = page.camera_ids.indexOf(cam)
    if (at >= 0) { onChange({ camera_ids: removeAt(page.camera_ids, at) }); setNotice(null); return }
    if (firstEmpty(page.camera_ids, tiles) == null) {
      setNotice(null)
      confirm({ title: t('tours.pageFullTitle'), message: t('tours.pageFull', { name: nameOf(cam) }), confirmLabel: t('tours.pageFullAdd') })
        .then((ok) => { if (ok) { onSpill([cam], tiles); setNotice(spilledNotice(1, tiles)) } })
      return
    }
    onChange(withCameras(addCamera(page.camera_ids, cam, tiles)))
    setNotice(null)
  }
  // "Add remaining": as many as fit here, the rest on new pages after this one.
  const addAll = (cams: number[]) => {
    const { ids, rest } = fill(page.camera_ids, cams, tiles)
    onChange(withCameras(ids))
    onSpill(rest, tiles)
    setTarget(null)
    setNotice(rest.length ? spilledNotice(rest.length, tiles) : null)
  }

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 4 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  )
  // A camera from the list lands where the pointer is, or nowhere (let go
  // over the list and nothing changes); a tile being reordered snaps to
  // the nearest one.
  const collide: CollisionDetection = (args) =>
    String(args.active.id).startsWith('pick:') ? pointerWithin(args) : closestCenter(args)
  const onDragStart = ({ active }: DragStartEvent) => {
    const id = String(active.id)
    // Only a camera from the list needs an overlay: a tile moves itself.
    setDragging(id.startsWith('pick:') ? Number(id.slice(5)) : null)
  }
  const onCameraDrag = ({ active, over }: DragEndEvent) => {
    setDragging(null)
    if (!over || active.id === over.id) return
    const ids = page.camera_ids
    const overId = String(over.id)
    const at = overId.startsWith('slot:') ? Number(overId.slice(5)) : ids.indexOf(Number(over.id))
    if (at < 0) return
    const activeId = String(active.id)
    if (activeId.startsWith('pick:')) { place(Number(activeId.slice(5)), at); return }
    setNotice(null)
    // Swapped, as on a running wall: the cameras between the two stay put.
    onChange({ camera_ids: moveTo(ids, ids.indexOf(Number(active.id)), at, tiles) })
  }

  return (
    <section className="@container flex min-w-0 flex-col border border-[var(--border)] bg-[var(--panel-2)] xl:h-full" data-testid="tour-page-editor" aria-label={t('tours.pageN', { n: index + 1 })}
      onKeyDown={(e) => { if (e.key === 'Escape' && target != null) { e.stopPropagation(); setTarget(null) } }}>
      <header className="flex shrink-0 items-center gap-2 px-2 py-1.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
        <span className="shrink-0 w-6 h-6 inline-flex items-center justify-center text-xs font-semibold font-mono bg-[var(--accent)] text-white">{index + 1}</span>
        <div className="min-w-0 flex-1">
          <Input value={page.label} maxLength={60} placeholder={t('tours.pageLabelPlaceholder')}
            aria-label={t('tours.pageLabel')} title={t('tours.pageLabel')} onChange={(e) => onChange({ label: e.target.value })} />
        </div>
      </header>
      {(badLayout || !known) && <p className="px-2 pt-1.5 text-xs text-[var(--danger)]">{t('tours.layoutUnavailable')}</p>}

      <div className="min-h-0 flex-1 p-2">
        {/* Two panes: pick cameras on the left, arrange them on the right in
            the preview itself (drag tiles to reorder, x to remove). They stack
            when the panel is too narrow for both. */}
        <DndContext sensors={sensors} collisionDetection={collide} onDragStart={onDragStart} onDragEnd={onCameraDrag} onDragCancel={() => setDragging(null)}>
        <div className="grid gap-3 items-start @min-[40rem]:grid-cols-[minmax(15rem,1fr)_min(58%,max(20rem,calc((100vh-23rem)*16/9)))] xl:h-full xl:grid-rows-[minmax(0,1fr)] xl:items-stretch">
          <CameraPicker
            cameras={cameras}
            pagesOf={pagesOf}
            pageNumber={index + 1}
            targetCamera={target != null ? shown.cameraIds[target] ?? null : null}
            selected={page.camera_ids}
            target={target}
            onPick={(cam) => target != null && place(cam, target)}
            onLive={setLive}
            onToggle={toggle}
            onAddAll={addAll}
          />

          <div className="min-w-0 space-y-2 xl:overflow-y-auto" data-testid="tour-page-preview-pane">
            {/* This column is as wide as the screen can be at this window height,
                and the list takes the rest, so nothing is left beside the screen.
                Three rows level with the picker's: the page's controls with the
                search, the preview's title and what acts on its tiles with the
                location filter, the screen with the top of the list. */}
            <div className="mt-[9px] flex min-h-[34px] flex-wrap items-center gap-2 text-sm" data-testid="tour-page-controls">
              <label className="flex shrink-0 items-center gap-1.5 text-xs text-[var(--text-dim)]">
                {t('tours.layout')}
                <div className="w-28 text-sm text-[var(--text)]">
                <Select value={page.layout_id} onChange={(e) => changeLayout(e.target.value)}
                  className={clsx((badLayout || !known) && '!border-[var(--danger)]')}>
                  {!known && <option value={page.layout_id}>{t('tours.layoutGone', { id: page.layout_id })}</option>}
                  <optgroup label={t('tours.layoutsGrids')}>
                    {extraGrid && <option value={extraGrid.id}>{extraGrid.label} ({extraGrid.tiles})</option>}
                    {options.grids.map((o) => <option key={o.id} value={o.id}>{o.label} ({o.tiles})</option>)}
                  </optgroup>
                  <optgroup label={t('tours.layoutsBuiltIn')}>
                    {options.builtIn.map((o) => <option key={o.id} value={o.id}>{o.label} ({o.tiles})</option>)}
                  </optgroup>
                  {options.custom.length > 0 && (
                    <optgroup label={t('tours.layoutsCustom')}>
                      {options.custom.map((o) => <option key={o.id} value={o.id}>{o.label} ({o.tiles})</option>)}
                    </optgroup>
                  )}
                </Select>
                </div>
              </label>
              <div className="shrink-0 flex items-center gap-1">
                <label className="flex items-center gap-1.5 text-xs text-[var(--text-dim)]" title={t('tours.duration')}>
                  {t('tours.showFor')}
                  <div className="w-[5.5rem] text-sm text-[var(--text)]">
                  <Select
                    value={custom ? 'custom' : String(page.duration_s)}
                    onChange={(e) => onChange({ duration_s: e.target.value === 'custom' ? (custom ? page.duration_s : 90) : Number(e.target.value) })}
                  >
                    {DURATION_PRESETS.map((d) => <option key={d} value={d}>{formatDuration(d)}</option>)}
                    <option value="custom">{t('tours.durationCustom')}</option>
                  </Select>
                  </div>
                </label>
                {custom && (
                  // Sized by a wrapper: the shared control class is always w-full.
                  <div className="w-16">
                    <Input
                      type="number" min={MIN_DURATION} max={MAX_DURATION} value={page.duration_s}
                      aria-label={t('tours.durationSeconds')} title={t('tours.durationSeconds')}
                      onChange={(e) => onChange({ duration_s: Math.min(MAX_DURATION, Math.max(MIN_DURATION, Number(e.target.value) || MIN_DURATION)) })}
                    />
                  </div>
                )}
              </div>
            </div>
            <div className="flex min-h-[34px] min-w-0 items-center gap-2 text-sm" data-testid="tour-preview-title">
              <span className="font-medium">{t('tours.preview')}</span>
              <InfoTip label={t('tours.preview')}>{t('tours.editorHint')}</InfoTip>
              <span className="ml-auto min-w-0 truncate text-xs text-[var(--text-dim)]" title={page.label || undefined}>
                {t('tours.pageOf', { n: index + 1, total: pageCount })}{page.label ? ` · ${page.label}` : ''}
              </span>
              {cameraCount > 0 && (
                <span className="shrink-0 flex items-center gap-1">
                  {hasGaps && (
                    <Button size="sm" variant="ghost" onClick={() => { onChange({ camera_ids: closeGaps(page.camera_ids) }); setTarget(null); setNotice(null) }}
                      title={t('tours.closeGapsHint')} aria-label={t('tours.closeGaps')} data-testid="tour-close-gaps">
                      <Rows3 size={12} /> <span className="hidden @min-[56rem]:inline">{t('tours.closeGaps')}</span>
                    </Button>
                  )}
                  <Button size="sm" variant="ghost" onClick={() => { onChange({ camera_ids: [] }); setTarget(null); setNotice(null) }}>{t('tours.clearCameras')}</Button>
                </span>
              )}
            </div>
            <SortableContext items={camerasOf(shown.cameraIds)} strategy={rectSortingStrategy}>
              <PagePreview
                layout={layout}
                cameraIds={shown.cameraIds}
                camById={camById}
                cover={cover}
                bad={badCameras}
                target={target}
                onSelect={choose}
                onLive={setLive}
                onRemove={(cid) => { onChange({ camera_ids: removeAt(page.camera_ids, page.camera_ids.indexOf(cid)) }); setTarget(null); setNotice(null) }}
              />
            </SortableContext>
            <div className="space-y-0.5 text-xs">
              <p className={cameraCount === 0 ? (showErrors ? 'text-[var(--danger)]' : 'text-[var(--text-dim)]') : emptyTiles > 0 ? 'text-[var(--warn)]' : 'text-[var(--text-dim)]'} data-testid="tour-tile-count">
                {t('tours.tilesHaveCameras', { count: cameraCount, tiles })}
                {cameraCount === 0 ? ` ${t('tours.previewEmpty')}` : emptyTiles > 0 ? ` ${tilesEmptyMessage(t, emptyTiles)}` : ''}
              </p>
              <p className="empty:hidden text-[var(--accent)] font-medium" role="status" data-testid="tour-page-notice">
                {target != null ? t('tours.editorTarget', { n: target + 1 }) : notice}
              </p>
            </div>
          </div>
        </div>
        <DragOverlay dropAnimation={null}>
          {dragging != null && (
            <div className="flex items-center gap-2 w-56 p-1 pr-2 text-xs text-white bg-black/85 border border-[var(--accent)] shadow-lg">
              <CameraThumb cameraId={dragging} alt="" className="w-14 aspect-video shrink-0" />
              <span className="truncate">{nameOf(dragging)}</span>
            </div>
          )}
        </DragOverlay>
        </DndContext>
      </div>
      {live != null && <LivePeek cameraId={live} cam={camById.get(live)} onClose={() => setLive(null)} />}
    </section>
  )
}

/** The page as the wall will show it, from stills; each tile can be
 *  dragged to reorder, removed, or opened live. */
function PagePreview({ layout, cameraIds, camById, cover, bad, target, onSelect, onLive, onRemove }: {
  layout: LayoutDefinition
  cameraIds: Slot[]
  camById: Map<number, CameraItem>
  cover: boolean
  bad: Set<number>
  /** The chosen tile (0-based), if any. */
  target: number | null
  onSelect: (slot: number) => void
  onLive: (cameraId: number) => void
  onRemove: (cameraId: number) => void
}) {
  return (
    // Drawn as a display on its stand: this is the screen the wall will show.
    // Dark in both themes, like the hardware it stands for.
    <div className="w-full" data-testid="tour-page-screen">
      <div className="rounded border border-[#2c3646] bg-[#05070b] p-1.5 shadow-md">
        <div
          className="aspect-video bg-black p-px"
          style={{ display: 'grid', gridTemplateColumns: `repeat(${layout.gridCols}, minmax(0, 1fr))`, gridTemplateRows: `repeat(${layout.gridRows}, minmax(0, 1fr))`, gap: '1px' }}
          data-testid="tour-page-preview"
        >
          {layout.tiles.map((tile, i) => {
            const cid = cameraIds[i]
            const area = { gridRow: `${tile.row + 1} / span ${tile.rowSpan}`, gridColumn: `${tile.col + 1} / span ${tile.colSpan}` }
            return cid != null ? (
              <PreviewTile key={cid} cameraId={cid} order={i + 1} area={area} cam={camById.get(cid)} cover={cover}
                bad={bad.has(cid)} selected={target === i} onSelect={() => onSelect(i)} onLive={() => onLive(cid)} onRemove={() => onRemove(cid)} />
            ) : (
              <EmptyTile key={`e${i}`} slot={i} order={i + 1} area={area} selected={target === i} onSelect={() => onSelect(i)} />
            )
          })}
        </div>
      </div>
      <div className="mx-auto h-2.5 w-14 bg-[#2c3646]" aria-hidden="true" />
      <div className="mx-auto h-1 w-28 rounded-b bg-[#3a4657]" aria-hidden="true" />
    </div>
  )
}

/** An empty tile: a camera can be dropped on it, or it can be chosen. */
function EmptyTile({ slot, order, area, selected, onSelect }: {
  slot: number
  order: number
  area: React.CSSProperties
  selected: boolean
  onSelect: () => void
}) {
  const { t } = useTranslation()
  const drop = useDroppable({ id: `slot:${slot}` })
  return (
    <button
      ref={drop.setNodeRef}
      type="button"
      style={area}
      onClick={onSelect}
      aria-pressed={selected}
      aria-label={t('tours.arrangeEmptyTile', { n: order })}
      title={t('tours.arrangeEmptyTile', { n: order })}
      data-testid="tour-preview-empty"
      className={clsx('flex flex-col items-center justify-center gap-0.5 border border-dashed bg-[#0b0f16] text-[11px] text-white/55 hover:bg-white/5',
        selected ? 'border-transparent ring-2 ring-inset ring-[var(--accent)]' : drop.isOver ? 'border-transparent ring-2 ring-inset ring-[var(--accent)] bg-[var(--accent)]/15' : 'border-white/10')}
    >
      <span className="font-mono text-[10px] text-white/35">{order}</span>
      {t('tours.tileEmpty')}
    </button>
  )
}

function PreviewTile({ cameraId, order, area, cam, cover, bad, selected, onSelect, onLive, onRemove }: {
  cameraId: number
  order: number
  area: React.CSSProperties
  cam: CameraItem | undefined
  cover: boolean
  bad: boolean
  selected: boolean
  onSelect: () => void
  onLive: () => void
  onRemove: () => void
}) {
  const { t } = useTranslation()
  const { attributes, listeners, setNodeRef, transform, transition, isDragging, isOver, active } = useSortable({ id: cameraId })
  // A camera from the list held over this tile would replace it.
  const pickOver = isOver && String(active?.id ?? '').startsWith('pick:')
  const name = cam?.name ?? `#${cameraId}`
  return (
    <div
      ref={setNodeRef}
      style={{ ...area, transform: CSS.Transform.toString(transform), transition }}
      className={clsx('group/tile relative min-w-0 min-h-0 overflow-hidden bg-[#0b0f16]', isDragging && 'z-10 opacity-80 ring-2 ring-[var(--accent)]', bad && 'ring-2 ring-inset ring-[var(--danger)]')}
      data-testid="tour-preview-tile"
      data-camera-id={cameraId}
    >
      <div className="absolute inset-0 cursor-grab active:cursor-grabbing" {...attributes} {...listeners}
        onClick={onSelect} aria-pressed={selected} aria-label={t('tours.arrangeTile', { n: order, name })} title={t('tours.arrangeTile', { n: order, name })}>
        <CameraThumb cameraId={cameraId} alt={name} className={clsx('absolute inset-0', !cover && '[&_img]:object-contain')} />
      </div>
      <div className={clsx('pointer-events-none absolute inset-0',
        selected ? 'ring-4 ring-inset ring-[var(--accent)]' : pickOver && 'ring-2 ring-inset ring-[var(--accent)] bg-[var(--accent)]/15')} />
      <span className="pointer-events-none absolute left-1 top-1 px-1 text-[10px] font-mono text-white bg-black/60">{order}</span>
      <span className="pointer-events-none absolute left-1 right-1 bottom-0.5 text-[10px] text-white truncate [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">{name}</span>
      {/* Shown on hover or keyboard focus; always reachable by Tab. */}
      <span className="absolute right-1 top-1 flex gap-0.5 opacity-0 group-hover/tile:opacity-100 focus-within:opacity-100 transition-opacity">
        <button type="button" onClick={onLive} className="p-1 text-white bg-black/65 hover:bg-black/85" aria-label={t('tours.watchLive', { name })} title={t('tours.watchLive', { name })}>
          <Play size={11} />
        </button>
        <button type="button" onClick={onRemove} className="p-1 text-white bg-black/65 hover:bg-[var(--danger)]" aria-label={t('tours.removeCamera', { name })} title={t('tours.removeCamera', { name })}>
          <X size={11} />
        </button>
      </span>
    </div>
  )
}

function LayoutPreview({ layout, small }: { layout: LayoutDefinition; small?: boolean }) {
  return (
    <div
      className={clsx('shrink-0 aspect-video bg-[var(--bg)] p-[2px]', small ? 'w-9' : 'w-12')}
      style={{ display: 'grid', gridTemplateColumns: `repeat(${layout.gridCols}, 1fr)`, gridTemplateRows: `repeat(${layout.gridRows}, 1fr)`, gap: '1px' }}
      aria-hidden="true"
    >
      {layout.tiles.map((tile, i) => (
        <div key={i} className="bg-[var(--accent)]/40"
          style={{ gridRow: `${tile.row + 1} / span ${tile.rowSpan}`, gridColumn: `${tile.col + 1} / span ${tile.colSpan}` }} />
      ))}
    </div>
  )
}

const NO_LOCATION = '\u0000none'

function CameraPicker({ cameras, pagesOf, pageNumber, targetCamera, selected, target, onPick, onToggle, onAddAll, onLive }: {
  cameras: CameraItem[]
  pagesOf: Map<number, number[]>
  /** The page being edited, numbered from 1. */
  pageNumber: number
  /** The camera on the chosen tile, if any: marked in the list and scrolled to. */
  targetCamera: number | null
  /** The page's tiles; null is an empty one. */
  selected: Slot[]
  /** A chosen tile: a click then puts the camera there. */
  target: number | null
  onPick: (cameraId: number) => void
  /** Ticked or unticked. */
  onToggle: (cameraId: number) => void
  /** "Add remaining": these cameras, in order. */
  onAddAll: (cameraIds: number[]) => void
  onLive: (cameraId: number) => void
}) {
  const { t } = useTranslation()
  const position = useMemo(() => new Map(selected.flatMap((id, i) => (id == null ? [] : [[id, i] as const]))), [selected])
  const [q, setQ] = useState('')
  const [location, setLocation] = useState('')

  const locations = useMemo(() => {
    const counts = new Map<string, number>()
    for (const c of cameras) {
      const loc = c.location?.trim() || NO_LOCATION
      counts.set(loc, (counts.get(loc) ?? 0) + 1)
    }
    return [...counts.entries()].sort(([a], [b]) => (a === NO_LOCATION ? 1 : b === NO_LOCATION ? -1 : a.localeCompare(b)))
  }, [cameras])

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return cameras
      .filter((c) => !location || (c.location?.trim() || NO_LOCATION) === location)
      .filter((c) => !needle || c.name.toLowerCase().includes(needle) || c.ip_address?.includes(needle) || c.location?.toLowerCase().includes(needle))
      .sort((a, b) => (a.location ?? '').localeCompare(b.location ?? '') || a.name.localeCompare(b.name))
  }, [cameras, q, location])

  // Of those on show, the ones on no page yet, so adding never repeats a camera.
  const remaining = shown.filter((c) => !pagesOf.has(c.id))

  const listRef = useRef<HTMLUListElement>(null)
  useEffect(() => {
    const row = targetCamera != null ? listRef.current?.querySelector<HTMLElement>(`[data-camera-id="${targetCamera}"]`) : null
    if (listRef.current && row) scrollWithin(listRef.current, row)
  }, [targetCamera, shown])
  const locationLabel = (loc: string) => (loc === NO_LOCATION ? t('tours.noLocation') : loc)

  return (
    <div className="@container flex min-w-0 min-h-0 flex-col border border-[var(--border)] bg-[var(--panel)]" data-testid="tour-camera-picker" role="group" aria-label={t('tours.addCameras')}>
      {/* Search on its own line (the pane is narrow); location and "add all" share the next. */}
      <div className="shrink-0 space-y-2 p-2 border-b border-[var(--border)]">
        <div className="relative">
          <Search size={13} className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" aria-hidden="true" />
          <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('tours.searchCameras')} className="pl-7" aria-label={t('tours.searchCameras')} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-[10rem] flex-1">
            <Select value={location} onChange={(e) => setLocation(e.target.value)} aria-label={t('tours.location')}>
              <option value="">{t('tours.allLocations', { count: cameras.length })}</option>
              {locations.map(([loc, n]) => <option key={loc} value={loc}>{locationLabel(loc)} ({n})</option>)}
            </Select>
          </div>
          {remaining.length > 0 && (
            <Button size="sm" variant="outline" className="shrink-0 whitespace-nowrap" onClick={() => onAddAll(remaining.map((c) => c.id))}
              title={t('tours.addRemainingHint')} data-testid="tour-add-remaining">
              <span className="@min-[20rem]:hidden">{t('tours.addRemainingShort', { count: remaining.length })}</span>
              <span className="hidden @min-[20rem]:inline">{t('tours.addRemaining', { count: remaining.length })}</span>
            </Button>
          )}
        </div>
      </div>
      {shown.length === 0 ? (
        <div className="p-3 text-xs text-[var(--text-dim)]">{t('tours.noCamerasMatch')}</div>
      ) : (
        <ul ref={listRef} className="max-h-[calc(100vh-18rem)] min-h-48 flex-1 overflow-auto divide-y divide-[var(--border)] xl:max-h-none xl:min-h-0">
          {shown.map((c) => (
            <PickerRow key={c.id} cam={c} at={position.get(c.id)} otherPages={(pagesOf.get(c.id) ?? []).filter((n) => n !== pageNumber)} current={c.id === targetCamera} choosing={target != null}
              onToggle={() => onToggle(c.id)} onPick={() => onPick(c.id)} onLive={() => onLive(c.id)} />
          ))}
        </ul>
      )}
    </div>
  )
}

/** A camera in the picker: tick to add or remove it, drag it onto a tile,
 *  or, with a tile chosen, click to put it there. */
function PickerRow({ cam, at, otherPages, current, choosing, onToggle, onPick, onLive }: {
  cam: CameraItem
  /** Its tile on the page, when it is on it. */
  at: number | undefined
  /** The other pages of the tour it is on. */
  otherPages: number[]
  /** It is on the tile just chosen. */
  current: boolean
  choosing: boolean
  onToggle: () => void
  onPick: () => void
  onLive: () => void
}) {
  const { t } = useTranslation()
  const drag = useDraggable({ id: `pick:${cam.id}` })
  const onPage = at != null
  const details = [cam.location, cam.ip_address, !cam.is_active && t('tours.cameraPaused')].filter(Boolean).join(' · ')
  const pages = otherPages.join(', ')
  const elsewhere = otherPages.length === 0 ? null
    : t(onPage ? (otherPages.length === 1 ? 'tours.alsoOnPage' : 'tours.alsoOnPages') : (otherPages.length === 1 ? 'tours.onPage' : 'tours.onPages'), { pages })
  return (
    <li ref={drag.setNodeRef} {...drag.listeners} {...drag.attributes} role={undefined} tabIndex={-1}
      className={clsx('flex items-center gap-2 pr-1 cursor-grab active:cursor-grabbing',
        current ? 'bg-[color-mix(in_oklab,var(--accent)_26%,transparent)] shadow-[inset_3px_0_0_var(--accent)]' : onPage ? 'bg-[color-mix(in_oklab,var(--accent)_8%,transparent)] hover:bg-[var(--panel-2)]' : 'hover:bg-[var(--panel-2)]',
        drag.isDragging && 'opacity-40')}
      aria-current={current || undefined}
      data-camera-id={cam.id}
      data-testid="tour-picker-row">
      <label className="flex flex-1 min-w-0 items-center gap-2 pl-2 py-1.5 text-xs cursor-pointer"
        title={choosing ? t('tours.arrangePick', { name: cam.name }) : undefined}
        onClick={(e) => { if (choosing) { e.preventDefault(); onPick() } }}>
        <input type="checkbox" checked={onPage} onChange={onToggle} />
        <CameraThumb cameraId={cam.id} alt={cam.name} className="w-16 aspect-video shrink-0" />
        <span className="min-w-0 flex-1">
          <span className="block truncate">{cam.name}</span>
          {/* The camera itself on the left, where this tour uses it on the right.
              Short of room, the camera's details give way first. */}
          <span className="flex min-w-0 items-center gap-2 text-[10px] leading-4">
            <span className="min-w-0 flex-1 truncate text-[var(--text-dim)]" title={details}>{details}</span>
            <span className="flex min-w-0 max-w-[80%] items-center justify-end gap-1" data-testid="tour-camera-usage">
              {onPage && <span className="shrink-0 px-1 font-medium text-[var(--accent)] bg-[color-mix(in_oklab,var(--accent)_16%,transparent)]">{t('tours.arrangeOnTile', { n: at + 1 })}</span>}
              {elsewhere && <span className="min-w-0 truncate px-1 text-[var(--text)] bg-[color-mix(in_oklab,var(--text)_10%,transparent)]" title={elsewhere} data-testid="tour-camera-elsewhere">{elsewhere}</span>}
              {!onPage && !elsewhere && <span className="shrink-0 px-1 font-medium text-[color-mix(in_oklab,var(--warn)_70%,var(--text))] bg-[color-mix(in_oklab,var(--warn)_14%,transparent)]" data-testid="tour-camera-not-on-tour">{t('tours.notOnTour')}</span>}
            </span>
          </span>
        </span>
      </label>
      <IconAction label={t('tours.watchLive', { name: cam.name })} onClick={onLive}><Play size={12} /></IconAction>
    </li>
  )
}
