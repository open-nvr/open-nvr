/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The printed attendance sheet: white paper and dark ink whatever the
// theme (so this file is exempt from the theme-token lint rule, like the
// screening report), one row per person, one column per session.

import { PrintSheet } from '../../components/PrintSheet'
import type { MarkStatus, Report } from '../../services/attendanceService'
import { pct } from './shared'

const LETTER: Record<MarkStatus, string> = { PRESENT: 'P', LATE: 'L', ABSENT: 'A', EXCUSED: 'E' }

export function ReportPrint({ report, onClose }: { report: Report; onClose: () => void }) {
  const cell = 'border border-neutral-300 px-2'
  return (
    <PrintSheet onClose={onClose}>
      <h1 className="text-lg font-semibold">Attendance — {report.group.name}</h1>
      <p className="mb-3 text-sm text-neutral-600">
        {report.from} to {report.to} · times in {report.group.timezone}
      </p>
      <table className="text-sm">
        <thead>
          <tr className="text-left">
            <th className={cell}>Name</th>
            <th className={cell}>Ref.</th>
            {report.sessions.map((s) => (
              <th key={s.id} className={`${cell} whitespace-nowrap text-center`}>
                {s.local_date.slice(5)}<br />{s.start}{s.no_coverage ? ' *' : ''}
              </th>
            ))}
            <th className={cell}>P</th><th className={cell}>L</th>
            <th className={cell}>A</th><th className={cell}>E</th><th className={cell}>Rate</th>
          </tr>
        </thead>
        <tbody>
          {report.people.map((p) => (
            <tr key={p.person_id}>
              <td className={`${cell} whitespace-nowrap`}>{p.display_name}{p.member ? '' : ' (left)'}</td>
              <td className={cell}>{p.external_ref ?? ''}</td>
              {report.sessions.map((s) => {
                const m = p.marks[String(s.id)]
                return <td key={s.id} className={`${cell} text-center`}>{m ? LETTER[m] : ''}</td>
              })}
              <td className={cell}>{p.present}</td><td className={cell}>{p.late}</td>
              <td className={cell}>{p.absent}</td><td className={cell}>{p.excused}</td>
              <td className={cell}>{pct(p.rate)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.sessions.some((s) => s.no_coverage) && (
        <p className="mt-2 text-xs text-neutral-600">
          * Nothing was watched in this session (camera or app down); its automatic marks are
          not counted in the totals.
        </p>
      )}
    </PrintSheet>
  )
}
