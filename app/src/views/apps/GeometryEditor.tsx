/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Geometry editor for App SDK `geometry.polygon` / `geometry.tripwire`
 * config params. An operator draws a zone or a tripwire directly on a
 * live camera still (falls back to a plain grid when no snapshot is
 * available) instead of hand-typing pixel coordinates into a textarea.
 *
 * COORDINATES ARE NORMALIZED 0–1 (resolution-independent — the snapshot
 * and the app's processing resolution need not match). The value stored
 * in the app config is keyed by camera id:
 *   polygon  → { "<cam>": [[x,y], [x,y], …] }         (≥3 points)
 *   tripwire → { "<cam>": { a:[x,y], b:[x,y], count_direction } }
 * This is exactly the per-camera dict the server's validate_app_config
 * checks and the SDK geometry consumes.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiService } from '../../lib/apiService'
import { displayAspect } from '../../lib/aspect'
import type { AspectOverride } from '../../lib/aspect'
import { Button } from '../../components/ui'

type Pt = [number, number]
type Dir = 'both' | 'a_to_b' | 'b_to_a'
type Tripwire = { a: Pt; b: Pt; count_direction: Dir }
type PerCam = Record<string, Pt[] | Tripwire>

type Camera = {
  id: number | string
  name?: string
  display_aspect_ratio?: AspectOverride | null
}

// Parse whatever is currently stored (JSON string or object) into the
// per-camera map; tolerate junk by starting empty rather than throwing.
function parseValue(raw: string): PerCam {
  if (!raw || !raw.trim()) return {}
  try {
    const v = JSON.parse(raw)
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as PerCam) : {}
  } catch {
    return {}
  }
}

function clamp01(n: number): number {
  return Math.max(0, Math.min(1, n))
}

// One camera snapshot, fetched as an object URL (revoked on unmount /
// camera change). A failed capture (offline camera) resolves to null →
// the editor draws on a grid.
function useSnapshotUrl(cameraId: number | string | null) {
  const numericId = typeof cameraId === 'number' ? cameraId : Number(cameraId)
  const enabled = cameraId != null && Number.isFinite(numericId)
  const query = useQuery({
    queryKey: ['camera-snapshot', cameraId],
    queryFn: async () => {
      const { data } = await apiService.getCameraSnapshot(numericId)
      return URL.createObjectURL(data as Blob)
    },
    enabled,
    retry: 0,
    staleTime: 30_000,
  })
  useEffect(() => {
    const url = query.data
    return () => {
      if (url) URL.revokeObjectURL(url)
    }
  }, [query.data])
  return query
}

