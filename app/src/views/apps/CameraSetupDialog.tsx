/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Setting up ONE camera for an app: every per-camera shape the app asks
// for (scan zone, guard post, tripwire, ROI…), drawn on that camera, in
// one place (#480).
//
// It used to be a grid navigated by dropdown: each per-camera field was
// its own section of the form with its own camera selector, so drawing
// Guard Scan's two zones on two cameras meant choosing a camera four
// times. Opened from the camera's card, the camera is already chosen.
//
// Generic: the fields come from the manifest (`per_camera=True`,
// `geometry.*`), so every app gets this with no app-specific UI. Edits
// are held here until Done, then written into the form's draft — which
// still needs the form's Save, like everything else in it. Cancel drops
// them.

import { useState } from 'react'
import { Button } from '../../components/ui'
import { GeometryEditor, type GeometryReference } from './GeometryEditor'
import { StackedDialog } from './StackedDialog'

export type SetupParam = {
  name: string
  type: string
  label?: string
  description?: string
}

/** A short name for a per-camera field: "Scan zone", "Entry line", "ROI". */
export function shortParamName(name: string): string {
  if (name === 'roi') return 'ROI'
  return name.charAt(0).toUpperCase() + name.slice(1).replace(/_/g, ' ')
}

function parse(raw: string | boolean | undefined): Record<string, unknown> {
  try {
    const v = JSON.parse(String(raw ?? '') || '{}')
    return v && typeof v === 'object' && !Array.isArray(v) ? v : {}
  } catch {
    return {}
  }
}

/** This camera's entry in a per-camera map (keyed "3", or "cam3"). */
export function entryFor(raw: string | boolean | undefined, cameraId: number): unknown {
  const map = parse(raw)
  return map[String(cameraId)] ?? map[`cam${cameraId}`]
}

export function isDrawn(v: unknown): boolean {
  return Array.isArray(v) ? v.length > 0 : !!(v && typeof v === 'object')
}

export function CameraSetupDialog({
  appName, cameraId, cameraName, location, params, values, canEdit, onCancel, onDone,
}: {
  appName: string
  cameraId: number
  cameraName: string
  location?: string | null
  params: SetupParam[]
  values: Record<string, string | boolean>
  /** False for a camera this user can't manage: shapes shown, not editable. */
  canEdit: boolean
  onCancel: () => void
  /** The edited values of `params`, to merge into the form. */
  onDone: (edited: Record<string, string>) => void
}) {
  const [local, setLocal] = useState<Record<string, string>>(() =>
    Object.fromEntries(params.map((p) => [p.name, String(values[p.name] ?? '')])))
  const [active, setActive] = useState(params[0]?.name ?? '')
  const param = params.find((p) => p.name === active) ?? params[0]
  const changed = params.some((p) => local[p.name] !== String(values[p.name] ?? ''))

  if (!param) return null

  const t = (param.type || '').toLowerCase()
  const references: GeometryReference[] = params
    .filter((p) => p.name !== param.name)
    .map((p) => ({ label: shortParamName(p.name), value: entryFor(local[p.name], cameraId) }))
    .filter((r) => isDrawn(r.value))

  return (
    <StackedDialog
      title={`Set up ${cameraName}`}
      subtitle={[appName, location].filter(Boolean).join(' · ')}
      widthClassName="w-[960px]"
      onClose={onCancel}
      footer={(
        <>
          <span className="text-xs text-[var(--text-dim)]">
            {canEdit
              ? 'Done keeps these in the form — Save there applies them.'
              : "You can't manage this camera."}
          </span>
          <Button variant="ghost" size="sm" className="ml-auto" onClick={onCancel}>
            {canEdit ? 'Cancel' : 'Close'}
          </Button>
          {canEdit && (
            <Button variant="primary" size="sm" onClick={() => onDone(local)} disabled={!changed}>
              Done
            </Button>
          )}
        </>
      )}
    >
      {params.length > 1 && (
        <div role="tablist" className="flex gap-1 border-b border-[var(--border)] px-4 pt-2">
          {params.map((p) => {
            const drawn = isDrawn(entryFor(local[p.name], cameraId))
            const on = p.name === param.name
            return (
              <button
                key={p.name}
                type="button"
                role="tab"
                aria-selected={on}
                onClick={() => setActive(p.name)}
                className={`-mb-px border-b-2 px-3 py-1.5 text-xs ${
                  on ? 'border-[var(--accent)] text-[var(--text)]'
                     : 'border-transparent text-[var(--text-dim)] hover:text-[var(--text)]'}`}
              >
                {shortParamName(p.name)}
                <span className={`ml-1.5 ${drawn ? 'text-emerald-400' : 'text-[var(--text-dim)]'}`}>
                  {drawn ? '✓' : '·'}
                </span>
              </button>
            )
          })}
        </div>
      )}
      <div className="min-h-0 flex-1 space-y-2 overflow-y-auto px-4 py-3">
        <div>
          <div className="text-sm font-medium">{param.label || shortParamName(param.name)}</div>
          {param.description && (
            <div className="text-xs text-[var(--text-dim)]">{param.description}</div>
          )}
        </div>
        {(t === 'geometry.polygon' || t === 'geometry.tripwire') && (
          <GeometryEditor
            key={param.name}
            kind={t === 'geometry.tripwire' ? 'tripwire' : 'polygon'}
            cameraId={cameraId}
            value={local[param.name]}
            onChange={(json) => setLocal((v) => ({ ...v, [param.name]: json }))}
            references={references}
            readOnly={!canEdit}
          />
        )}
      </div>
    </StackedDialog>
  )
}
