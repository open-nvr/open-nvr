// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// "On the picture": what a camera's live overlay draws, each layer with a
// checkbox, all / none per group, and Show all / Hide all. One component
// for every place a camera plays live (Live View tiles, the dashboard
// wall, tours, the peek dialog, app pages). Each view keeps its own choice
// per camera (lib/overlayLayerStore's scope), remembered in the browser.
//
// The menu renders in a portal, fixed to the button: video tiles clip
// their overflow, and a menu cut off at the tile's edge is no menu.

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Layers } from 'lucide-react'
import { clsx } from 'clsx'
import type { OverlayLayer } from '../../lib/overlayLayers'
import {
  setLayerHidden, setLayersHidden, showAllLayers, useOverlayLayers, type LayerScope,
} from '../../lib/overlayLayerStore'

const MENU_W = 248

export function OverlayLayersButton({ cameraId, scope, variant = 'panel', className }: {
  cameraId: number | null | undefined
  /** Which view this is ('live', 'dashboard', 'tour', 'peek', 'app:<id>'):
   *  each keeps its own choice per camera. Must match the player's
   *  overlayScope in the same view. */
  scope: LayerScope
  /** 'panel': a dim icon in a card's header. 'video': the dark translucent
   *  square the tiles' other on-video controls use. */
  variant?: 'panel' | 'video'
  className?: string
}) {
  const { layers, hidden } = useOverlayLayers(cameraId, scope)
  const [open, setOpen] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const [at, setAt] = useState<{ left: number; top?: number; bottom?: number } | null>(null)

  // Where the menu goes: under the button, or above it when the button
  // sits low on the screen; kept inside the window either way.
  const place = () => {
    const r = btnRef.current?.getBoundingClientRect()
    if (!r) return
    const left = Math.max(8, Math.min(window.innerWidth - MENU_W - 8, r.right - MENU_W))
    setAt(r.bottom > window.innerHeight * 0.55
      ? { left, bottom: window.innerHeight - r.top + 4 }
      : { left, top: r.bottom + 4 })
  }
  useLayoutEffect(() => { if (open) place() }, [open])
  useEffect(() => {
    if (!open) return
    const outside = (e: PointerEvent) => {
      const t = e.target as Node
      if (!btnRef.current?.contains(t) && !menuRef.current?.contains(t)) setOpen(false)
    }
    const key = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('pointerdown', outside, true)
    window.addEventListener('keydown', key)
    window.addEventListener('resize', place)
    window.addEventListener('scroll', place, true)
    return () => {
      window.removeEventListener('pointerdown', outside, true)
      window.removeEventListener('keydown', key)
      window.removeEventListener('resize', place)
      window.removeEventListener('scroll', place, true)
    }
  }, [open])

  if (cameraId == null || (layers.length === 0 && hidden.size === 0)) return null
  const off = layers.filter((l) => hidden.has(l.key)).length
  const groups = [...new Set(layers.map((l) => l.group))]
  const link = 'text-[var(--accent)] hover:underline disabled:opacity-40 disabled:no-underline disabled:cursor-default'

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        // Tiles start a drag on pointer-down; a click here is not a drag.
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v) }}
        aria-label="Choose what is drawn on the picture"
        aria-expanded={open}
        title={off ? `Choose what is drawn on the picture (${off} off)` : 'Choose what is drawn on the picture'}
        className={clsx(
          'flex items-center gap-1',
          variant === 'video'
            ? 'p-1.5 bg-black/60 hover:bg-black/80 text-white/90 border border-white/10 text-[10px]'
            : clsx('rounded px-1 py-0.5 text-[11px] hover:bg-[var(--panel)] hover:text-[var(--text)]',
                   off ? 'text-[var(--accent)]' : 'text-[var(--text-dim)]'),
          className,
        )}
      >
        <Layers size={variant === 'video' ? 12 : 13} />
        {off > 0 && <span>{off} off</span>}
      </button>
      {open && at && createPortal(
        <div
          ref={menuRef}
          role="dialog"
          aria-label="On the picture"
          onPointerDown={(e) => e.stopPropagation()}
          style={{ position: 'fixed', width: MENU_W, ...at }}
          className="z-[1000] max-h-[60vh] overflow-y-auto border border-[var(--border)] bg-[var(--panel-2)] p-2 text-xs text-[var(--text)] shadow-xl"
        >
          <div className="mb-1 flex items-center gap-2">
            <span className="font-semibold">On the picture</span>
            <span className="ml-auto flex gap-2">
              <button type="button" className={link} disabled={hidden.size === 0}
                      onClick={() => showAllLayers(cameraId, scope)}>Show all</button>
              <button type="button" className={link} disabled={layers.length > 0 && off === layers.length}
                      onClick={() => setLayersHidden(cameraId, scope, layers.map((l) => l.key), true)}>Hide all</button>
            </span>
          </div>
          {layers.length === 0 ? (
            <p className="text-[var(--text-dim)]">
              Nothing is being drawn right now.{hidden.size > 0 ? ' Some layers are switched off.' : ''}
            </p>
          ) : groups.map((group) => {
            const own: OverlayLayer[] = layers.filter((l) => l.group === group)
            const ownOff = own.filter((l) => hidden.has(l.key)).length
            return (
              <div key={group} className="mt-1.5">
                <div className="flex items-center text-[10px] uppercase tracking-wide text-[var(--text-dim)]">
                  <span className="truncate">{group}</span>
                  {own.length > 1 && (
                    <span className="ml-auto flex gap-1.5 normal-case tracking-normal">
                      <button type="button" className={link} disabled={ownOff === 0}
                              onClick={() => setLayersHidden(cameraId, scope, own.map((l) => l.key), false)}>all</button>
                      <span aria-hidden="true">·</span>
                      <button type="button" className={link} disabled={ownOff === own.length}
                              onClick={() => setLayersHidden(cameraId, scope, own.map((l) => l.key), true)}>none</button>
                    </span>
                  )}
                </div>
                {own.map((l) => (
                  <label key={l.key} className="flex cursor-pointer items-center gap-2 py-0.5">
                    <input type="checkbox" checked={!hidden.has(l.key)}
                           onChange={(e) => setLayerHidden(cameraId, scope, l.key, !e.target.checked)} />
                    <span className="truncate" title={l.name}>{l.name}</span>
                  </label>
                ))}
              </div>
            )
          })}
        </div>,
        document.body,
      )}
    </>
  )
}