export function GeometryEditor({
  kind,
  value,
  onChange,
}: {
  kind: 'polygon' | 'tripwire'
  value: string
  onChange: (json: string) => void
}) {
  const perCam = useMemo(() => parseValue(value), [value])
  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = (data?.cameras ?? data ?? []) as Camera[]
      return Array.isArray(list) ? list : []
    },
    retry: 0,
  })
  const cameras = camerasQuery.data ?? []

  // Active camera: first stored, else first in the roster, else "1".
  const [cam, setCam] = useState<string>('')
  useEffect(() => {
    if (cam) return
    const first = Object.keys(perCam)[0] ?? (cameras[0] && String(cameras[0].id))
    if (first) setCam(first)
  }, [cam, perCam, cameras])

  const snap = useSnapshotUrl(cam || null)
  // Natural size of the still. The snapshot comes off the camera at its CODED
  // size, so a 1080N frame arrives 960x1080 — see lib/aspect.ts (#354).
  const [snapDims, setSnapDims] = useState<{ w: number; h: number } | null>(null)
  useEffect(() => {
    setSnapDims(null)
  }, [snap.data])
  const svgRef = useRef<SVGSVGElement | null>(null)
  const [drag, setDrag] = useState<number | 'a' | 'b' | null>(null)

  const write = (next: PerCam) => onChange(JSON.stringify(next))

  const current = cam ? perCam[cam] : undefined
  const poly: Pt[] = kind === 'polygon' && Array.isArray(current) ? current : []
  const wire: Tripwire | null =
    kind === 'tripwire' && current && !Array.isArray(current)
      ? (current as Tripwire)
      : null

  const evtToNorm = (e: React.MouseEvent): Pt => {
    const svg = svgRef.current!
    const r = svg.getBoundingClientRect()
    return [clamp01((e.clientX - r.left) / r.width), clamp01((e.clientY - r.top) / r.height)]
  }

  const onCanvasClick = (e: React.MouseEvent) => {
    if (!cam || drag != null) return
    const p = evtToNorm(e)
    if (kind === 'polygon') {
      write({ ...perCam, [cam]: [...poly, p] })
    } else {
      // Tripwire: first click sets A, second sets B, later clicks move B.
      if (!wire) write({ ...perCam, [cam]: { a: p, b: p, count_direction: 'both' } })
      else write({ ...perCam, [cam]: { ...wire, b: p } })
    }
  }

  const onPointDrag = (e: React.MouseEvent) => {
    if (drag == null || !cam) return
    const p = evtToNorm(e)
    if (kind === 'polygon' && typeof drag === 'number') {
      const next = poly.slice()
      next[drag] = p
      write({ ...perCam, [cam]: next })
    } else if (wire && (drag === 'a' || drag === 'b')) {
      write({ ...perCam, [cam]: { ...wire, [drag]: p } })
    }
  }

  const removePoint = (i: number) => {
    if (kind !== 'polygon') return
    write({ ...perCam, [cam]: poly.filter((_, k) => k !== i) })
  }

  const clearCam = () => {
    const next = { ...perCam }
    delete next[cam]
    write(next)
  }

  // Draw surface aspect. It must match the still's DISPLAY aspect, not the
  // container's old hard-coded 16:9: the overlay's normalized 0–1 coordinates
  // span the whole box, so if the image doesn't span exactly the same box
  // every point an operator places lands somewhere else in the frame the
  // pipeline processes. Falls back to 16:9 until the still loads.
  const camOverride = cameras.find((c) => String(c.id) === cam)
  const drawAspect =
    displayAspect(snapDims?.w, snapDims?.h, camOverride?.display_aspect_ratio) ?? 16 / 9

  // The SVG viewBox is VH tall and VW wide, with the container locked to the
  // same aspect, so x and y scale equally — circles stay round and strokes
  // uniform. Deriving VW from drawAspect is what preserves that for a camera
  // that isn't 16:9. Crucially, `<polygon points>` needs UNITLESS numbers
  // (percentages are invalid there and render nothing — the bug the first
  // draft had); viewBox coords fix that. VW/VH map normalized 0–1 into that
  // space.
  const VH = 90
  const VW = Math.round(VH * drawAspect)
  const vx = (n: number) => n * VW
  const vy = (n: number) => n * VH

  return (
    <div className="space-y-2">
      {/* Camera selector (per-camera geometry) */}
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-[var(--text-dim)]">Camera:</span>
        {cameras.length === 0 ? (
          <input
            className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)] w-24"
            placeholder="camera id"
            value={cam}
            onChange={(e) => setCam(e.target.value)}
          />
        ) : (
          <select
            className="px-2 py-1 rounded border border-[var(--border)] bg-[var(--bg-2)]"
            value={cam}
            onChange={(e) => setCam(e.target.value)}
          >
            {cameras.map((c) => (
              <option key={String(c.id)} value={String(c.id)}>
                {c.name ? `${c.name} (#${c.id})` : `#${c.id}`}
                {perCam[String(c.id)] ? ' ✓' : ''}
              </option>
            ))}
          </select>
        )}
        <span className="ml-auto text-[var(--text-dim)]">
          {kind === 'polygon'
            ? `${poly.length} point${poly.length === 1 ? '' : 's'}`
            : wire
              ? 'tripwire set'
              : 'no tripwire'}
        </span>
        <Button variant="ghost" className="text-xs px-2 py-0.5" onClick={clearCam} disabled={!current}>
          Clear
        </Button>
      </div>

      {/* Draw surface: snapshot (or grid) + SVG overlay */}
      <div className="relative w-full rounded border border-[var(--border)] overflow-hidden bg-[var(--bg-2)]" style={{ aspectRatio: String(drawAspect) }}>
        {snap.data ? (
          /* object-fill, not object-cover: cover crops a still whose aspect
             differs from the box, and the cropped-away band is frame the
             operator would then be unable to draw on (and would mis-map). */
          <img
            src={snap.data}
            alt="camera still"
            className="absolute inset-0 w-full h-full object-fill"
            draggable={false}
            onLoad={(e) =>
              setSnapDims({
                w: e.currentTarget.naturalWidth,
                h: e.currentTarget.naturalHeight,
              })
            }
          />
        ) : (
          <div className="absolute inset-0" style={{
            backgroundImage:
              'linear-gradient(var(--border) 1px, transparent 1px), linear-gradient(90deg, var(--border) 1px, transparent 1px)',
            backgroundSize: '10% 10%', opacity: 0.5,
          }} />
        )}
        <svg
          ref={svgRef}
          viewBox={`0 0 ${VW} ${VH}`}
          preserveAspectRatio="none"
          className="absolute inset-0 w-full h-full cursor-crosshair"
          onClick={onCanvasClick}
          onMouseMove={onPointDrag}
          onMouseUp={() => setDrag(null)}
          onMouseLeave={() => setDrag(null)}
        >
          {kind === 'polygon' && poly.length > 0 && (
            <>
              <polygon
                points={poly.map((p) => `${vx(p[0])},${vy(p[1])}`).join(' ')}
                fill="rgba(59,130,246,0.25)" stroke="rgb(59,130,246)"
                strokeWidth={2} vectorEffect="non-scaling-stroke"
              />
              {poly.map((p, i) => (
                <circle
                  key={i} cx={vx(p[0])} cy={vy(p[1])} r={1.6}
                  fill="rgb(59,130,246)" stroke="white" strokeWidth={0.6}
                  style={{ cursor: 'grab' }}
                  onMouseDown={(e) => { e.stopPropagation(); setDrag(i) }}
                  onDoubleClick={(e) => { e.stopPropagation(); removePoint(i) }}
                />
              ))}
            </>
          )}
          {kind === 'tripwire' && wire && (
            <>
              <defs>
                <marker id="tw-arrow" markerWidth={6} markerHeight={6} refX={5} refY={3} orient="auto">
                  <path d="M0,0 L0,6 L6,3 z" fill="rgb(234,88,12)" />
                </marker>
              </defs>
              <line
                x1={vx(wire.a[0])} y1={vy(wire.a[1])}
                x2={vx(wire.b[0])} y2={vy(wire.b[1])}
                stroke="rgb(234,88,12)" strokeWidth={2.5}
                vectorEffect="non-scaling-stroke"
                markerEnd={wire.count_direction === 'a_to_b' ? 'url(#tw-arrow)' : undefined}
                markerStart={wire.count_direction === 'b_to_a' ? 'url(#tw-arrow)' : undefined}
              />
              {(['a', 'b'] as const).map((k) => (
                <circle
                  key={k} cx={vx(wire[k][0])} cy={vy(wire[k][1])} r={1.8}
                  fill="rgb(234,88,12)" stroke="white" strokeWidth={0.6}
                  style={{ cursor: 'grab' }}
                  onMouseDown={(e) => { e.stopPropagation(); setDrag(k) }}
                />
              ))}
              {/* A/B labels so the direction toggle reads clearly */}
              <text x={vx(wire.a[0])} y={vy(wire.a[1]) - 2.5} fontSize={4} fill="white" textAnchor="middle">A</text>
              <text x={vx(wire.b[0])} y={vy(wire.b[1]) - 2.5} fontSize={4} fill="white" textAnchor="middle">B</text>
            </>
          )}
        </svg>
      </div>

      {/* Hints + tripwire direction control */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--text-dim)]">
        {kind === 'polygon' ? (
          <span>Click to add points · drag a point to move · double-click a point to delete{poly.length > 0 && poly.length < 3 ? ' · need at least 3' : ''}</span>
        ) : (
          <>
            <span>Click to set the line, drag the endpoints ·</span>
            <span>count:</span>
            {(['both', 'a_to_b', 'b_to_a'] as Dir[]).map((d) => (
              <button
                key={d}
                disabled={!wire}
                className={`px-2 py-0.5 rounded border text-xs ${wire && wire.count_direction === d ? 'border-[var(--accent)] text-[var(--accent)]' : 'border-[var(--border)]'}`}
                onClick={() => wire && write({ ...perCam, [cam]: { ...wire, count_direction: d } })}
              >
                {d === 'both' ? 'both ways' : d === 'a_to_b' ? 'A→B' : 'B→A'}
              </button>
            ))}
          </>
        )}
        {snap.isError && <span className="text-amber-400">(camera offline — drawing on a grid)</span>}
      </div>
    </div>
  )
}
