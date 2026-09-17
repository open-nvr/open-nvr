/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The entry-screening report: one page a showroom manager can file.
//
// The CSV next to it is a data dump — 20,000 rows of session ids, for a
// spreadsheet. This answers the questions a manager actually asks, in
// the order they ask them: how are we doing, is it getting better or
// worse, what exactly are the guards getting wrong, and when.
//
// "Which surfaces get missed" is the line to read first. A compliance
// percentage is a score; "the back is missed four times more often than
// anything else" is an instruction you can give at tomorrow's briefing.

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { extractApiError } from '../../lib/apiError'
import { PrintSheet } from '../../components/PrintSheet'
import { Skeleton } from '../../components/ui'
import {
  guardScanService, VERDICT_LABEL,
  type ComplianceReport, type Tally,
} from '../../services/guardScanService'

// Rows the report groups by. Deliberately fewer choices than the page:
// a printed document wants a period someone will recognise, not a knob.
const PERIODS = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
]

const NO_GUARD = 'unidentified'
const num = { fontVariantNumeric: 'tabular-nums' } as const

function pct(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : `${value}%`
}

export function ScreeningReport({ days: initialDays, onClose }: {
  days: number
  onClose: () => void
}) {
  const [days, setDays] = useState(initialDays)
  const tz = useMemo(() => -new Date().getTimezoneOffset(), [])

  const query = useQuery({
    queryKey: ['guardscan-report-sheet', days, tz],
    queryFn: async () => (await guardScanService.getReport({
      // Daily rows: a printed trend wants one line per day, and week or
      // month buckets over 90 days give three rows and say nothing.
      days, period: 'day', tz_offset_minutes: tz,
    })).data as ComplianceReport,
    retry: 0,
    staleTime: 60_000,
  })
  const report = query.data
  const totals = report?.totals
  const guards = (report?.guards ?? []).filter((g) => g.label !== NO_GUARD)
  const worstMiss = report?.missed?.[0]

  return (
    <PrintSheet
      ready={!!report}
      onClose={onClose}
      controls={
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          aria-label="Report period"
          className="rounded border border-neutral-300 bg-white px-2 py-1.5 text-sm"
        >
          {PERIODS.map((p) => (
            <option key={p.days} value={p.days}>{p.label}</option>
          ))}
        </select>
      }
    >
      <h1 className="text-xl font-semibold">Entry Screening Report</h1>
      <p className="mt-1 text-sm text-neutral-500">
        Last {days} days · generated {new Date().toLocaleString()} · OpenNVR
      </p>

      {query.isPending ? (
        <Skeleton className="h-64" />
      ) : query.isError ? (
        <div className="text-sm text-neutral-500">
          {extractApiError(query.error, 'Could not build the report.')}
        </div>
      ) : !totals || totals.screenings === 0 ? (
        <p className="mt-6 text-sm text-neutral-500">
          Nothing was screened in this period.
        </p>
      ) : (
        <>
          <div className="mt-6 grid grid-cols-4 gap-4">
            <Figure label="Compliance" value={pct(totals.compliance)}
                    note="complete scans, of every screening" />
            <Figure label="Screenings" value={String(totals.screenings)}
                    note="people scanned at the door" />
            <Figure label="Scanner flags" value={String(totals.flagged)}
                    note="the wand's indicator lit" />
            <Figure label="Mean score"
                    value={totals.mean_score === null ? '—' : String(totals.mean_score)}
                    note="average of every screening" />
          </div>

          {/* The headline, in a sentence, because a number in a table is
              something to look up and a sentence is something to act on. */}
          {worstMiss && (
            <p className="mt-6 text-sm">
              The surface missed most often is{' '}
              <strong>{worstMiss.step.toLowerCase()}</strong> — skipped{' '}
              <strong>{worstMiss.count}</strong>{' '}
              {worstMiss.count === 1 ? 'time' : 'times'} in this period.
            </p>
          )}

          <Section title="What the guards miss"
                   note="Counted across every screening that was not complete.">
            {report?.missed?.length ? (
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-neutral-300 text-left">
                    <Th>Surface</Th><Th right>Times missed</Th><Th right>Share of screenings</Th>
                  </tr>
                </thead>
                <tbody>
                  {report.missed.map((m) => (
                    <tr key={m.step} className="border-b border-neutral-100">
                      <Td>{m.step}</Td>
                      <Td right>{m.count}</Td>
                      <Td right>
                        {Math.round((100 * m.count) / totals.screenings)}%
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="text-sm text-neutral-500">
                Nothing was missed: every screening covered all four surfaces.
              </p>
            )}
          </Section>

          <Section title="By hour of day"
                   note="Local time. Only hours that saw somebody are listed.">
            <OutcomeTable rows={report?.hours ?? []} head="Hour"
                          name={(t) => (t as Tally & { hour: number }).label ?? ''} />
          </Section>

          <Section title="Day by day">
            <OutcomeTable rows={[...(report?.buckets ?? [])].reverse()} head="Day"
                          name={(t) => t.label ?? ''} />
          </Section>

          {guards.length > 0 && (
            <Section title="By guard">
              <OutcomeTable rows={guards} head="Guard" name={(t) => t.label ?? ''} />
            </Section>
          )}

          {(report?.cameras.length ?? 0) > 1 && (
            <Section title="By camera">
              <OutcomeTable rows={report!.cameras} head="Camera"
                            name={(t) => t.label ?? ''} />
            </Section>
          )}

          <p className="mt-8 border-t border-neutral-200 pt-3 text-xs text-neutral-500">
            Compliance is complete scans over every screening recorded, clean ones
            included — a rate counted only over complaints would have no denominator.
            A period with nothing screened shows a dash rather than 100%.
          </p>
        </>
      )}
    </PrintSheet>
  )
}

function Figure({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div>
      <div className="text-2xl font-semibold" style={num}>{value}</div>
      <div className="text-sm font-medium">{label}</div>
      <div className="text-xs text-neutral-500">{note}</div>
    </div>
  )
}

function Section({ title, note, children }: {
  title: string; note?: string; children: React.ReactNode
}) {
  return (
    // break-inside-avoid so a table is not sliced across two sheets of
    // paper, which is the difference between a report and a printout.
    <section className="mt-8 break-inside-avoid">
      <h2 className="text-base font-semibold">{title}</h2>
      {note && <p className="mb-2 text-xs text-neutral-500">{note}</p>}
      {children}
    </section>
  )
}

/** The same five columns wherever screenings are grouped by something. */
function OutcomeTable({ rows, head, name }: {
  rows: Tally[]; head: string; name: (t: Tally) => string
}) {
  if (!rows.length) {
    return <p className="text-sm text-neutral-500">Nothing in this period.</p>
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="border-b border-neutral-300 text-left">
          <Th>{head}</Th>
          <Th right>Screenings</Th>
          <Th right>{VERDICT_LABEL.compliant}</Th>
          <Th right>Not complete</Th>
          <Th right>Flags</Th>
          <Th right>Compliance</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((t, i) => (
          <tr key={t.key ?? t.label ?? i} className="border-b border-neutral-100">
            <Td>{name(t)}</Td>
            <Td right>{t.screenings}</Td>
            <Td right>{t.compliant}</Td>
            <Td right>{t.partial + t.incomplete + t.no_scan}</Td>
            <Td right>{t.flagged}</Td>
            <Td right>{pct(t.compliance)}</Td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function Th({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <th className={`py-1.5 font-medium ${right ? 'text-right' : ''}`}>{children}</th>
  )
}

function Td({ children, right }: { children: React.ReactNode; right?: boolean }) {
  return (
    <td className={`py-1.5 ${right ? 'text-right' : ''}`} style={right ? num : undefined}>
      {children}
    </td>
  )
}
