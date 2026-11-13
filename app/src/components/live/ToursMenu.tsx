// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, Plus, Route, Search } from 'lucide-react'
import { clsx } from 'clsx'
import { useTranslation } from '../../i18n'
import { apiService } from '../../lib/apiService'
import { useClickOutside } from '../../hooks/useClickOutside'
import { usePermissions } from '../../hooks/usePermissions'
import { resolveLayout, type WindowLayoutSettings } from '../../lib/liveLayouts'
import { buildPages, cycleSeconds, formatDuration } from '../../lib/tourPlan'
import type { Tour } from '../../services/tourService'

export const TOURS_SETTINGS_PATH = '/settings/more-settings/camera-tours'
/** More tours than this and the menu offers a search box. */
const SEARCH_FROM = 6

/**
 * Live View's "Tours" button: start a tour on this screen, or go and set
 * one up. Hidden for someone who has no tour to run and may not make one.
 */
export function ToursMenu({ dropUp = false }: { dropUp?: boolean }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const location = useLocation()
  const { hasPermission } = usePermissions()
  const canManage = hasPermission('cameras.manage')
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useClickOutside(ref, open, () => setOpen(false))

  const tours = useQuery({
    queryKey: ['tours'],
    queryFn: async () => ((await apiService.getTours()).data?.tours ?? []) as Tour[],
    staleTime: 60_000,
  })
  const windowSettings = useQuery({
    queryKey: ['window-settings'],
    queryFn: async () => (await apiService.getWindowSettings()).data as WindowLayoutSettings,
    staleTime: 5 * 60_000,
    enabled: open,
  })

  const [query, setQuery] = useState('')
  const runnable = useMemo(() => (tours.data ?? []).filter((x) => x.enabled), [tours.data])
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return runnable
    return runnable.filter((x) => `${x.name} ${x.description ?? ''}`.toLowerCase().includes(q))
  }, [runnable, query])
  if (!canManage && runnable.length === 0) return null

  // A handful of tours reads at a glance; past that, a search box earns its row.
  const searchable = runnable.length > SEARCH_FROM
  const close = () => { setOpen(false); setQuery('') }
  const run = (tour: Tour) => { close(); navigate(`/live/tour/${tour.id}`) }

  return (
    <div className="relative" ref={ref} onKeyDown={(e) => { if (e.key === 'Escape' && open) { e.stopPropagation(); close() } }}>
      <button
        type="button"
        onClick={() => (open ? close() : setOpen(true))}
        aria-haspopup="menu"
        aria-expanded={open}
        title={t('tours.menu')}
        className={clsx(
          'h-5 px-1.5 inline-flex items-center gap-1 text-[10px] leading-none border',
          open
            ? 'border-[var(--accent)] text-[var(--accent)]'
            : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]',
        )}
      >
        <Route size={12} aria-hidden="true" />
        <span className="hidden sm:inline">{t('tours.menu')}</span>
        <ChevronDown size={11} aria-hidden="true" />
      </button>
      {open && (
        <div
          role="menu"
          className={`absolute right-0 z-50 flex flex-col bg-[var(--panel)] border border-[var(--border)] shadow-lg w-[260px] max-w-[calc(100vw-1rem)] max-h-[min(28rem,70vh)] ${dropUp ? 'bottom-full mb-1' : 'top-full mt-1'}`}
        >
          {searchable && (
            <div className="shrink-0 border-b border-[var(--border)] p-1.5">
              <div className="relative">
                <Search size={12} aria-hidden="true" className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" />
                <input
                  type="search"
                  autoFocus
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => { if (e.key === 'Enter' && shown[0]) run(shown[0]) }}
                  placeholder={t('tours.searchTours')}
                  aria-label={t('tours.searchTours')}
                  data-testid="tours-menu-search"
                  className="w-full h-7 pl-6 pr-2 text-xs bg-[var(--bg)] border border-[var(--border)] text-[var(--text)] placeholder:text-[var(--text-dim)] focus:outline-none focus:border-[var(--accent)]"
                />
              </div>
            </div>
          )}
          <div className="thin-scroll min-h-0 flex-1 overflow-y-auto overscroll-contain py-0.5">
            {runnable.length === 0 ? (
              <div className="px-3 py-2 text-xs text-[var(--text-dim)]">{tours.isPending ? t('tours.loading') : t('tours.noneYet')}</div>
            ) : shown.length === 0 ? (
              <div className="px-3 py-2 text-xs text-[var(--text-dim)]">{t('tours.noToursMatch')}</div>
            ) : (
              shown.map((tour) => {
                // Only the pages this viewer has a camera on: what the wall will show.
                const pages = buildPages(tour.pages, (lid) => resolveLayout(lid, windowSettings.data))
                return (
                  <button
                    key={tour.id}
                    type="button"
                    role="menuitem"
                    title={tour.description || tour.name}
                    className="w-full text-left px-3 py-1.5 hover:bg-[var(--panel-2)] focus-visible:bg-[var(--panel-2)] focus:outline-none"
                    onClick={() => run(tour)}
                  >
                    <div className="text-xs text-[var(--text)] truncate">{tour.name}</div>
                    <div className="text-[10px] text-[var(--text-dim)] font-mono tabular-nums">
                      {t('tours.summary', { pages: pages.length, cycle: formatDuration(cycleSeconds(pages)) })}
                    </div>
                  </button>
                )
              })
            )}
          </div>
          {canManage && (
            <div className="shrink-0 border-t border-[var(--border)] px-3 py-1.5 flex items-center justify-between gap-2">
              <button
                type="button"
                role="menuitem"
                data-testid="tours-menu-new"
                className="inline-flex items-center gap-1 text-xs text-[var(--accent)] hover:underline"
                onClick={() => { close(); navigate(`/tours/new?return=${encodeURIComponent(location.pathname + location.search)}`) }}
              >
                <Plus size={12} aria-hidden="true" /> {t('tours.new')}
              </button>
              <button
                type="button"
                role="menuitem"
                className="text-xs text-[var(--text-dim)] hover:text-[var(--accent)]"
                onClick={() => { close(); navigate(TOURS_SETTINGS_PATH) }}
              >
                ⚙ {t('tours.manage')}
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
