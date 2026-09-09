import type { ReactNode } from 'react'
import { clsx } from 'clsx'
import { Check } from 'lucide-react'
import { Button, EmptyState, SeverityBadge } from '../ui'
import { DataTable, type Column } from '../ui/DataTable'
import {
  alarmSeenAt, alarmSeenTitle, type InboxAlert,
} from '../../services/alertsInboxService'

/**
 * The one alarm table. Both surfaces that show alarms render through it.
 *
 * They used to share their data layer and duplicate the entire
 * presentation layer — severity pill, title+description cell, the Seen
 * cell, the Ack button, the unacknowledged toggle — which is how they
 * came to disagree about WHICH timestamp to show until #451 fixed both
 * by hand. Anything genuinely different between them is a prop here;
 * everything else is shared by construction.
 */

/** `cam3` / `cam-3` / `3` -> 3. The handle is a producer-supplied string
 *  with no FK, so this is a parse, not a lookup. */
export function cameraIdFromHandle(handle: string | null): number | null {
  if (!handle) return null
  const n = Number(String(handle).replace(/^cam-?/i, ''))
  return Number.isFinite(n) ? n : null
}

export type AlarmsTableProps = {
  rows: InboxAlert[]
  /** Alerts & Incidents shows it; the Vehicles tab is one source already. */
  showSource?: boolean
  /** Resolves a camera handle to its display name. */
  cameraLabel?: (handle: string | null) => string
  onAck: (ids: number[]) => void
  ackPending?: boolean
  /** Ticked row ids. Omit the selection props for a read-only table. */
  selected?: Set<number>
  onToggle?: (id: number) => void
  onToggleAll?: (on: boolean) => void
  emptyTitle: string
  emptyDescription?: string
  emptyAction?: ReactNode
  caption: string
  isPending?: boolean
  isFetching?: boolean
  isError?: boolean
  error?: unknown
  onRetry?: () => void
  footer?: ReactNode
  toolbar?: ReactNode
  fillHeight?: boolean
}

