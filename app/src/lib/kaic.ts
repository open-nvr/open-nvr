/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * What the AI layer can do right now, read the way the server reads it.
 *
 * KAI-C's /capabilities nests each adapter's contract under
 * `adapters[name].capabilities` ({ url, capabilities: { tasks_advertised,
 * … } } — or { url, error } when the adapter is down). Three views used
 * to read `adapters[name].tasks_advertised` — one level too high — so
 * every app's "requires <task>" badge said "not installed" whatever was
 * running. This is the one place that shape is known.
 *
 * Tier-0 is the second source of truth: the platform's always-on
 * detect-pipeline is not a KAI-C adapter, but it IS object detection on
 * the bus — the stream that Detector apps (occupancy, loitering,
 * line-crossing) actually consume. When it is running, `object_detection`
 * is provided even with no adapter registered.
 */

export type AdapterCapsEntry = Record<string, any> | undefined | null

export type CapabilitiesLike = {
  adapters?: Record<string, AdapterCapsEntry>
} | undefined | null

export type Tier0Like = {
  available?: boolean
  mode?: string
  health?: { workers_up?: number } | null
} | undefined | null

/** The task the platform's Tier-0 detector provides on the bus. */
export const TIER0_TASK = 'object_detection'

function asStringList(v: unknown): string[] {
  return Array.isArray(v) ? v.map(String) : []
}

/** The contract block of one adapter entry, whichever shape it arrived in. */
export function adapterContract(entry: AdapterCapsEntry): Record<string, any> {
  if (!entry || typeof entry !== 'object') return {}
  const nested = (entry as any).capabilities
  return nested && typeof nested === 'object' ? nested : (entry as Record<string, any>)
}

/** Tasks one adapter advertises (`tasks_advertised`, or legacy `tasks`). */
export function adapterTasks(entry: AdapterCapsEntry): string[] {
  const c = adapterContract(entry)
  return asStringList(c.tasks_advertised).concat(asStringList(c.tasks))
}

/** Whether Tier-0 detection is actually running (not merely configured). */
export function tier0Running(t0: Tier0Like): boolean {
  if (!t0 || !t0.available) return false
  if (t0.mode === 'not_running') return false
  const up = t0.health?.workers_up
  return typeof up === 'number' ? up > 0 : true
}

/** Every task something provides right now: adapters ∪ Tier-0. */
export function availableTasks(caps: CapabilitiesLike, t0?: Tier0Like): Set<string> {
  const out = new Set<string>()
  for (const entry of Object.values(caps?.adapters ?? {})) {
    for (const t of adapterTasks(entry)) out.add(t)
  }
  if (tier0Running(t0)) out.add(TIER0_TASK)
  return out
}

/** Who provides a task, for the badge text: 'adapter' | 'tier0' | null. */
export function taskProvider(task: string, caps: CapabilitiesLike, t0?: Tier0Like): 'adapter' | 'tier0' | null {
  for (const entry of Object.values(caps?.adapters ?? {})) {
    if (adapterTasks(entry).includes(task)) return 'adapter'
  }
  if (task === TIER0_TASK && tier0Running(t0)) return 'tier0'
  return null
}
