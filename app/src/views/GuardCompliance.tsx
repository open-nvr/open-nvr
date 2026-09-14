/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Entry-screening compliance: how consistently the guard follows the
// scanning procedure, and who was flagged by the wand.
//
// The page answers two different questions for two different people,
// and keeps them apart:
//
//   "is my team doing the procedure?"   — the rate, the trend, per guard
//   "who did the scanner find?"         — the flagged screenings, with photos
//
// The figure is complete scans over ALL screenings, which is why the
// clean ones are recorded too. A period with nothing screened shows a
// dash, never 100%: nobody walked past that camera, and a green tile
// over an empty day is a number someone would act on.

import { useCallback, useMemo, useRef, useState, type ReactNode } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import {
  ChevronDown, ChevronUp, Download, FileText, Settings2, ShieldCheck,
} from 'lucide-react'
import { useAuth } from '../auth/AuthContext'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { AuthedImage } from '../components/AuthedImage'
import { EvidenceViewer } from '../components/EvidenceViewer'
import {
  Badge, Button, Card, CardContent, EmptyState, ErrorCard, PageHeader, Skeleton,
  type BadgeVariant,
} from '../components/ui'
import { DataTable, type Column } from '../components/ui/DataTable'
import { Pagination } from '../components/ui/Pagination'
import { SegmentedControl } from '../components/ui/SegmentedControl'
import { usePagination } from '../hooks/usePagination'
import { APP_VERTICALS, manifestProvides } from '../lib/appVerticals'
import { AppConfigModal, type RegisteredApp } from './AppCatalog'
import { LiveCameraPanel } from './guardscan/LiveCameraPanel'
import { ScreeningClip } from './guardscan/ScreeningClip'
import { ScreeningReport } from './guardscan/ScreeningReport'
import {
  guardScanService, VERDICT_LABEL,
  type ComplianceReport, type Screening, type Tally,
} from '../services/guardScanService'

export const GUARD_SCAN_CAPABILITY = 'guard_scan'

// The same row the nav and the catalog read, so "the app that lights up
// this page" means one thing everywhere — including for installs whose
// manifest predates `provides` and is matched on `requires_tasks`.
const GUARD_VERTICAL = APP_VERTICALS.find(
  (v) => v.capability === GUARD_SCAN_CAPABILITY)!

// Verdicts are STATUS, not series identity, so they wear the status
// palette and every one of them is labelled — never colour alone.
const VERDICT_COLOUR: Record<string, string> = {
  compliant: 'var(--ok)',
  partial: 'var(--warn)',
  incomplete: 'var(--danger)',
  no_scan: 'var(--critical)',
}
const VERDICT_BADGE: Record<string, BadgeVariant> = {
  compliant: 'success',
  partial: 'warning',
  incomplete: 'destructive',
  no_scan: 'critical',
}
const VERDICT_ORDER = ['compliant', 'partial', 'incomplete', 'no_scan'] as const

// The report groups screenings the app could not attribute under this
// exact string (server/routers/guardscan.py). Until a duty roster
// exists it is EVERY row, and a "By guard" table whose only line is
// "unidentified" reads as a broken page rather than as missing setup.
const NO_GUARD = 'unidentified'

/** Where the summary row's folded/unfolded state is remembered. */
const SUMMARY_KEY = 'opennvr.guardscan.summary'
/** …and how the list and the camera share the width. */
const SPLIT_KEY = 'opennvr.guardscan.split'
//: Neither pane may be squeezed to uselessness: a table under about
//: half the row cannot show its columns, and a camera over about half
//: is no longer a side panel.
const SPLIT_MIN = 45
const SPLIT_MAX = 85

const PERIODS = [
  { value: 'day', label: 'Daily' },
  { value: 'week', label: 'Weekly' },
  { value: 'month', label: 'Monthly' },
]
const RANGES = [
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days' },
]

type CameraRow = { id: number; name: string }

function pct(value: number | null): string {
  return value === null || value === undefined ? '—' : `${value}%`
}

/**
 * The window the whole page covers, as the API wants it. Computed at
 * request time rather than held in state, so the query key stays `days`
 * and a re-render never invents a new range.
 */
function since(days: number): string {
  return new Date(Date.now() - days * 86_400_000).toISOString()
}

