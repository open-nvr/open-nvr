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

/**
 * The PTZ pad: hold a direction (pointer, touch or arrow key) to move,
 * release to stop. Shared by the Live View tile overlay and the camera's
 * device settings.
 *
 * Commands to one camera are sent one after another, each waiting for the
 * previous answer. A quick tap used to send its stop while the move was
 * still on its way, and the camera could get the stop first and keep
 * turning. Pointer capture keeps the release with the button that was
 * pressed, so sliding off it still stops the camera, and merely moving the
 * mouse across the pad sends nothing (it used to send a stop per button).
 *
 * Keyboard, anywhere inside ``keyScope`` (the Live View tile; the pad itself
 * in settings), never page-wide: arrows move and combine into diagonals,
 * + / − zoom, Shift moves fast, 1–9 recall presets, Home the Home preset,
 * Esc closes the pad.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode, RefObject } from 'react'
import {
  ArrowDown, ArrowDownLeft, ArrowDownRight, ArrowLeft, ArrowRight, ArrowUp,
  ArrowUpLeft, ArrowUpRight, Minus, Plus, Square, X,
} from 'lucide-react'
import { clsx } from 'clsx'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useTranslation } from '../i18n'
import { DIR_KEYS, buttonFor, digitOf, heldVector, homePreset, presetForDigit } from '../lib/ptzKeys'
import type { Preset, Vec } from '../lib/ptzKeys'

const SPEEDS = [0.2, 0.5, 1] as const
const FAST = SPEEDS.length - 1
const SPEED_KEY = 'opennvr.ptzSpeed'

function loadSpeed(): number {
  try {
    const raw = localStorage.getItem(SPEED_KEY)
    const i = raw === null ? NaN : Number(raw)
    return Number.isInteger(i) && i >= 0 && i < SPEEDS.length ? i : 1
  } catch {
    return 1
  }
}

function saveSpeed(i: number) {
  try {
    localStorage.setItem(SPEED_KEY, String(i))
  } catch {
    /* a convenience only */
  }
}

// ── per-camera command queue ───────────────────────────────────────────

type Cmd = { kind: 'move'; vec: Vec } | { kind: 'stop' } | { kind: 'goto'; token: string }

const queues = new Map<number, { tail: Promise<void>; last: Cmd['kind'] | null }>()

/** Send ``cmd`` after every command already queued for this camera has been
 *  answered. A stop queued straight after another stop is dropped. */
function send(cameraId: number, cmd: Cmd, onError: (e: unknown) => void, force = false) {
  const q = queues.get(cameraId) ?? { tail: Promise.resolve(), last: null }
  if (cmd.kind === 'stop' && q.last === 'stop' && !force) return
  q.last = cmd.kind
  q.tail = q.tail.then(async () => {
    try {
      if (cmd.kind === 'move') await apiService.ptzMove(cameraId, cmd.vec[0], cmd.vec[1], cmd.vec[2])
      else if (cmd.kind === 'goto') await apiService.ptzGotoPreset(cameraId, cmd.token)
      else await apiService.ptzStop(cameraId)
    } catch (e) {
      onError(e)
    }
  })
  queues.set(cameraId, q)
}

// ── presets, for the 1–9 and Home keys ─────────────────────────────────

/** Presets per camera, kept a minute so a run of key presses asks once;
 *  a press while the list is still loading waits for the same request. */
const presetCache = new Map<number, { at: number; presets: Promise<Preset[]> }>()

function loadPresets(cameraId: number): Promise<Preset[]> {
  const hit = presetCache.get(cameraId)
  if (hit && Date.now() - hit.at < 60_000) return hit.presets
  const presets = apiService.ptzPresets(cameraId).then(
    ({ data }) => (data as { presets?: Preset[] } | undefined)?.presets ?? [])
  presetCache.set(cameraId, { at: Date.now(), presets })
  // A failed load is not remembered: the next press asks again.
  presets.catch(() => { if (presetCache.get(cameraId)?.presets === presets) presetCache.delete(cameraId) })
  return presets
}

// ── keys ───────────────────────────────────────────────────────────────

function isTyping(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null
  return !!el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))
}

// ── the pad ─────────────────────────────────────────────────────────────