export function AlarmsTable({
  rows, showSource = false, cameraLabel, onAck, ackPending = false,
  selected, onToggle, onToggleAll,
  emptyTitle, emptyDescription, emptyAction, caption,
  isPending, isFetching, isError, error, onRetry, footer, toolbar,
  fillHeight = true,
}: AlarmsTableProps) {
  const selectable = !!selected && !!onToggle
  const pageIds = rows.map((a) => a.id)
  const allOnPage = selectable && pageIds.every((id) => selected!.has(id))
  const someOnPage = selectable && pageIds.some((id) => selected!.has(id))

  const columns: Column<InboxAlert>[] = [
    ...(selectable ? [{
      key: 'select',
      // A real checkbox in the header, with indeterminate set through a
      // ref — the attribute does not exist in HTML, only the property.
      header: (
        <input
          type="checkbox"
          className="accent-[var(--accent)] align-middle"
          aria-label="Select all on this page"
          checked={pageIds.length > 0 && allOnPage}
          ref={(el) => { if (el) el.indeterminate = someOnPage && !allOnPage }}
          onChange={(e) => onToggleAll?.(e.target.checked)}
        />
      ),
      width: 'w-10',
      cell: (a: InboxAlert) => (
        <input
          type="checkbox"
          className="accent-[var(--accent)] align-middle"
          aria-label={`Select: ${a.title}`}
          checked={selected!.has(a.id)}
          onChange={() => onToggle?.(a.id)}
          onClick={(e) => e.stopPropagation()}
        />
      ),
    } as Column<InboxAlert>] : []),
    {
      // Left gutter, like the reads table, and always visible: an action
      // an operator performs on most rows should not require discovering
      // it by hovering. It is a dim icon until you approach it, so it
      // stays quiet without hiding.
      key: 'ack', header: '', srHeader: 'Acknowledge',
      width: 'w-10', className: 'whitespace-nowrap',
      isAction: true,
      cell: (a) => a.acknowledged_at ? null : (
        <button
          type="button"
          title="Acknowledge this alarm"
          aria-label={`Acknowledge: ${a.title}`}
          className="rounded p-1 text-[var(--text-dim)] hover:bg-[var(--panel-2)] hover:text-[var(--text)]"
          disabled={ackPending}
          onClick={() => onAck([a.id])}
        >
          <Check size={15} />
        </button>
      ),
    },
    {
      key: 'severity', header: 'Severity', width: 'w-[92px]',
      cell: (a) => (
        <SeverityBadge
          severity={a.severity}
          className={a.acknowledged_at ? 'opacity-60' : undefined}
        />
      ),
    },
    {
      // The flexible column — no width, so it takes the slack. Its
      // description truncates because under table-fixed a long one wraps
      // and makes the row taller than its neighbours.
      key: 'alarm', header: 'Alarm',
      // One line, not two. The description was a second row of 11px text
      // under every title, which doubled the row height to show a
      // sentence that mostly restates the title. Inline and dim, it
      // still reads as the detail it is — and twice as many alarms fit.
      cell: (a) => (
        <div
          className="truncate"
          title={a.description ? `${a.title} — ${a.description}` : a.title}
        >
          <span className={a.acknowledged_at ? 'font-normal' : 'font-semibold'}>
            {a.title}
          </span>
          {a.description && (
            <span className="ml-1.5 text-[var(--text-dim)]">
              {/* The description already ends in a full stop, which
                  would read as "(….)" once bracketed. */}
              ({a.description.replace(/\.\s*$/, '')})
            </span>
          )}
        </div>
      ),
    },
    ...(showSource ? [{
      key: 'source', header: 'Source', hideBelow: 'lg' as const,
      width: 'w-[168px]', cellClassName: 'text-[var(--text-dim)] truncate',
      cell: (a: InboxAlert) => a.source_name || '—',
    }] : []),
    {
      key: 'camera', header: 'Camera', hideBelow: 'sm', width: 'w-[140px]',
      className: 'truncate',
      // The reads table resolves a friendly name and this printed the raw
      // handle, so one screen called the same camera `Demo IN` and `cam1`.
      cell: (a) => cameraLabel?.(a.camera_id) ?? (a.camera_id || '—'),
    },
    {
      key: 'seen', header: 'Date & time', width: 'w-[164px]',
      className: 'whitespace-nowrap',
      cellClassName: 'text-[var(--text-dim)] tabular-nums',
      cell: (a) => <span title={alarmSeenTitle(a)}>{alarmSeenAt(a)}</span>,
    },
  ]

  return (
    <DataTable<InboxAlert>
      caption={caption}
      columns={columns}
      rows={rows}
      rowKey={(a) => a.id}
      // State lives in the ROW, not a column of words. An acknowledged
      // alarm recedes and an unacknowledged one keeps full contrast —
      // the same read-vs-unread cue a mail list uses, which frees the
      // width a "Status" column was spending to repeat it.
      // ONE VISUAL CHANNEL PER MEANING. That is the whole rule here,
      // and breaking it is what made the earlier attempts look wrong:
      //
      //   text  = state        (acknowledged or not)
      //   bg    = interaction  (hover, selection)
      //
      // Before this, three rules competed for the background — the
      // zebra stripe, the hover, and an accent tint for state. Zebra and
      // tint are both plain classes of equal specificity, so which one
      // painted a given row came down to stylesheet order rather than
      // intent, and the state cue appeared on some rows and not others.
      //
      // Text also survives both themes, which a background tint did not:
      // --bg-2 is a hair off --panel in dark, and in light a grey wash
      // would darken the row that needs the MOST attention.
      striped={false}
      rowClassName={(a) => clsx(
        a.acknowledged_at
          // Recedes on both grounds: the dim token plus a little
          // transparency, which also takes the severity badge and the
          // description down with it.
          ? 'text-[var(--text-dim)] opacity-70'
          : 'text-[var(--text)]',
        // Selection outranks hover — the row you ticked must stay
        // obviously ticked while the pointer moves over it.
        selectable && selected!.has(a.id) && '!bg-[var(--accent)]/15',
      )}
      isPending={isPending}
      isFetching={isFetching}
      isError={isError}
      error={error}
      errorTitle="Could not load alarms"
      onRetry={onRetry}
      empty={<EmptyState title={emptyTitle} description={emptyDescription} action={emptyAction} />}
      footer={footer}
      toolbar={toolbar}
      fillHeight={fillHeight}
      fixed
      dense
      minWidth="min-w-[640px]"
    />
  )
}

