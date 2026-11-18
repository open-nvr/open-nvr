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
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import {
  ArrowDown, ArrowDownLeft, ArrowDownRight, ArrowLeft, ArrowRight, ArrowUp,
  ArrowUpLeft, ArrowUpRight, Minus, Plus, Square, X,
} from 'lucide-react'
import { clsx } from 'clsx'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useTranslation } from '../i18n'

/** Pan, tilt, zoom direction, each -1, 0 or 1; scaled by the speed. */
type Vec = readonly [number, number, number]

const SPEEDS = [0.2, 0.5, 1] as const
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

type Cmd = { kind: 'move'; vec: Vec } | { kind: 'stop' }

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
      else await apiService.ptzStop(cameraId)
    } catch (e) {
      onError(e)
    }
  })
  queues.set(cameraId, q)
}

// ── the pad ─────────────────────────────────────────────────────────────

const KEYS: Record<string, Vec> = {
  ArrowUp: [0, 1, 0],
  ArrowDown: [0, -1, 0],
  ArrowLeft: [-1, 0, 0],
  ArrowRight: [1, 0, 0],
  '+': [0, 0, 1],
  '=': [0, 0, 1],
  '-': [0, 0, -1],
}

export function PtzPad({
  cameraId,
  variant = 'overlay',
  onClose,
  onUnsupported,
}: {
  cameraId: number
  /** 'overlay' sits on a live tile; 'panel' fills a settings page. */
  variant?: 'overlay' | 'panel'
  onClose?: () => void
  /** The camera answered that it has no PTZ. */
  onUnsupported?: () => void
}) {
  const { t } = useTranslation()
  const [speed, setSpeed] = useState(loadSpeed)
  const [active, setActive] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [unsupported, setUnsupported] = useState(false)
  const activeRef = useRef<string | null>(null)
  const rootRef = useRef<HTMLDivElement>(null)
  const errorTimer = useRef<number | undefined>(undefined)
  // Callbacks from the parent change on its every render; read through a
  // ref so the effects below never re-run (and stop the camera) because of it.
  const onUnsupportedRef = useRef(onUnsupported)
  onUnsupportedRef.current = onUnsupported
  const tRef = useRef(t)
  tRef.current = t

  const onError = useCallback((err: unknown) => {
    const e = err as { status?: number; response?: { status?: number } }
    if ((e?.status ?? e?.response?.status) === 409) {
      setUnsupported(true)
      onUnsupportedRef.current?.()
      return
    }
    const fallback = tRef.current('ptz.failed')
    const msg = extractApiError(err, fallback)
    setError(msg.length < 120 ? msg : fallback)
    window.clearTimeout(errorTimer.current)
    errorTimer.current = window.setTimeout(() => setError(null), 5000)
  }, [])

  const start = (id: string, vec: Vec) => {
    if (unsupported) return
    const s = SPEEDS[speed]
    activeRef.current = id
    setActive(id)
    send(cameraId, { kind: 'move', vec: [vec[0] * s, vec[1] * s, vec[2] * s] }, onError)
  }

  const stop = useCallback((id?: string) => {
    if (activeRef.current === null || (id !== undefined && activeRef.current !== id)) return
    activeRef.current = null
    setActive(null)
    send(cameraId, { kind: 'stop' }, onError)
  }, [cameraId, onError])

  // Never leave the camera moving: closing the pad, switching tabs or
  // windows mid-press all stop it.
  useEffect(() => {
    const halt = () => stop()
    window.addEventListener('blur', halt)
    document.addEventListener('visibilitychange', halt)
    return () => {
      window.removeEventListener('blur', halt)
      document.removeEventListener('visibilitychange', halt)
      halt()
      window.clearTimeout(errorTimer.current)
    }
  }, [stop])

  // The overlay takes the keyboard as soon as it opens.
  useEffect(() => {
    if (variant === 'overlay') rootRef.current?.focus({ preventScroll: true })
  }, [variant])

  const onKeyDown = (e: ReactKeyboardEvent) => {
    if (e.key === 'Escape' && onClose) {
      e.stopPropagation()
      onClose()
      return
    }
    const vec = KEYS[e.key]
    if (!vec) return
    e.preventDefault()
    e.stopPropagation()
    if (e.repeat || activeRef.current === `key:${e.key}`) return
    start(`key:${e.key}`, vec)
  }
  const onKeyUp = (e: ReactKeyboardEvent) => {
    if (!KEYS[e.key]) return
    e.preventDefault()
    e.stopPropagation()
    stop(`key:${e.key}`)
  }

  const big = variant === 'panel'
  const cell = big ? 'h-12 w-12' : 'h-8 w-8'
  const icon = big ? 18 : 15

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
        active === id
          ? 'bg-[var(--accent)] text-white'
          : 'bg-[var(--panel-2)] text-[var(--text)] hover:bg-[var(--accent)]/30',
      )}
      onPointerDown={(e: ReactPointerEvent<HTMLButtonElement>) => {
        if (e.button !== 0) return
        e.preventDefault()
        e.currentTarget.setPointerCapture(e.pointerId)
        start(id, vec)
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
      onKeyDown={onKeyDown}
      onKeyUp={onKeyUp}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          const k = activeRef.current
          if (k?.startsWith('key:')) stop(k)
        }
      }}
      className={clsx(
        'outline-none text-[var(--text)]',
        variant === 'overlay'
          ? 'bg-black/80 border border-[var(--border)] p-2 focus-visible:ring-1 focus-visible:ring-[var(--accent)]'
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
              activeRef.current = null
              setActive(null)
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
      {(unsupported || error) && (
        <div role="status" className={clsx('mt-1 max-w-[220px]', big ? 'text-xs' : 'text-[10px]', 'text-[var(--danger)]')}>
          {unsupported ? t('ptz.noPtz') : error}
        </div>
      )}
      {big && <p className="mt-3 text-xs text-[var(--text-dim)]">{t('ptz.hint')}</p>}
    </div>
  )
}
