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
import { InfoTip } from './ui/InfoTip'

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

/**
 * One line in the results toolbar — where and when — with the rest one
 * hover away. The full block used to sit above the grid and push the
 * results off the screen; the counts it carried are all still here.
 */
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
    counted > listed ? ` ${t('search.answer.more', { count: counted - listed })}` : ''

  const where = cameras.length === 0 ? '' : camera_count === 1
    ? (cameras[0].name || `#${cameras[0].id}`)
    : t('search.answer.onCameras', { count: camera_count })

  // "Nothing looked" and "only similar" are answers a grid cannot give,
  // so the icon that carries them turns amber when either applies.
  const caveat = undescribed > 0 || (similar_only ?? 0) > 0

  return (
    <span className="inline-flex min-w-0 items-center gap-1.5 text-[var(--text-dim)]">
      <span className="truncate">
        {[where, when].filter(Boolean).join(' · ')}
      </span>
      <span className={caveat ? 'text-[var(--badge-warning-text)] inline-flex' : 'inline-flex'}>
        <InfoTip label={t('search.answer.details')}>
          <div className="space-y-1.5">
            <div className="text-[var(--text-dim)]">
              {t(total > shown ? 'search.answer.scopePage' : 'search.answer.scopeAll', { shown, total })}
            </div>
            {cameras.length > 0 && (
              <div>
                <b>{t('search.answer.cameras')}:</b>{' '}
                {cameras.map((c) => `${c.name || `#${c.id}`} (${c.count})`).join(', ')}
                {more(cameras.length, camera_count)}
              </div>
            )}
            {claims.length > 0 && (
              <div>
                <b>{t('search.answer.claims')}:</b>{' '}
                {claims.map((c) => `${c.value} (${c.count})`).join(', ')}
                {more(claims.length, claim_count)}
              </div>
            )}
            {plates.length > 0 && (
              <div>
                <b>{t('search.answer.plates')}:</b>{' '}
                <span className="font-mono">{plates.join(', ')}</span>
                {more(plates.length, plate_count)}
              </div>
            )}
            <div>{t('search.answer.withEvidence', { count: with_evidence, shown })}</div>
            {(similar_only ?? 0) > 0 && (
              <div className="text-[var(--badge-warning-text)]">
                {t('search.answer.similarOnly', { count: similar_only ?? 0, matched: matched_words ?? 0 })}
              </div>
            )}
            {undescribed > 0 && (
              <div className="text-[var(--badge-warning-text)]">
                {t('search.answer.undescribed', { count: undescribed })}{' '}
                {t('search.answer.undescribedWhy')}
              </div>
            )}
          </div>
        </InfoTip>
      </span>
    </span>
  )
}
