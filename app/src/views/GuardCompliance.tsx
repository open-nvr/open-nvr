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

import { useMemo, useState, type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Settings2, ShieldCheck } from 'lucide-react'
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
  })

  const totals = report.data?.totals
  const list = screenings.data?.screenings ?? []
  // Every screening is attributed to nobody until a duty roster exists,
  // so the per-guard breakdown is only worth a table once at least one
  // row names a real person.
  const allGuards = report.data?.guards ?? []
  const guards = allGuards.filter((g) => g.label !== NO_GUARD)
  const guardsPending = allGuards.length > 0 && guards.length === 0

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
    <section className="space-y-4">
      <PageHeader
        title={
          <span className="inline-flex items-center gap-2">
            <ShieldCheck size={18} className="text-[var(--accent)]" /> Entry Screening
          </span>
        }
        description="Did the guard scan every person, properly? Every screening is recorded — the clean ones included — because that is what makes the compliance figure mean something."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {guardApp && canConfigure && (
              <Button variant="outline" size="sm" onClick={() => setConfigOpen(true)}>
                <Settings2 size={13} /> Configure
              </Button>
            )}
            <Button variant="outline" size="sm" onClick={download} disabled={exporting}>
              <Download size={13} /> {exporting ? 'Exporting…' : 'CSV'}
            </Button>
          </div>
        }
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
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

      <Card>
        <CardContent>
          {/* The range and the grouping only ever governed this chart
              and the figures above it, so they live on it. In the page
              header they read as page-wide controls and cost a whole
              band of vertical space that the table below needed. */}
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-2">
            <div className="min-w-0">
              <h3 className="text-sm font-semibold">How each period went</h3>
              <p className="text-xs text-[var(--text-dim)]">
                Every screening, by outcome. Hover a band for the count.
              </p>
            </div>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <SegmentedControl
                label="Range"
                showLabel={false}
                value={String(days)}
                options={RANGES}
                onChange={(v) => { setDays(Number(v)); resetPage() }}
              />
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
            <Skeleton className="h-[104px]" />
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
              <BucketChart buckets={report.data.buckets} />
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
      {guardsPending && (
        <Card>
          <CardContent className="py-3 text-xs text-[var(--text-dim)]">
            <span className="font-medium text-[var(--text)]">
              By guard: not yet available.
            </span>{' '}
            The app can tell one guard from another within a run but cannot learn their
            name — that comes from a duty roster, which is not set up. Until then every
            screening is recorded against nobody.
          </CardContent>
        </Card>
      )}

      <Card>
        <CardContent className="pb-3">
          <h3 className="text-sm font-semibold">Recent screenings</h3>
        </CardContent>
        <div className="px-4 pb-4">
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
        </div>
      </Card>

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
          onClose={() => setViewing(null)}
        />
      )}

      {configOpen && guardApp && (
        <AppConfigModal app={guardApp} onClose={() => setConfigOpen(false)} />
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
  // Label above figure, and the explanation on hover rather than on a
  // third line: four tiles at three lines each pushed the screenings
  // table off the bottom of the screen, and the table is what an
  // operator came for.
  return (
    <Card>
      <CardContent className="px-3 py-2">
        <div className="truncate text-[11px] text-[var(--text-dim)]"
             title={hint} style={hint ? { cursor: 'help' } : undefined}>
          {label}
        </div>
        <div className="text-xl font-semibold leading-tight tabular-nums"
             style={{ color: colour }}>
          {value}
        </div>
      </CardContent>
    </Card>
  )
}

/** One stacked bar per period: how many screenings, and how they went. */
function BucketChart({ buckets }: { buckets: Tally[] }) {
  const ordered = [...buckets].reverse()          // oldest left, like a calendar
  const top = Math.max(...ordered.map((b) => b.screenings), 1)
  const W = 720, H = 110, PAD = 4, LABEL_H = 15
  const plot = H - LABEL_H - PAD
  // A bar is a QUANTITY, and its width carries none of it — only its
  // height does. Two days of data divided across the full width gave
  // two 350px slabs that read as a colour-blocked background rather
  // than a chart. Cap the width and pin the series to the left, so one
  // day and thirty days are drawn in the same units and a week's
  // history does not change shape as it fills up.
  const SLOT = 44
  const slot = Math.min(SLOT, (W - 2 * PAD) / Math.max(ordered.length, 1))
  const bw = slot * 0.72                          // a gap between bars
  const every = Math.ceil(ordered.length / 8)     // keep the axis readable

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full"
           style={{ minWidth: 420, height: H }} role="img"
           aria-label="Screenings per period, by outcome">
        {ordered.map((bucket, i) => {
          const x = PAD + i * slot + (slot - bw) / 2
          let y = plot + PAD
          return (
            <g key={bucket.key ?? i}>
              {VERDICT_ORDER.map((verdict) => {
                const n = (bucket as unknown as Record<string, number>)[verdict] ?? 0
                if (!n) return null
                // 2px of surface between segments, so adjacent bands
                // read as separate quantities rather than one block.
                const h = Math.max((n / top) * plot - 2, 2)
                y -= h + 2
                return (
                  <rect key={verdict} x={x} y={y} width={Math.max(bw, 1)}
                        height={h} rx="2" fill={VERDICT_COLOUR[verdict]}>
                    <title>{`${bucket.label}: ${n} ${VERDICT_LABEL[verdict].toLowerCase()}`}</title>
                  </rect>
                )
              })}
              {bucket.screenings === 0 && (
                <rect x={x} y={plot + PAD - 2} width={Math.max(bw, 1)}
                      height={2} rx="1" fill="var(--border)">
                  <title>{`${bucket.label}: nothing screened`}</title>
                </rect>
              )}
              {i % every === 0 && (
                <text x={x + bw / 2} y={H - 4} textAnchor="middle" fontSize="9"
                      fill="var(--text-dim)">
                  {(bucket.label ?? '').split(' ').slice(0, 2).join(' ')}
                </text>
              )}
            </g>
          )
        })}
      </svg>
    </div>
  )
}

function Legend() {
  return (
    <div className="mt-2 flex flex-wrap gap-4">
      {VERDICT_ORDER.map((verdict) => (
        <span key={verdict} className="flex items-center gap-1.5 text-[11px]
                                       text-[var(--text-dim)]">
          <span className="inline-block h-2.5 w-2.5 rounded-sm"
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
      key: 'missing', header: 'Missed', className: 'truncate',
      cell: (s) => s.steps_missing.length
        ? <span className="text-[var(--text-dim)]">{s.steps_missing.join(', ')}</span>
        : <span className="text-[var(--text-dim)]">—</span>,
    },
    // The requirement names "camera location" on every record, and on a
    // multi-camera site a screening without its door is unattributable.
    { key: 'camera', header: 'Camera', width: 'w-[140px]', hideBelow: 'sm',
      cellClassName: 'truncate text-[var(--text-dim)]',
      cell: (s) => cameraName(s.camera_id) },
    { key: 'guard', header: 'Guard', width: 'w-[120px]', hideBelow: 'lg',
      cellClassName: 'truncate text-[var(--text-dim)]',
      cell: (s) => s.guard_name || s.guard_key || '—' },
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
      fillHeight={false}
      minWidth="min-w-[860px]"
    />
  )
}
