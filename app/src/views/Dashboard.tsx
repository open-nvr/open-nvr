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

import { useCallback, useLayoutEffect, useMemo, useRef, useState, type ComponentType, type CSSProperties } from 'react'
import { useIsFetching, useQueryClient } from '@tanstack/react-query'
import { GridLayout, type EventCallback, type Layout } from 'react-grid-layout'
import 'react-grid-layout/css/styles.css'
import { Check, ChevronDown, LayoutGrid, Plus, RefreshCw, RotateCcw } from 'lucide-react'
import { Button } from '../components/ui'
import { useAuth } from '../auth/AuthContext'
import { useClickOutside } from '../hooks/useClickOutside'
import { useTranslation } from '../i18n'
import { HealthBar } from './dashboard/HealthBar'
import { DRAG_HANDLE_CLASS, NO_DRAG_CLASS } from './dashboard/WidgetFrame'
import {
  ALL_WIDGETS,
  GRID_COLS,
  GRID_ROWS,
  WIDGET_LIMITS,
  swapOnDrop,
  useDashboardLayout,
  type WidgetId,
  type WidgetPlacement,
} from './dashboard/layout'
import { VitalsWidget } from './dashboard/widgets/VitalsWidget'
import { LiveWallWidget } from './dashboard/widgets/LiveWallWidget'
import { AlarmsWidget } from './dashboard/widgets/AlarmsWidget'
import { CameraStatusWidget } from './dashboard/widgets/CameraStatusWidget'
import { ResourceTrendWidget } from './dashboard/widgets/ResourceTrendWidget'
import { FootageWidget } from './dashboard/widgets/FootageWidget'
import { NetworkIdsWidget } from './dashboard/widgets/NetworkIdsWidget'

type WidgetProps = { editing?: boolean; onRemove?: () => void }

const WIDGETS: Record<WidgetId, ComponentType<WidgetProps>> = {
  vitals: VitalsWidget,
  live: LiveWallWidget,
  alarms: AlarmsWidget,
  cameras: CameraStatusWidget,
  resources: ResourceTrendWidget,
  footage: FootageWidget,
  ids: NetworkIdsWidget,
}

/** Gap between widgets; halved on short screens so rows keep usable height. */
const GAP = 8
const GAP_TIGHT = 4
const TIGHT_BELOW = 760
/** Below this width the grid gives way to a single scrolling column. */
const STACK_BELOW = 768
/** Row height when stacked, or when the viewport is too short to fit. */
const MIN_ROW = 10
const MAX_ROW = 64
/** The shell's <main> bottom padding, which the grid must leave clear. */
const MAIN_PAD_BOTTOM = 16

/**
 * Measure the grid's width, and the height left between its top edge and
 * the bottom of the viewport. The row height is derived from the latter so
 * that GRID_ROWS rows — the default layout — exactly fill the screen.
 */
function useGridMetrics() {
  const ref = useRef<HTMLDivElement>(null)
  const [m, setM] = useState({ width: 0, avail: 0 })
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => {
      const rect = el.getBoundingClientRect()
      const top = rect.top + window.scrollY
      setM((prev) => {
        const next = { width: Math.floor(rect.width), avail: Math.floor(document.documentElement.clientHeight - top - MAIN_PAD_BOTTOM) }
        return prev.width === next.width && prev.avail === next.avail ? prev : next
      })
    }
    measure()
    // The body observer catches content above the grid changing height
    // (the system alert banner appearing), which moves the grid's top edge.
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    ro.observe(document.body)
    window.addEventListener('resize', measure)
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', measure)
    }
  }, [])
  return { ref, ...m }
}

/** Order-independent identity of a layout, for recognising one the grid reports again. */
function layoutKey(l: Layout): string {
  return JSON.stringify([...l].map(({ i, x, y, w, h }) => [i, x, y, w, h]).sort())
}

