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

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, ShieldCheck } from 'lucide-react'
import { AuthedImage } from '../components/AuthedImage'
import { Button, EmptyState } from '../components/ui'
import { DataTable, type Column } from '../components/ui/DataTable'
import { SegmentedControl } from '../components/ui/SegmentedControl'
import {
  guardScanService, VERDICT_LABEL,
  type ComplianceReport, type Screening, type Tally,
} from '../services/guardScanService'

// Verdicts are STATUS, not series identity, so they wear the status
// palette and every one of them is labelled — never colour alone.
const VERDICT_COLOUR: Record<string, string> = {
  compliant: 'var(--ok, #2f9e5e)',
  partial: 'var(--warn, #d4901f)',
  incomplete: 'var(--danger, #c4472f)',
  no_scan: 'var(--critical, #9b2226)',
}
const VERDICT_ORDER = ['compliant', 'partial', 'incomplete', 'no_scan'] as const

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

function pct(value: number | null): string {
  return value === null || value === undefined ? '—' : `${value}%`
}

export default function GuardCompliance() {
  const [period, setPeriod] = useState<'day' | 'week' | 'month'>('day')
  const [days, setDays] = useState(7)
  const [onlyFlagged, setOnlyFlagged] = useState(false)
  const tz = useMemo(() => -new Date().getTimezoneOffset(), [])

  const report = useQuery({
    queryKey: ['guardscan-report', period, days, tz],
    queryFn: async () => (await guardScanService.getReport({
      period, days, tz_offset_minutes: tz,
    })).data as ComplianceReport,
    refetchInterval: 30_000,
  })

  const screenings = useQuery({
    queryKey: ['guardscan-screenings', onlyFlagged],
    queryFn: async () => (await guardScanService.listScreenings({
      limit: 50, ...(onlyFlagged ? { flagged: true } : {}),
    })).data as { screenings: Screening[]; total: number },
    refetchInterval: 15_000,
  })

  const totals = report.data?.totals
  const rows = screenings.data?.screenings ?? []

  const download = async () => {
    const res = await guardScanService.exportCsv(days)
    const url = URL.createObjectURL(res.data as Blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `screenings-${days}d.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="space-y-5 p-4 md:p-6">
      <header className="flex flex-wrap items-center gap-3">
        <ShieldCheck size={18} className="text-[var(--accent)]" />
        <h1 className="text-lg font-semibold">Entry Screening</h1>
        <span className="text-xs text-[var(--text-dim)]">
          Did the guard scan every person, properly?
        </span>
        <div className="flex-1" />
        <SegmentedControl
          label="Range"
          showLabel={false}
          value={String(days)}
          options={RANGES}
          onChange={(v) => setDays(Number(v))}
        />
        <SegmentedControl
          label="Group by"
          showLabel={false}
          value={period}
          options={PERIODS}
          onChange={(v) => setPeriod(v as 'day' | 'week' | 'month')}
        />
        <Button variant="outline" size="sm" onClick={download}>
          <Download size={13} /> CSV
        </Button>
      </header>

      <div className="flex flex-wrap gap-3">
        <Tile label="Compliance" value={pct(totals?.compliance ?? null)}
              hint="Complete scans, of every screening"
              tone={toneFor(totals?.compliance ?? null)} />
        <Tile label="Screenings" value={String(totals?.screenings ?? 0)} />
        <Tile label="Scanner flags" value={String(totals?.flagged ?? 0)}
              tone={(totals?.flagged ?? 0) > 0 ? 'bad' : undefined} />
        <Tile label="Mean score"
              value={totals?.mean_score === null || totals?.mean_score === undefined
                ? '—' : String(totals.mean_score)} />
      </div>

      <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
        <h2 className="mb-1 text-sm font-semibold">How each period went</h2>
        <p className="mb-3 text-xs text-[var(--text-dim)]">
          Every screening, by outcome. Hover a band for the count.
        </p>
        {report.isPending ? (
          <div className="h-[120px] animate-pulse rounded bg-[var(--panel-2)]" />
        ) : report.data && report.data.buckets.length > 0 ? (
          <>
            <BucketChart buckets={report.data.buckets} />
            <Legend />
          </>
        ) : (
          <EmptyState
            title="No screenings recorded yet"
            description="Install the Guard Scan Compliance app and assign it an entrance camera."
          />
        )}
      </section>

      {(report.data?.guards.length ?? 0) > 0 && (
        <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
          <h2 className="mb-3 text-sm font-semibold">By guard</h2>
          <GuardTable guards={report.data!.guards} />
        </section>
      )}

      <section className="rounded-lg border border-[var(--border)] bg-[var(--panel)] p-4">
        <div className="mb-3 flex items-center gap-3">
          <h2 className="text-sm font-semibold">Recent screenings</h2>
          <div className="flex-1" />
          <label className="flex items-center gap-2 text-xs text-[var(--text-dim)]">
            <input
              type="checkbox"
              className="accent-[var(--accent)]"
              checked={onlyFlagged}
              onChange={(e) => setOnlyFlagged(e.target.checked)}
            />
            Scanner-flagged only
          </label>
        </div>
        <ScreeningTable rows={rows} isPending={screenings.isPending} />
      </section>
    </div>
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
  const colour = tone === 'good' ? 'var(--ok, #2f9e5e)'
    : tone === 'warn' ? 'var(--warn, #d4901f)'
    : tone === 'bad' ? 'var(--danger, #c4472f)'
    : undefined
  return (
    <div className="min-w-[150px] flex-1 rounded-lg border border-[var(--border)]
                    bg-[var(--panel)] px-4 py-3">
      <div className="text-2xl font-semibold tabular-nums" style={{ color: colour }}>
        {value}
      </div>
      <div className="text-xs text-[var(--text-dim)]">{label}</div>
      {hint && <div className="mt-0.5 text-[10px] text-[var(--text-dim)]">{hint}</div>}
    </div>
  )
}

/** One stacked bar per period: how many screenings, and how they went. */
function BucketChart({ buckets }: { buckets: Tally[] }) {
  const ordered = [...buckets].reverse()          // oldest left, like a calendar
  const top = Math.max(...ordered.map((b) => b.screenings), 1)
  const W = 720, H = 132, PAD = 4, LABEL_H = 16
  const plot = H - LABEL_H - PAD
  const bw = (W - 2 * PAD) / Math.max(ordered.length, 1)
  const every = Math.ceil(ordered.length / 8)     // keep the axis readable

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full"
           style={{ minWidth: 420, height: H }} role="img"
           aria-label="Screenings per period, by outcome">
        {ordered.map((bucket, i) => {
          const x = PAD + i * bw
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
                  <rect key={verdict} x={x + 1} y={y} width={Math.max(bw - 2, 1)}
                        height={h} rx="3" fill={VERDICT_COLOUR[verdict]}>
                    <title>{`${bucket.label}: ${n} ${VERDICT_LABEL[verdict].toLowerCase()}`}</title>
                  </rect>
                )
              })}
              {bucket.screenings === 0 && (
                <rect x={x + 1} y={plot + PAD - 2} width={Math.max(bw - 2, 1)}
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
          ? 'var(--danger, #c4472f)' : undefined }}>
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

function ScreeningTable({ rows, isPending }: {
  rows: Screening[]; isPending?: boolean
}) {
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
            className="h-8 w-8 rounded border border-[var(--border)] object-cover"
          />
        )
      },
    },
    {
      key: 'verdict', header: 'Outcome', width: 'w-[130px]',
      cell: (s) => (
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-full"
                style={{ background: VERDICT_COLOUR[s.verdict] ?? 'var(--text-dim)' }} />
          {VERDICT_LABEL[s.verdict] ?? s.verdict}
        </span>
      ),
    },
    { key: 'score', header: 'Score', width: 'w-[80px]',
      cellClassName: 'tabular-nums', cell: (s) => `${Math.round(s.score)}%` },
    {
      key: 'missing', header: 'Missed', className: 'truncate',
      cell: (s) => s.steps_missing.length
        ? <span className="text-[var(--text-dim)]">{s.steps_missing.join(', ')}</span>
        : <span className="text-[var(--text-dim)]">—</span>,
    },
    { key: 'guard', header: 'Guard', width: 'w-[140px]',
      cellClassName: 'truncate text-[var(--text-dim)]',
      cell: (s) => s.guard_name || s.guard_key || '—' },
    {
      key: 'flagged', header: 'Scanner', width: 'w-[96px]',
      cell: (s) => s.flagged
        ? <span style={{ color: 'var(--critical, #9b2226)' }}>Flagged</span>
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
      isPending={isPending}
      empty={<EmptyState
        title="Nothing screened yet"
        description="Screenings appear here as people are scanned at the entrance." />}
      dense
      fixed
      fillHeight={false}
      minWidth="min-w-[720px]"
    />
  )
}