export function PtzPad({
  cameraId,
  variant = 'overlay',
  onClose,
  onUnsupported,
  onMovingChange,
  keyScope,
}: {
  cameraId: number
  /** 'overlay' sits on a live tile; 'panel' fills a settings page. */
  variant?: 'overlay' | 'panel'
  onClose?: () => void
  /** The camera answered that it has no PTZ. */
  onUnsupported?: () => void
  /** True while a press or held key is moving the camera. */
  onMovingChange?: (moving: boolean) => void
  /** Where the keyboard drives the camera. Defaults to the pad itself. */
  keyScope?: RefObject<HTMLElement | null>
}) {
  const { t } = useTranslation()
  const [speed, setSpeed] = useState(loadSpeed)
  const [active, setActive] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [unsupported, setUnsupported] = useState(false)
  const activeRef = useRef<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const msgTimer = useRef<number | undefined>(undefined)
  const heldRef = useRef<Set<string>>(new Set())
  const shiftRef = useRef(false)
  // Callbacks from the parent change on its every render; read through a
  // ref so the effects below never re-run (and stop the camera) because of it.
  const onUnsupportedRef = useRef(onUnsupported)
  onUnsupportedRef.current = onUnsupported
  const onMovingRef = useRef(onMovingChange)
  onMovingRef.current = onMovingChange
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose
  const tRef = useRef(t)
  tRef.current = t
  const speedRef = useRef(speed)
  speedRef.current = speed
  const unsupportedRef = useRef(unsupported)
  unsupportedRef.current = unsupported

  const say = useCallback((kind: 'error' | 'note', msg: string) => {
    setError(kind === 'error' ? msg : null)
    setNote(kind === 'note' ? msg : null)
    window.clearTimeout(msgTimer.current)
    msgTimer.current = window.setTimeout(() => { setError(null); setNote(null) }, kind === 'error' ? 5000 : 2500)
  }, [])

  const onError = useCallback((err: unknown) => {
    const e = err as { status?: number; response?: { status?: number } }
    if ((e?.status ?? e?.response?.status) === 409) {
      setUnsupported(true)
      onUnsupportedRef.current?.()
      return
    }
    const fallback = tRef.current('ptz.failed')
    const msg = extractApiError(err, fallback)
    say('error', msg.length < 120 ? msg : fallback)
  }, [say])

  const setActiveId = useCallback((id: string | null) => {
    const was = activeRef.current
    activeRef.current = id
    setActive(id)
    if ((was === null) !== (id === null)) onMovingRef.current?.(id !== null)
  }, [])

  /** Move along ``vec`` as press ``id``; Shift (or ``fast``) means full speed. */
  const start = useCallback((id: string, vec: Vec, fast = false) => {
    if (unsupportedRef.current) return
    const s = SPEEDS[fast || shiftRef.current ? FAST : speedRef.current]
    setActiveId(id)
    send(cameraId, { kind: 'move', vec: [vec[0] * s, vec[1] * s, vec[2] * s] }, onError)
  }, [cameraId, onError, setActiveId])

  const stop = useCallback((id?: string) => {
    const cur = activeRef.current
    if (cur === null || (id !== undefined && cur !== id)) return
    setActiveId(null)
    send(cameraId, { kind: 'stop' }, onError)
  }, [cameraId, onError, setActiveId])

  /** Re-send the held keys' direction: a key pressed or released, or Shift. */
  const steerByKeys = useCallback(() => {
    const v = heldVector(heldRef.current)
    if (v[0] === 0 && v[1] === 0 && v[2] === 0) {
      if (activeRef.current?.startsWith('key:')) stop()
      return
    }
    start(`key:${buttonFor(v) ?? v.join(',')}`, v)
  }, [start, stop])

  const gotoPreset = useCallback(async (which: number | 'home') => {
    if (unsupportedRef.current) return
    let presets: Preset[]
    try {
      presets = await loadPresets(cameraId)
    } catch (e) {
      onError(e)
      return
    }
    const p = which === 'home' ? homePreset(presets) : presetForDigit(presets, which)
    const tt = tRef.current
    if (!p) {
      say('error', which === 'home' ? tt('ptz.noHomePreset') : tt('ptz.noPreset', { n: which }))
      return
    }
    if (activeRef.current !== null) setActiveId(null)
    heldRef.current.clear()
    send(cameraId, { kind: 'goto', token: p.token }, onError)
    say('note', tt('ptz.goingToPreset', { name: p.name }))
  }, [cameraId, onError, say, setActiveId])

  // Never leave the camera moving: closing the pad, switching tabs or
  // windows mid-press all stop it.
  useEffect(() => {
    const halt = () => { heldRef.current.clear(); shiftRef.current = false; stop() }
    window.addEventListener('blur', halt)
    document.addEventListener('visibilitychange', halt)
    return () => {
      window.removeEventListener('blur', halt)
      document.removeEventListener('visibilitychange', halt)
      halt()
      window.clearTimeout(msgTimer.current)
    }
  }, [stop])

  // The overlay takes the keyboard as soon as it opens.
  useEffect(() => {
    if (variant === 'overlay') rootRef.current?.focus({ preventScroll: true })
  }, [variant])

  // The keyboard, anywhere in the scope. Native listeners on the scope run
  // before the player's own window-level keys (arrows = volume / seek), and
  // stopping the event here keeps those from also firing while PTZ is open.
  useEffect(() => {
    const scope = keyScope?.current ?? rootRef.current
    if (!scope) return
    const down = (e: KeyboardEvent) => {
      if (isTyping(e.target) || e.ctrlKey || e.altKey || e.metaKey) return
      if (e.key === 'Shift') {
        if (!shiftRef.current) {
          shiftRef.current = true
          if (activeRef.current?.startsWith('key:')) steerByKeys()
        }
        return
      }
      if (e.key === 'Escape' && onCloseRef.current) {
        e.preventDefault(); e.stopPropagation()
        onCloseRef.current()
        return
      }
      const digit = digitOf(e.code)
      if (digit !== null || e.code === 'Home') {
        e.preventDefault(); e.stopPropagation()
        if (!e.repeat) void gotoPreset(digit ?? 'home')
        return
      }
      if (!DIR_KEYS[e.code]) return
      e.preventDefault(); e.stopPropagation()
      if (e.repeat || heldRef.current.has(e.code)) return
      heldRef.current.add(e.code)
      steerByKeys()
    }
    const up = (e: KeyboardEvent) => {
      if (e.key === 'Shift') {
        shiftRef.current = false
        if (activeRef.current?.startsWith('key:')) steerByKeys()
        return
      }
      if (!heldRef.current.has(e.code)) return
      e.preventDefault(); e.stopPropagation()
      heldRef.current.delete(e.code)
      steerByKeys()
    }
    // Focus leaving the scope takes the keyboard with it: stop.
    const out = (e: FocusEvent) => {
      if (scope.contains(e.relatedTarget as Node | null)) return
      heldRef.current.clear()
      shiftRef.current = false
      if (activeRef.current?.startsWith('key:')) stop()
    }
    scope.addEventListener('keydown', down)
    scope.addEventListener('keyup', up)
    scope.addEventListener('focusout', out)
    return () => {
      scope.removeEventListener('keydown', down)
      scope.removeEventListener('keyup', up)
      scope.removeEventListener('focusout', out)
    }
  }, [keyScope, steerByKeys, gotoPreset, stop])

  const big = variant === 'panel'
  const cell = big ? 'h-12 w-12' : 'h-8 w-8'
  const icon = big ? 18 : 15
  const lit = active?.startsWith('key:') ? active.slice(4) : active

  // A render helper, not a component: a component declared here would be a
  // new type on every render, remounting the button mid-press and dropping
  // its pointer capture.
  const btn = (id: string, vec: Vec, label: string, children: ReactNode) => (
    <button
      key={id}
      type="button"
      aria-label={label}
      title={label}
      disabled={unsupported}
      className={clsx(
        cell,
        'flex items-center justify-center border border-[var(--border)] select-none touch-none',
        'disabled:opacity-40',
        lit === id
          ? 'bg-[var(--accent)] text-white'
          : 'bg-[var(--panel-2)] text-[var(--text)] hover:bg-[var(--accent)]/30',
      )}
      onPointerDown={(e: ReactPointerEvent<HTMLButtonElement>) => {
        if (e.button !== 0) return
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        start(id, vec, e.shiftKey)
      }}
      onPointerUp={() => stop(id)}
      onPointerCancel={() => stop(id)}
      onLostPointerCapture={() => stop(id)}
      onContextMenu={(e) => e.preventDefault()}
    >
      {children}
    </button>
  )

  return (
    <div
      ref={rootRef}
      tabIndex={0}
      role="group"
      aria-label={t('video.ptz')}
      aria-keyshortcuts="ArrowUp ArrowDown ArrowLeft ArrowRight Shift + - 1 2 3 4 5 6 7 8 9 Home Escape"
      title={big ? undefined : t('ptz.keys')}
      className={clsx(
        'outline-none text-[var(--text)]',
        variant === 'overlay'
          ? clsx(
              'bg-black/80 border border-[var(--border)] p-2 focus-visible:ring-1 focus-visible:ring-[var(--accent)]',
              // Out of the picture's way until it is used: faded unless the
              // mouse is on it, the camera is moving, or it has news.
              'transition-opacity duration-200 hover:opacity-100',
              active || error || note || unsupported ? 'opacity-100' : 'opacity-40',
            )
          : 'inline-block',
      )}
    >
      <div className="flex items-start gap-2">
        <div className="grid grid-cols-3 gap-1">
          {btn('ul', [-1, 1, 0], t('ptz.upLeft'), <ArrowUpLeft size={icon} />)}
          {btn('u', [0, 1, 0], t('ptz.up'), <ArrowUp size={icon} />)}
          {btn('ur', [1, 1, 0], t('ptz.upRight'), <ArrowUpRight size={icon} />)}
          {btn('l', [-1, 0, 0], t('ptz.left'), <ArrowLeft size={icon} />)}
          <button
            type="button"
            aria-label={t('ptz.stop')}
            title={t('ptz.stop')}
            disabled={unsupported}
            className={clsx(cell, 'flex items-center justify-center border border-[var(--border)] bg-[var(--panel-2)] hover:bg-[var(--panel)] disabled:opacity-40')}
            onClick={() => {
              heldRef.current.clear()
              setActiveId(null)
              send(cameraId, { kind: 'stop' }, onError, true)
            }}
          >
            <Square size={icon - 3} />
          </button>
          {btn('r', [1, 0, 0], t('ptz.right'), <ArrowRight size={icon} />)}
          {btn('dl', [-1, -1, 0], t('ptz.downLeft'), <ArrowDownLeft size={icon} />)}
          {btn('d', [0, -1, 0], t('ptz.down'), <ArrowDown size={icon} />)}
          {btn('dr', [1, -1, 0], t('ptz.downRight'), <ArrowDownRight size={icon} />)}
        </div>
        <div className="flex flex-col gap-1">
          {btn('zi', [0, 0, 1], t('ptz.zoomIn'), <Plus size={icon} />)}
          {btn('zo', [0, 0, -1], t('ptz.zoomOut'), <Minus size={icon} />)}
          {onClose && (
            <button
              type="button"
              aria-label={t('ptz.close')}
              title={t('ptz.close')}
              className={clsx(cell, 'flex items-center justify-center border border-[var(--border)] bg-[var(--panel-2)] hover:bg-[color-mix(in_oklab,var(--danger)_30%,transparent)]')}
              onClick={onClose}
            >
              <X size={icon} />
            </button>
          )}
        </div>
      </div>
      {/* Speed: as wide as the buttons above and never wider. `w-0 min-w-full`
          keeps the row out of the pad's own width, so a longer label or
          translation can't push it past the pad's edge; each button takes its
          word's width plus a share of the rest. The visible "Speed"
          label is gone for room; the group and each button still say it. */}
      <div
        role="group"
        aria-label={t('ptz.speed')}
        className={clsx('mt-2 flex w-0 min-w-full gap-1', big ? 'text-xs' : 'text-[10px]')}
      >
        {SPEEDS.map((_, i) => {
          const label = t(['ptz.speedSlow', 'ptz.speedMedium', 'ptz.speedFast'][i])
          return (
            <button
              key={i}
              type="button"
              aria-pressed={speed === i}
              title={`${t('ptz.speed')}: ${label}`}
              className={clsx(
                'min-w-0 flex-auto truncate px-1 py-0.5 border border-[var(--border)]',
                speed === i ? 'bg-[var(--accent)] text-white' : 'bg-[var(--panel-2)] hover:bg-[var(--panel)]',
              )}
              onClick={() => { setSpeed(i); saveSpeed(i) }}
            >
              {label}
            </button>
          )
        })}
      </div>
      {(unsupported || error || note) && (
        <div
          role="status"
          className={clsx(
            'mt-1 w-0 min-w-full',
            big ? 'text-xs' : 'text-[10px]',
            unsupported || error ? 'text-[var(--danger)]' : 'text-[var(--text-dim)]',
          )}
        >
          {unsupported ? t('ptz.noPtz') : error ?? note}
        </div>
      )}
      {big && <p className="mt-3 text-xs text-[var(--text-dim)]">{t('ptz.hint')}</p>}
    </div>
  )
}