/**
 * The strip that appears once rows are ticked. Mail-client behaviour:
 * selecting every row on the page offers to extend the action to
 * everything the filter matches, because "select all" on page 1 of 46
 * meaning 25 rows is the single most common bulk-action surprise.
 *
 * The escalation is not a bigger list of ids — those rows are not in the
 * browser. It sends the FILTER to the server, which is what the ack
 * endpoint's source_name/severity form is for.
 */
export function AlarmsSelectionBar({
  count, allOnPage, allMatching, matchingTotal, label,
  onSelectAllMatching, onClear, onAck, ackPending,
}: {
  count: number
  allOnPage: boolean
  allMatching: boolean
  matchingTotal?: number
  label?: string
  onSelectAllMatching: () => void
  onClear: () => void
  onAck: () => void
  ackPending?: boolean
}) {
  if (!count && !allMatching) return null
  const noun = label ?? 'alarms'
  return (
    // Inline, not a strip of its own. A full-width bar appearing above
    // the filters pushed the whole table down the moment a checkbox was
    // ticked — the rows moving under the pointer that just clicked one.
    // The filter row already had empty space between the chips and the
    // pager, which is where a contextual action belongs.
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-[var(--border)] bg-[var(--panel-2)] px-2 py-1 text-xs">
      <span className="font-medium text-[var(--text)]">
        {allMatching
          ? `All ${matchingTotal ?? ''} ${noun} selected`
          : `${count} selected`}
      </span>
      {!allMatching && allOnPage && typeof matchingTotal === 'number' && matchingTotal > count && (
        <button type="button" onClick={onSelectAllMatching}
                className="underline text-[var(--accent)] hover:brightness-110">
          Select all {matchingTotal} {noun}
        </button>
      )}
      <Button variant="outline" size="sm" disabled={ackPending} onClick={onAck}>
        <Check size={13} /> Acknowledge
      </Button>
      <button type="button" onClick={onClear}
              className="text-[var(--text-dim)] hover:text-[var(--text)]">
        Clear
      </button>
    </div>
  )
}

/**
 * The filter set both alarm views show, so neither can grow a control
 * the other lacks. It renders INSIDE the table's toolbar, beside the
 * pagination — the same shape the plate-reads table uses — rather than
 * as a separate bar above a separately-bordered table.
 *
 * Severity is a segmented control rather than a row of buttons for the
 * same reason the reads table's time range is: it is one choice from a
 * fixed set, and five outlined buttons read as five separate actions.
 */
export const ALARM_SEVERITIES = ['critical', 'high', 'medium', 'low'] as const

export function AlarmsFilters({
  onlyUnacked, onToggleUnacked, severity, onSeverity, children,
}: {
  onlyUnacked: boolean
  onToggleUnacked: () => void
  severity: string | null
  onSeverity: (s: string | null) => void
  children?: ReactNode
}) {
  return (
    <>
      <Button
        size="sm"
        variant={onlyUnacked ? 'default' : 'outline'}
        aria-pressed={onlyUnacked}
        onClick={onToggleUnacked}
      >
        Unacknowledged only
      </Button>
      <div className="flex overflow-hidden rounded border border-[var(--border)]"
           role="group" aria-label="Severity">
        {([null, ...ALARM_SEVERITIES] as const).map((sev) => (
          <button
            key={sev ?? 'all'}
            type="button"
            aria-pressed={severity === sev}
            onClick={() => onSeverity(sev)}
            className={`px-2.5 py-1 text-xs capitalize ${severity === sev
              ? 'bg-[var(--panel-2)] font-semibold text-[var(--text)]'
              : 'text-[var(--text-dim)] hover:text-[var(--text)]'}`}
          >
            {sev ?? 'All'}
          </button>
        ))}
      </div>
      {children}
    </>
  )
}