export default function GuardCompliance() {
  const { showError, showSuccess } = useSnackbar()
  const { user: me } = useAuth()
  const [period, setPeriod] = useState<'day' | 'week' | 'month'>('day')
  const [days, setDays] = useState(7)
  // What the operator is looking for. "Problems" is the reason this
  // list exists: on a busy door the handful that went wrong sit tens of
  // rows below everything that went right, and scrolling for them is
  // not a search.
  const [show, setShow] = useState<'all' | 'problems' | 'flagged'>('all')
  const [cameraId, setCameraId] = useState<number | ''>('')
  const [viewing, setViewing] = useState<Screening | null>(null)
  const [exporting, setExporting] = useState(false)
  const [configOpen, setConfigOpen] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  // Collapsing the summary hands its height to the list. Remembered,
  // because someone who works from the list all day should not have to
  // fold the figures away again every morning.
  const [summaryOpen, setSummaryOpen] = useState(() => {
    try {
      return window.localStorage.getItem(SUMMARY_KEY) !== 'closed'
    } catch {
      return true          // private mode throws rather than returning null
    }
  })
  // How the last row is divided, remembered like the page size is.
  const [split, setSplit] = useState<number>(() => {
    try {
      const raw = Number(window.localStorage.getItem(SPLIT_KEY))
      return Number.isFinite(raw) && raw >= SPLIT_MIN && raw <= SPLIT_MAX ? raw : 75
    } catch {
      return 75
    }
  })
  const [popped, setPopped] = useState(false)
  const rowRef = useRef<HTMLDivElement>(null)

  const startDrag = useCallback((down: React.PointerEvent) => {
    const row = rowRef.current
    if (!row) return
    down.preventDefault()
    // Capture on the handle, so a fast drag that outruns the pointer
    // keeps resizing instead of dropping the gesture over the table.
    const handle = down.currentTarget as HTMLElement
    handle.setPointerCapture(down.pointerId)
    const move = (e: PointerEvent) => {
      const box = row.getBoundingClientRect()
      if (box.width <= 0) return
      const pct = ((e.clientX - box.left) / box.width) * 100
      setSplit(Math.round(Math.min(SPLIT_MAX, Math.max(SPLIT_MIN, pct))))
    }
    const up = () => {
      handle.releasePointerCapture(down.pointerId)
      handle.removeEventListener('pointermove', move)
      handle.removeEventListener('pointerup', up)
      setSplit((pct) => {
        try {
          window.localStorage.setItem(SPLIT_KEY, String(pct))
        } catch {
          // Not remembering a preference is not an error.
        }
        return pct
      })
    }
    handle.addEventListener('pointermove', move)
    handle.addEventListener('pointerup', up)
  }, [])

  const toggleSummary = () => {
    setSummaryOpen((open) => {
      try {
        window.localStorage.setItem(SUMMARY_KEY, open ? 'closed' : 'open')
      } catch {
        // Not remembering a preference is not an error.
      }
      return !open
    })
  }
  const rows = usePagination(25, 'guard-screenings')
  const tz = useMemo(() => -new Date().getTimezoneOffset(), [])

  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as CameraRow[]
    },
    retry: 0,
  })
  const cameraName = (id: number | null) =>
    id == null ? '—' : camerasQuery.data?.find((c) => c.id === id)?.name ?? `cam${id}`

  // Which app owns this page, so Configure can open its form here
  // rather than sending the operator to hunt through the App Catalog.
  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      const list = Array.isArray(data) ? data : (data as any)?.apps
      return (Array.isArray(list) ? list : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const guardApp = (appsQuery.data ?? []).find(
    (a) => manifestProvides(a.manifest, GUARD_VERTICAL))
  const canConfigure = !!me?.is_superuser

  const report = useQuery({
    queryKey: ['guardscan-report', period, days, tz, cameraId],
    queryFn: async () => (await guardScanService.getReport({
      period, days, tz_offset_minutes: tz,
      ...(cameraId === '' ? {} : { camera_id: cameraId }),
    })).data as ComplianceReport,
    refetchInterval: 30_000,
    // Every one of those values is part of the query KEY, so switching
    // Weekly or a camera asks react-query for a series it has never
    // fetched — and an unseen key has no cached data. Without this the
    // page renders its own empty state while the request is in flight:
    // the tiles fall back to "—" and 0, the chart is replaced by a
    // skeleton, and a moment later everything snaps back. Nothing was
    // wrong with the numbers; the page just threw them away first.
    //
    // Keeping the previous answer on screen means the figures change
    // once, from old to new, which is what the operator asked for.
    placeholderData: keepPreviousData,
  })

  const screenings = useQuery({
    queryKey: ['guardscan-screenings', show, days, cameraId, rows.skip, rows.pageSize],
    queryFn: async () => (await guardScanService.listScreenings({
      limit: rows.pageSize,
      skip: rows.skip,
      // The range control sits over the whole page, so it governs the
      // list too. It used to move the chart and leave the table
      // identical, with nothing on screen saying why.
      from: since(days),
      ...(cameraId === '' ? {} : { camera_id: cameraId }),
      ...(show === 'flagged' ? { flagged: true } : {}),
      ...(show === 'problems' ? { outcome: 'problems' as const } : {}),
    })).data as { screenings: Screening[]; total: number },
    refetchInterval: 15_000,
    // Same reason, and the same fix the alarm list already uses
    // (useAlarmsList.ts): without it the table empties to skeleton rows
    // on every page turn, filter and camera change.
    placeholderData: keepPreviousData,
  })

  const totals = report.data?.totals
  const list = screenings.data?.screenings ?? []
  // Every screening is attributed to nobody until a duty roster exists,
  // so the per-guard breakdown is only worth a table once at least one
  // row names a real person.
  const guards = (report.data?.guards ?? []).filter((g) => g.label !== NO_GUARD)

  // The door being watched: whichever camera the operator filtered to,
  // and otherwise the one the most recent screening came from — that is
  // the entrance with people walking through it.
  const liveCameraId = cameraId !== ''
    ? cameraId
    : list.find((s) => s.camera_id != null)?.camera_id ?? null

  const download = async () => {
    setExporting(true)
    try {
      const res = await guardScanService.exportCsv(days)
      const url = URL.createObjectURL(res.data as Blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `screenings-${days}d.csv`
      a.click()
      URL.revokeObjectURL(url)
      showSuccess('Screenings exported.')
    } catch (err) {
      // A download that silently does nothing is indistinguishable from
      // a browser that blocked it, so say which happened.
      showError(extractApiError(err, 'Could not export the screenings.'))
    } finally {
      setExporting(false)
    }
  }

  const resetPage = () => rows.setPage(1)

  return (
    <section className="flex min-h-0 flex-col space-y-4">
      <PageHeader
        title={
          <span className="inline-flex items-center gap-2">
            <ShieldCheck size={18} className="text-[var(--accent)]" /> Entry Screening
          </span>
        }
        description="Did the guard scan every person, properly?"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {/* This one really is page-wide: it moves the tiles, the
                chart AND the list. It briefly lived on the chart, back
                when it only drove the chart — then the list was wired
                to it too, and a control that changes three things while
                sitting inside one of them is a control in the wrong
                place. */}
            <SegmentedControl
              label="Range"
              showLabel={false}
              value={String(days)}
              options={RANGES}
              onChange={(v) => { setDays(Number(v)); resetPage() }}
            />
            {guardApp && canConfigure && (
              <Button variant="outline" size="sm" onClick={() => setConfigOpen(true)}>
                <Settings2 size={13} /> Configure
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={() => setReportOpen(true)}>
              <FileText size={13} /> Report
            </Button>
            <Button variant="outline" size="sm" onClick={download} disabled={exporting}>
              <Download size={13} /> {exporting ? 'Exporting…' : 'CSV'}
            </Button>
          </div>
        }
      />

      {/* Row one: the figures on the left half, the trend on the right.
          Two rows of two strips rather than four across, so the block
          is the same height as the chart beside it and neither leaves a
          band of empty card.

          Dimmed only while the figures on screen belong to a DIFFERENT
          query than the one now selected — `isPlaceholderData`, not
          `isFetching`. Keying it off isFetching would dim them every
          thirty seconds on the background poll, which is a worse
          distraction than the flicker this replaced. */}
      {/* Row one is ONE card, and the toggle sits on it. A control that
          folds a card belongs to that card — in the page header it read
          as a page-wide switch, next to Report and CSV which are
          nothing of the kind. */}
      <Card>
        <CardContent className="p-3">
          <button
            type="button"
            onClick={toggleSummary}
            aria-expanded={summaryOpen}
            title={summaryOpen
              ? 'Hide the figures and give the height to the list'
              : 'Show the figures and the trend'}
            className="flex w-full items-center gap-2 text-left text-xs
                       font-semibold text-[var(--text-dim)]
                       hover:text-[var(--text)]"
          >
            Summary
            <span className="ml-auto">
              {summaryOpen ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            </span>
          </button>

          {summaryOpen && (
          <div className="mt-2 grid gap-3 lg:grid-cols-2">
          <div
            className={`grid grid-cols-2 gap-2 ${
              report.isPlaceholderData ? 'opacity-60 transition-opacity' : ''}`}
            aria-busy={report.isPlaceholderData || undefined}
          >
        <Tile
          label="Compliance"
          value={pct(totals?.compliance ?? null)}
          hint="Complete scans, of every screening"
          tone={toneFor(totals?.compliance ?? null)}
        />
        <Tile
          label="Screenings"
          value={String(totals?.screenings ?? 0)}
          hint="In the selected range"
        />
        <Tile
          label="Scanner flags"
          value={String(totals?.flagged ?? 0)}
          hint="The wand's indicator lit"
          tone={(totals?.flagged ?? 0) > 0 ? 'bad' : undefined}
        />
        <Tile
          label="Mean score"
          hint="Average of every screening's score"
          value={totals?.mean_score === null || totals?.mean_score === undefined
            ? '—' : String(totals.mean_score)}
        />
      </div>

          <div className={`h-full ${
            report.isPlaceholderData ? 'opacity-60 transition-opacity' : ''}`}>
        {/* Everything that is not a bar is chrome, and chrome here was
            eating the height the bars needed: a 14px heading, a line of
            prose, a legend at 11px and a fixed-aspect plot left the
            bars at about a third of the card. Title and legend are now
            label-sized and share one row each, and the plot takes
            whatever is left — so the bars grow with the card instead of
            sitting in it. */}
        <div className="flex h-full flex-col gap-2">
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <h3 className="min-w-0 text-xs font-semibold text-[var(--text)]"
                title="Every screening, by outcome. Hover a bar for the breakdown.">
              How each period went
            </h3>
            {/* Grouping stays: it changes nothing but this chart. */}
            <div className="ml-auto">
              <SegmentedControl
                label="Group by"
                showLabel={false}
                value={period}
                options={PERIODS}
                onChange={(v) => setPeriod(v as 'day' | 'week' | 'month')}
              />
            </div>
          </div>
          {report.isPending ? (
            <Skeleton className="min-h-[88px] flex-1" />
          ) : report.isError ? (
            // Without this a failed request fell through to the empty
            // state and told the operator to install an app they are
            // already running.
            <ErrorCard
              title="Could not load the compliance report"
              message={extractApiError(report.error, 'Please try again.')}
              onRetry={() => report.refetch()}
            />
          ) : report.data && report.data.buckets.length > 0 ? (
            <>
              <div className="min-h-[88px] flex-1">
                <BucketChart buckets={report.data.buckets} />
              </div>
              <Legend />
            </>
          ) : (
            <EmptyState
              icon={<ShieldCheck size={28} />}
              title="No screenings recorded yet"
              description={guardApp
                ? 'The app is installed. Assign it an entrance camera and draw the scan zone, then screenings appear here.'
                : 'Install the Guard Scan Compliance app from the App Catalog and assign it an entrance camera.'}
            />
          )}
        </div>
          </div>
          </div>
          )}
        </CardContent>
      </Card>

      {guards.length > 0 && (
        <Card>
          <CardContent>
            <h3 className="mb-3 text-sm font-semibold">By guard</h3>
            <GuardTable guards={guards} />
          </CardContent>
        </Card>
      )}
      {/* The list and the door, side by side, taking whatever height is
          left. Three quarters to the list because that is what an
          operator reads; a quarter is enough to see who is at the
          entrance, and the panel is tall enough there to be worth
          looking at. */}
      <div ref={rowRef} className="flex min-h-0 flex-1 gap-0">
      <Card className="flex min-h-0 flex-col"
            style={{ width: popped ? '100%' : `${split}%` }}>
        <CardContent className="flex min-h-0 flex-1 flex-col p-3">
        <h3 className="mb-2 text-sm font-semibold">Recent screenings</h3>
        <ScreeningTable
            rows={list}
            query={screenings}
            show={show}
            cameraName={cameraName}
            onOpen={setViewing}
            toolbar={
              <div className="flex flex-wrap items-center gap-2 py-1.5 pl-3">
                <SegmentedControl
                  label="Show"
                  showLabel={false}
                  value={show}
                  options={[
                    { value: 'all', label: 'All' },
                    { value: 'problems', label: 'Not complete' },
                    { value: 'flagged', label: 'Scanner flags' },
                  ]}
                  onChange={(v) => {
                    setShow(v as 'all' | 'problems' | 'flagged')
                    resetPage()
                  }}
                />
                <select
                  value={cameraId}
                  onChange={(e) => {
                    setCameraId(e.target.value === '' ? '' : Number(e.target.value))
                    resetPage()
                  }}
                  aria-label="Filter by camera"
                  className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-xs"
                >
                  <option value="">All cameras</option>
                  {(camerasQuery.data ?? []).map((c) => (
                    <option key={c.id} value={c.id}>{c.name}</option>
                  ))}
                </select>
                <div className="ml-auto">
                  <Pagination
                    page={rows.page}
                    pageSize={rows.pageSize}
                    total={screenings.data?.total}
                    rowCount={list.length}
                    hasNext={list.length === rows.pageSize}
                    isFetching={screenings.isFetching}
                    label="screenings"
                    onPageChange={rows.setPage}
                    onPageSizeChange={rows.setPageSize}
                  />
                </div>
              </div>
            }
        />
        </CardContent>
      </Card>

      {/* The handle between them. Whoever is watching the door wants a
          bigger picture; whoever is auditing wants more rows — and it
          is not our place to decide which, so it drags. */}
      {!popped && (
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize the list and the camera"
          onPointerDown={startDrag}
          className="group flex w-3 shrink-0 cursor-col-resize items-center justify-center"
        >
          <div className="h-10 w-[3px] rounded bg-[var(--border)]
                          group-hover:bg-[var(--accent)]" />
        </div>
      )}

      {/* Width collapses when the panel pops out, but the panel itself
          stays exactly here in the tree — moving it would unmount the
          player and drop the stream, which is the one thing a pop-out
          must not do. */}
      <div className="min-w-0 shrink-0"
           style={{ width: popped ? 0 : `calc(${100 - split}% - 0.75rem)` }}>
      {/* Outside the dimming above, deliberately: live video is not
          report data, and fading it whenever someone changes the range
          reads as the camera dropping out. */}
      <LiveCameraPanel
        cameraId={liveCameraId}
        cameraName={liveCameraId == null ? '' : cameraName(liveCameraId)}
        overlayEnabled={guardApp?.overlay_enabled}
        popped={popped}
        onTogglePop={() => setPopped((v) => !v)}
      />
      </div>
      </div>

      {viewing && (
        <EvidenceViewer
          title={`${VERDICT_LABEL[viewing.verdict] ?? viewing.verdict} · ${Math.round(viewing.score)}%`}
          subtitle={[
            viewing.ended_at ? new Date(viewing.ended_at).toLocaleString() : null,
            cameraName(viewing.camera_id),
            viewing.flagged ? 'scanner flagged' : null,
            viewing.steps_missing.length
              ? `missed ${viewing.steps_missing.join(', ')}` : null,
          ].filter(Boolean).join(' · ')}
          images={viewing.images}
          queryKeyPrefix={['screening-image', viewing.id]}
          fetchBlob={(name, signal) =>
            guardScanService.screeningImage(viewing.id, name, signal)}
          extra={<ScreeningClip screening={viewing} />}
          onClose={() => setViewing(null)}
        />
      )}

      {configOpen && guardApp && (
        <AppConfigModal app={guardApp} onClose={() => setConfigOpen(false)} />
      )}

      {reportOpen && (
        <ScreeningReport days={days} onClose={() => setReportOpen(false)} />
      )}
    </section>
  )
}

function toneFor(value: number | null): 'good' | 'warn' | 'bad' | undefined {
  if (value === null) return undefined
  if (value >= 95) return 'good'
  if (value >= 80) return 'warn'
  return 'bad'
}

function Tile({ label, value, hint, tone }: {
  label: string; value: string; hint?: string
  tone?: 'good' | 'warn' | 'bad'
}) {
  const colour = tone === 'good' ? 'var(--ok)'
    : tone === 'warn' ? 'var(--warn)'
    : tone === 'bad' ? 'var(--danger)'
    : undefined
  // A strip, not a tile: figure and name on ONE line. A number and its
  // label do not need a card of air around them, and the height those
  // four tiles were holding is the height the table and the camera
  // wanted. The explanation stays on hover.
  // Inset, not a card: these sit INSIDE the summary card now, and a
  // bordered box inside a bordered box reads as two panels where there
  // is one. `--panel` under the card's `--panel-2` recesses them just
  // enough to separate four figures from each other.
  //
  // The figure and its name share a baseline — a 20px number and an
  // 11px label aligned any other way read as a mistake.
  return (
    <div className="flex h-full flex-col justify-center rounded
                    bg-[var(--panel)] px-3 py-2">
      <div className="flex items-baseline gap-2">
        <div className="text-xl font-semibold leading-none tabular-nums"
             style={{ color: colour }}>
          {value}
        </div>
        <div className="min-w-0 truncate text-[11px] text-[var(--text-dim)]"
             title={hint} style={hint ? { cursor: 'help' } : undefined}>
          {label}
        </div>
      </div>
    </div>
  )
}

/**
 * One stacked bar per period: how many screenings, and how they went.
 *
 * Laid out, not drawn. An SVG with a viewBox has a FIXED aspect ratio,
 * so the plot could only ever be as tall as its width allowed — in a
 * card half the page wide that left the bars at a third of the height
 * available to them. Boxes with percentage heights take whatever the
 * card gives them, at any size, with no second copy of the geometry to
 * keep in step.
 *
 * Columns are capped and start at the LEFT: a bar's width carries no
 * quantity — only its height does — so two days must not draw as two
 * slabs half the card wide; and time reads left to right, so a series
 * that grows should extend rightwards rather than creep out from the
 * middle.
 */
function BucketChart({ buckets }: { buckets: Tally[] }) {
  const ordered = [...buckets].reverse()          // oldest left, like a calendar
  const top = Math.max(...ordered.map((b) => b.screenings), 1)
  const every = Math.ceil(ordered.length / 8)     // keep the axis readable
  // Bottom-up, because a flex column stacks its first child at the TOP
  // and the baseline of a bar chart is the bottom.
  const stack = [...VERDICT_ORDER].reverse()
  const [hover, setHover] = useState<number | null>(null)

  return (
    <div className="flex h-full flex-col">
      <div className="flex min-h-0 flex-1 items-end gap-1.5"
           role="img" aria-label="Screenings per period, by outcome">
        {ordered.map((bucket, i) => (
          <div
            key={bucket.key ?? i}
            // The whole column is the target, not just the coloured
            // part. A native `title` on the segments answered only if
            // you found the bar itself — and on a quiet day that is a
            // three-pixel stripe at the bottom of an empty column.
            className="relative flex h-full max-w-[46px] flex-1 flex-col justify-end gap-[2px]"
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover((h) => (h === i ? null : h))}
          >
            {hover === i && (
              // Anchored to whichever side keeps it on the card: a
              // centred tooltip on the first column hangs off the left
              // edge, which is where the series now starts.
              <BucketTip bucket={bucket}
                         align={i < ordered.length / 2 ? 'left' : 'right'} />
            )}
            {bucket.screenings === 0 ? (
              // A period nobody walked through is not a gap in the
              // chart: it is a fact, and it reads as one.
              <div className="h-[2px] rounded-sm bg-[var(--border)]" />
            ) : stack.map((verdict) => {
              const n = (bucket as unknown as Record<string, number>)[verdict] ?? 0
              if (!n) return null
              return (
                <div
                  key={verdict}
                  className="rounded-sm"
                  // A single screening in a busy period still has to be
                  // visible, hence the floor.
                  style={{
                    height: `${(n / top) * 100}%`,
                    minHeight: 3,
                    background: VERDICT_COLOUR[verdict],
                  }}
                />
              )
            })}
          </div>
        ))}
      </div>
      {/* The axis mirrors the bars' own widths, so a label always sits
          under the bar it names however many there are. */}
      <div className="mt-1 flex gap-1.5">
        {ordered.map((bucket, i) => (
          <div key={bucket.key ?? i}
               className="max-w-[46px] flex-1 truncate text-center text-[9px] leading-none text-[var(--text-dim)]">
            {/* The server sends an axis-sized label. The fallback is
                for a core older than that field, and is the crude thing
                this replaced: taking two words turned "Week 37, 2026"
                into "Week 37," and "September 2026" into an ellipsis. */}
            {i % every === 0
              ? bucket.short ?? (bucket.label ?? '').split(' ').slice(0, 2).join(' ')
              : ''}
          </div>
        ))}
      </div>
    </div>
  )
}

/**
 * What one period actually held, on hover.
 *
 * Every outcome with a count, not just the one under the pointer: the
 * question a stacked bar raises is "what is that period made of", and
 * answering it one band at a time makes the reader do the assembling.
 */
function BucketTip({ bucket, align }: { bucket: Tally; align: 'left' | 'right' }) {
  const rows = VERDICT_ORDER
    .map((v) => [v, (bucket as unknown as Record<string, number>)[v] ?? 0] as const)
    .filter(([, n]) => n > 0)
  return (
    <div
      // pointer-events-none so the tooltip can never sit between the
      // pointer and the column that spawned it, which is how a hover
      // tooltip ends up flickering.
      className={`pointer-events-none absolute bottom-full z-20 mb-1
                  whitespace-nowrap rounded border border-[var(--border)]
                  bg-[var(--bg-2)] px-2 py-1.5 shadow-lg ${
        align === 'left' ? 'left-0' : 'right-0'}`}
    >
      <div className="mb-0.5 text-[11px] font-medium text-[var(--text)]">
        {bucket.label}
      </div>
      <div className="text-[10px] text-[var(--text-dim)]">
        {bucket.screenings} screening{bucket.screenings === 1 ? '' : 's'}
        {bucket.compliance !== null && ` · ${bucket.compliance}% complete`}
      </div>
      {rows.map(([verdict, n]) => (
        <div key={verdict} className="mt-0.5 flex items-center gap-1.5 text-[10px]">
          <span className="inline-block h-2 w-2 rounded-sm"
                style={{ background: VERDICT_COLOUR[verdict] }} />
          <span className="text-[var(--text-dim)]">{VERDICT_LABEL[verdict]}</span>
          <span className="ml-auto pl-2 tabular-nums text-[var(--text)]">{n}</span>
        </div>
      ))}
    </div>
  )
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-x-3 gap-y-1">
      {VERDICT_ORDER.map((verdict) => (
        <span key={verdict} className="flex items-center gap-1 text-[10px]
                                       text-[var(--text-dim)]">
          <span className="inline-block h-2 w-2 rounded-sm"
                style={{ background: VERDICT_COLOUR[verdict] }} />
          {VERDICT_LABEL[verdict]}
        </span>
      ))}
    </div>
  )
}

function GuardTable({ guards }: { guards: Tally[] }) {
  const columns: Column<Tally>[] = [
    { key: 'guard', header: 'Guard', cell: (g) => g.label ?? '—' },
    {
      key: 'compliance', header: 'Compliance', width: 'w-[140px]',
      cellClassName: 'tabular-nums',
      cell: (g) => (
        <span style={{ color: g.compliance !== null && g.compliance < 80
          ? 'var(--danger)' : undefined }}>
          {pct(g.compliance)}
        </span>
      ),
    },
    { key: 'screenings', header: 'Screenings', width: 'w-[110px]',
      cellClassName: 'tabular-nums', cell: (g) => g.screenings },
    { key: 'missed', header: 'Not complete', width: 'w-[120px]',
      cellClassName: 'tabular-nums',
      cell: (g) => g.partial + g.incomplete + g.no_scan },
    { key: 'flagged', header: 'Scanner flags', width: 'w-[120px]',
      cellClassName: 'tabular-nums', cell: (g) => g.flagged },
  ]
  return (
    <DataTable<Tally>
      caption="Compliance by guard"
      columns={columns}
      rows={guards}
      rowKey={(g) => g.label ?? 'unknown'}
      empty={<EmptyState title="No screenings in this period" />}
      dense
      fillHeight={false}
    />
  )
}

function ScreeningTable({ rows, query, show, cameraName, onOpen, toolbar }: {
  rows: Screening[]
  query: {
    isPending: boolean; isFetching: boolean; isError: boolean
    error: unknown; refetch: () => unknown
  }
  show: 'all' | 'problems' | 'flagged'
  cameraName: (id: number | null) => string
  onOpen: (row: Screening) => void
  toolbar: ReactNode
}) {
  const emptyTitle = show === 'problems' ? 'Every scan was complete'
    : show === 'flagged' ? 'The scanner flagged nobody'
    : 'Nothing screened yet'
  const emptyHint = show === 'all'
    ? 'Screenings appear here as people are scanned at the entrance.'
    : 'Over the screenings in this range.'

  const columns: Column<Screening>[] = [
    {
      key: 'photo', header: '', srHeader: 'Who was scanned', width: 'w-[46px]',
      cell: (s) => {
        const name = s.images.includes('face') ? 'face'
          : s.images.includes('body') ? 'body' : s.images[0]
        if (!name) return null
        return (
          <AuthedImage
            queryKey={['screening-image', s.id, name]}
            fetchBlob={(signal) => guardScanService.screeningImage(s.id, name, signal)}
            alt={`Person screened at ${s.ended_at ?? ''}`}
            className="h-8 w-8 cursor-zoom-in rounded border border-[var(--border)] object-cover"
          />
        )
      },
    },
    {
      key: 'verdict', header: 'Outcome', width: 'w-[120px]',
      cell: (s) => (
        <Badge variant={VERDICT_BADGE[s.verdict] ?? 'neutral'}>
          {VERDICT_LABEL[s.verdict] ?? s.verdict}
        </Badge>
      ),
    },
    { key: 'score', header: 'Score', width: 'w-[72px]',
      cellClassName: 'tabular-nums', cell: (s) => `${Math.round(s.score)}%` },
    {
      // Fixed, and no longer the column that absorbs the slack: it is
      // a dash on most rows, and it was taking a third of the table to
      // say so while the camera name was clipped to "fake-showroom-…".
      key: 'missing', header: 'Missed', width: 'w-[150px]', className: 'truncate',
      cell: (s) => s.steps_missing.length
        ? <span className="text-[var(--text-dim)]">{s.steps_missing.join(', ')}</span>
        : <span className="text-[var(--text-dim)]">—</span>,
    },
    // The requirement names "camera location" on every record, and on a
    // multi-camera site a screening without its door is unattributable.
    { key: 'camera', header: 'Camera', hideBelow: 'sm',
      className: 'truncate',
      cellClassName: 'truncate text-[var(--text-dim)]',
      cell: (s) => cameraName(s.camera_id) },
    {
      key: 'guard', width: 'w-[120px]', hideBelow: 'lg',
      cellClassName: 'truncate text-[var(--text-dim)]',
      // Every row reads "—" until a duty roster exists, and the obvious
      // question is why. Answering it on the header costs no space; the
      // standing banner that used to answer it cost a band of the screen
      // and said the same thing every day.
      header: (
        <span title="The app tells guards apart within a run but cannot learn their names — that comes from a duty roster, which is not set up yet."
              style={{ cursor: 'help' }}>
          Guard
        </span>
      ),
      cell: (s) => s.guard_name || s.guard_key || '—',
    },
    {
      key: 'flagged', header: 'Scanner', width: 'w-[96px]',
      cell: (s) => s.flagged
        ? <Badge variant="critical">Flagged</Badge>
        : <span className="text-[var(--text-dim)]">—</span>,
    },
    { key: 'when', header: 'Date & time', width: 'w-[164px]',
      cellClassName: 'whitespace-nowrap text-[var(--text-dim)] tabular-nums',
      cell: (s) => s.ended_at ? new Date(s.ended_at).toLocaleString() : '—' },
  ]
  return (
    <DataTable<Screening>
      caption="Recent screenings"
      columns={columns}
      rows={rows}
      rowKey={(s) => s.id}
      isPending={query.isPending}
      isFetching={query.isFetching}
      isError={query.isError}
      error={query.error}
      errorTitle="Could not load the screenings"
      onRetry={() => query.refetch()}
      onRowClick={onOpen}
      toolbar={toolbar}
      empty={<EmptyState title={emptyTitle} description={emptyHint} />}
      dense
      fixed
      fillHeight
      minWidth="min-w-[760px]"
    />
  )
}