function AddWidgetMenu({ hidden, onAdd }: { hidden: WidgetId[]; onAdd: (id: WidgetId) => void }) {
  const { t } = useTranslation()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useClickOutside(ref, open, () => setOpen(false))
  return (
    <div ref={ref} className="relative">
      <Button size="sm" variant="outline" onClick={() => setOpen((o) => !o)} disabled={!hidden.length} aria-expanded={open}>
        <Plus size={13} /> {t('dashboard.addWidget')} <ChevronDown size={12} />
      </Button>
      {open && (
        <div className="absolute right-0 top-full mt-1 z-50 w-56 border border-[var(--border)] bg-[var(--panel)] shadow-xl py-1">
          {hidden.map((id) => (
            <button
              key={id}
              type="button"
              className="w-full text-left px-3 py-1.5 hover:bg-[var(--panel-2)]"
              onClick={() => { onAdd(id); setOpen(false) }}
            >
              <div className="text-[12px] text-[var(--text)]">{t(`dashboard.w.${id}`)}</div>
              <div className="text-[10.5px] text-[var(--text-dim)]">{t(`dashboard.wd.${id}`)}</div>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function Dashboard() {
  const { t } = useTranslation()
  const { user } = useAuth()
  const qc = useQueryClient()
  const fetching = useIsFetching() > 0
  const { layout, setLayout, add, remove, reset, customised } = useDashboardLayout(user?.id)
  const [editing, setEditing] = useState(false)
  const { ref, width, avail } = useGridMetrics()

  const stacked = width > 0 && width < STACK_BELOW
  const gap = avail > 0 && avail < TIGHT_BELOW ? GAP_TIGHT : GAP
  const rowHeight = stacked
    ? 30
    : Math.max(MIN_ROW, Math.min(MAX_ROW, Math.floor((avail - gap * (GRID_ROWS - 1)) / GRID_ROWS)))

  const gridLayout = useMemo<Layout>(
    () => layout.map((p) => ({ ...p, ...WIDGET_LIMITS[p.i] })),
    [layout],
  )
  const hidden = ALL_WIDGETS.filter((id) => !layout.some((p) => p.i === id))

  // When a drop becomes a swap, the grid still reports its own push-down
  // result for that drop — twice (once from the drop handler, once from an
  // effect after it re-renders). Saving either would undo the swap, and
  // answering it with the swap again ping-pongs forever, so that exact
  // layout is ignored until the next drag begins.
  const ignoreLayout = useRef<string | null>(null)

  const onDragStart = useCallback<EventCallback>(() => {
    ignoreLayout.current = null
  }, [])

  const onDragStop = useCallback<EventCallback>((pushed, oldItem, newItem) => {
    if (!oldItem || !newItem) return
    const next = swapOnDrop(layout, oldItem.i as WidgetId, newItem)
    if (next) {
      ignoreLayout.current = layoutKey(pushed)
      setLayout(next)
    }
  }, [layout, setLayout])

  const onLayoutChange = useCallback((next: Layout) => {
    if (!editing) return
    if (ignoreLayout.current && layoutKey(next) === ignoreLayout.current) return
    const placed: WidgetPlacement[] = next.map(({ i, x, y, w, h }) => ({ i: i as WidgetId, x, y, w, h }))
    const same = placed.length === layout.length && placed.every((p) => {
      const o = layout.find((l) => l.i === p.i)
      return o && o.x === p.x && o.y === p.y && o.w === p.w && o.h === p.h
    })
    if (!same) setLayout(placed)
  }, [editing, layout, setLayout])

  const refresh = () => qc.invalidateQueries()

  const actions = editing ? (
    <>
      <AddWidgetMenu hidden={hidden} onAdd={add} />
      <Button size="sm" variant="ghost" onClick={reset} disabled={!customised} title={t('dashboard.resetLayoutHint')}>
        <RotateCcw size={13} /> {t('dashboard.resetLayout')}
      </Button>
      <Button size="sm" variant="primary" onClick={() => setEditing(false)}>
        <Check size={13} /> {t('dashboard.done')}
      </Button>
    </>
  ) : (
    <>
      {!stacked && (
        <Button size="sm" variant="ghost" onClick={() => setEditing(true)} title={t('dashboard.customiseHint')}>
          <LayoutGrid size={13} /> <span className="hidden xl:inline">{t('dashboard.customise')}</span>
        </Button>
      )}
      <Button size="sm" variant="ghost" onClick={refresh} title={t('common.refresh')} aria-label={t('common.refresh')}>
        <RefreshCw size={13} className={fetching ? 'animate-spin' : ''} />
      </Button>
    </>
  )

  const ordered = [...layout].sort((a, b) => a.y - b.y || a.x - b.x)

  return (
    <section className="flex flex-col gap-2">
      <h1 className="sr-only">{t('dashboard.title')}</h1>
      <HealthBar actions={actions} />

      {/* Floating, so entering edit mode does not shift the grid it explains. */}
      {editing && (
        <div className="fixed bottom-4 left-1/2 -translate-x-1/2 z-40 px-3 py-1.5 text-[11px] text-[var(--text)] bg-[var(--panel)] border border-[var(--accent)] shadow-xl pointer-events-none">
          {t('dashboard.editHint')}
        </div>
      )}

      <div
        ref={ref}
        className={editing ? 'dash-grid dash-editing' : 'dash-grid'}
        // Slimmer widget title bars on a short screen, where every pixel of
        // row height goes to content.
        style={{ '--dash-head': gap === GAP_TIGHT ? '1.5rem' : '2rem' } as CSSProperties}
      >
        {width === 0 ? null : stacked ? (
          <div className="flex flex-col gap-2">
            {ordered.map((p) => {
              const W = WIDGETS[p.i]
              return (
                <div key={p.i} style={{ height: Math.max(p.i === 'live' ? 260 : 180, p.h * rowHeight) }}>
                  <W />
                </div>
              )
            })}
          </div>
        ) : (
          <GridLayout
            width={width}
            layout={gridLayout}
            gridConfig={{ cols: GRID_COLS, rowHeight, margin: [gap, gap], containerPadding: [0, 0] }}
            dragConfig={{ enabled: editing, handle: `.${DRAG_HANDLE_CLASS}`, cancel: `.${NO_DRAG_CLASS}` }}
            resizeConfig={{ enabled: editing, handles: ['se'] }}
            onLayoutChange={onLayoutChange}
            onDragStart={onDragStart}
            onDragStop={onDragStop}
          >
            {layout.map((p) => {
              const W = WIDGETS[p.i]
              return (
                <div key={p.i}>
                  <W editing={editing} onRemove={() => remove(p.i)} />
                </div>
              )
            })}
          </GridLayout>
        )}
      </div>
    </section>
  )
}
