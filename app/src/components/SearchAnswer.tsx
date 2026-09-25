// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What search found, said rather than listed.
//
// A grid of result cards makes the operator do the tallying: which
// camera saw most of these, over what stretch of time, what did the
// skills actually claim. This block does that counting for them.
//
// Two things it deliberately does NOT do. It never says anything the
// server did not count — every number arrives in `answer`, nothing is
// derived here beyond picking a wording. And it never lets a page
// masquerade as a total: when the server counted ten of twelve thousand
// matches, the block says so in its own first line, because a summary
// read as a fact about the whole day is worse than no summary.
//
// The line that earns the block is `undescribed`. A visit no skill ran
// on carries no claims at all, so the fact that none of these say "red"
// is not evidence that nothing red came past — nothing looked. That
// distinction is invisible in a result grid and it is the difference
// between an answer and a guess.
import { useTranslation, useDateFormat } from '../i18n'

export type SearchAnswerData = {
  scope: 'page'
  shown: number
  total: number
  cameras: { id: number; name: string | null; count: number }[]
  camera_count: number
  first_at: string | null
  last_at: string | null
  claims: { kind: string; value: string; count: number }[]
  claim_count: number
  plates: string[]
  plate_count: number
  with_evidence: number
  undescribed: number
  /** Of `shown`: rows the words found, and rows here only because a
   *  vector resembled the query. Absent on a single-arm search. */
  matched_words?: number
  similar_only?: number
}

const Chip = ({ children }: { children: React.ReactNode }) => (
  <span className="inline-flex items-center rounded bg-[var(--panel)] border border-[var(--border)] px-1.5 py-0.5 text-xs">
    {children}
  </span>
)

export default function SearchAnswer({ answer }: { answer?: SearchAnswerData }) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  if (!answer || !answer.shown) return null

  const {
    shown, total, cameras, camera_count, first_at, last_at,
    claims, claim_count, plates, plate_count, with_evidence, undescribed,
    matched_words, similar_only,
  } = answer

  const from = first_at ? fmt.time(first_at) : ''
  const to = last_at ? fmt.time(last_at) : ''
  const when = !from ? '' : from === to
    ? t('search.answer.at', { time: from })
    : t('search.answer.between', { from, to })

  const more = (listed: number, counted: number) =>
    counted > listed ? t('search.answer.more', { count: counted - listed }) : null

  return (
    <div className="rounded border border-[var(--border)] bg-[var(--panel)]/40 px-3 py-2.5 space-y-2">
      <div className="text-xs text-[var(--text-dim)]">
        {t(total > shown ? 'search.answer.scopePage' : 'search.answer.scopeAll',
           { shown, total })}
        {when && <> · {when}</>}
      </div>

      {cameras.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-[var(--text-dim)]">{t('search.answer.cameras')}</span>
          {cameras.map((c) => (
            <Chip key={c.id}>
              {c.name || `#${c.id}`}<span className="text-[var(--text-dim)]"> ×{c.count}</span>
            </Chip>
          ))}
          {more(cameras.length, camera_count) && (
            <span className="text-xs text-[var(--text-dim)]">{more(cameras.length, camera_count)}</span>
          )}
        </div>
      )}

      {claims.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-[var(--text-dim)]">{t('search.answer.claims')}</span>
          {claims.map((c) => (
            <Chip key={`${c.kind}:${c.value}`}>
              {c.value}<span className="text-[var(--text-dim)]"> ×{c.count}</span>
            </Chip>
          ))}
          {more(claims.length, claim_count) && (
            <span className="text-xs text-[var(--text-dim)]">{more(claims.length, claim_count)}</span>
          )}
        </div>
      )}

      {plates.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-xs text-[var(--text-dim)]">{t('search.answer.plates')}</span>
          {plates.map((p) => <Chip key={p}><span className="font-mono">{p}</span></Chip>)}
          {more(plates.length, plate_count) && (
            <span className="text-xs text-[var(--text-dim)]">{more(plates.length, plate_count)}</span>
          )}
        </div>
      )}

      <div className="text-xs text-[var(--text-dim)] space-y-1">
        <div>
          {t('search.answer.withEvidence', { count: with_evidence, shown })}
        </div>
        {/* A yes/no question turns on this line. Five visits that merely
            LOOK like the query and none described as it is a "no", and a
            grid of five cannot say so. */}
        {(similar_only ?? 0) > 0 && (
          <div className="text-[var(--warn,var(--text-dim))]">
            {t('search.answer.similarOnly', {
              count: similar_only ?? 0, matched: matched_words ?? 0,
            })}
          </div>
        )}
        {/* Stated even when it is the only thing this block says, because
            "nothing looked" is an answer the grid cannot give. */}
        {undescribed > 0 && (
          <div className="text-[var(--warn,var(--text-dim))]">
            {t('search.answer.undescribed', { count: undescribed })}
            {' '}
            {t('search.answer.undescribedWhy')}
          </div>
        )}
      </div>
    </div>
  )
}
