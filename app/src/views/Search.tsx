/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 *
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 *
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

// Search — "find me the red truck at the dock yesterday", over the
// canonical event store.
//
// Three things make this page rather than a box that returns JSON:
//
// 1. The interpretation is shown as chips, and each one can be removed.
//    Natural-language search fails by parsing a query wrongly and then
//    answering confidently with nothing; a chip row turns that dead end
//    into one click. Removing a chip re-runs with parse=false and the
//    remaining chips as explicit filters — the API's own contract.
// 2. Every result is a picture. The visit's best frame is already on
//    disk, chosen at capture time, so a result is recognisable at a
//    glance instead of being a row of text to decode.
// 3. Every result opens the footage. A card links to Recordings at that
//    camera and that instant — the whole point of searching, and the
//    step that used to be left to the operator and a wristwatch.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import {
  Camera as CameraIcon, CarFront, Clock, ImageOff, Info, Route,
  Search as SearchIcon, Sparkles, Tag, Type, UserRound, X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import SearchAnswer, { type SearchAnswerData } from '../components/SearchAnswer'
import { AuthedImage } from '../components/AuthedImage'
import { JourneyPanel } from '../components/JourneyPanel'
import { useTranslation, useDateFormat, type DateFormatters } from '../i18n'
import {
  Badge, Button, Card, CardContent, EmptyState, PageHeader, Skeleton,
} from '../components/ui'

type Interpretation = {
  labels: string[]
  camera_ids: number[]
  from: string | null
  to: string | null
  text: string
  plate: string
  attrs: string[]
  matched: Record<string, string>
  ignored: string[]
  source: 'parsed' | 'explicit' | string
  overridden: string[]
}
type Claim = {
  kind: string
  value: string
  confidence: number | null
  task: string | null
  adapter: string | null
}
type Hit = {
  id: number
  camera_id: number
  camera_name: string | null
  label: string | null
  score: number
  started_at: string | null
  ended_at: string | null
  plate_text: string | null
  caption: string | null
  attributes: string | null
  source: string
  event_type: string
  evidence_url: string | null
  claims: Claim[]
  anchor: { camera_id: number; at: string | null; ended_at: string | null }
}
type RelaxHint = {
  /** The server's field name — mapped to a Filters key by RELAXABLE. */
  drop: 'text' | 'when' | 'camera_ids' | 'labels' | 'plate'
  value: string
  would_match: number
}

type SearchResponse = {
  query: string
  interpretation: Interpretation
  results: Hit[]
  count: number
  /** Counted facts about `results` — see components/SearchAnswer. */
  answer?: SearchAnswerData
  total: number
  /** Empty result: which ONE chip is responsible, and what dropping it
   *  would find. Empty when there were results, or when no single chip
   *  explains it. */
  relax?: RelaxHint[]
  /** Present when a leftover word was DROPPED to avoid an empty page.
   *  These results are not the search that was typed, so the page owes
   *  the operator a visible say-so and a way back. */
  relaxed?: { dropped: string; without: number }
  /** Why the ranking looks the way it does, when a second arm took part
   *  — or why it could not. Absent on a deployment that has never
   *  embedded anything, which is most of them. */
  semantic?: { used: boolean; reason?: string; note?: string; text_total?: number }
}

/** The filters the page owns, which are exactly the API's parameters. */
type Filters = {
  labels: string[]
  cameraIds: number[]
  from: string | null
  to: string | null
  text: string
  plate: string
  /** kind:value claims a skill made, ANDed. */
  attrs: string[]
}

/** The server names the field; the UI already has a dropper keyed by
 *  Filters. "when" maps to `from` because drop() clears both ends of a
 *  window together — a half-open range is not a thing anyone meant. */
const RELAXABLE: Record<RelaxHint['drop'], { key: keyof Filters; label: string }> = {
  text: { key: 'text', label: 'the words' },
  when: { key: 'from', label: 'the time window' },
  camera_ids: { key: 'cameraIds', label: 'the camera' },
  labels: { key: 'labels', label: 'the object' },
  plate: { key: 'plate', label: 'the plate' },
}

/** One entry in the people picker — see GET /search/people. */
type Person = { value: string; attr: string; visits: number; last_seen: string | null }

/** The claim kind whose value is a person, spelled the same as the
 *  server's PERSON_KIND. It is a constant here for the same reason it is
 *  one there: this kind is handled differently from every other claim,
 *  because it is the one that is not a search word. */
const PERSON_KIND = 'face_id'

const PAGE = 24

const EXAMPLES = [
  'red truck at the dock yesterday',
  'person in the last 10 minutes',
  'plate ka01ab1234',
  'bicycle last night',
]

function when(iso: string | null, fmt: DateFormatters): string {
  if (!iso) return '—'
  const d = new Date(iso)
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  const time = fmt.time(d, { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  return sameDay ? time : `${fmt.date(d, { day: 'numeric', month: 'short' })} ${time}`
}

function rangeLabel(from: string | null, to: string | null, fmt: DateFormatters): string {
  if (!from && !to) return ''
  const f = from ? new Date(from) : null
  const t = to ? new Date(to) : null
  const d = (x: Date) => fmt.dateTime(x, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  })
  if (f && t) return `${d(f)} → ${d(t)}`
  return f ? `after ${d(f)}` : `before ${d(t!)}`
}

/* ----------------------------- Page ----------------------------- */

export function Search() {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const [params, setParams] = useSearchParams()
  const [draft, setDraft] = useState(params.get('q') ?? '')
  // The query that has actually been run, and the filters it produced.
  const [sentence, setSentence] = useState(params.get('q') ?? '')
  const [filters, setFilters] = useState<Filters | null>(null)
  const [page, setPage] = useState(0)
  /** The visit whose route is open, if any. */
  const [following, setFollowing] = useState<number | null>(null)
  const boxRef = useRef<HTMLInputElement>(null)

  // A search page that needs a click before you can type is a search page
  // people stop using. Focus only on a cold open: arriving back from a
  // result with a query already in the URL should leave the page scrolled
  // where it was, not yank the caret.
  useEffect(() => {
    if (!params.get('q')) boxRef.current?.focus()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Typing a new sentence puts the page back under the parser's control;
  // editing chips takes it away. That is the whole state machine.
  const editing = filters !== null

  const request = useMemo(() => {
    const qs = new URLSearchParams()
    qs.set('limit', String(PAGE))
    qs.set('skip', String(page * PAGE))
    if (editing) {
      qs.set('parse', 'false')
      for (const l of filters!.labels) qs.append('label', l)
      for (const c of filters!.cameraIds) qs.append('camera_id', String(c))
      for (const a of filters!.attrs) qs.append('attr', a)
      if (filters!.text) qs.set('text', filters!.text)
      if (filters!.plate) qs.set('plate', filters!.plate)
      if (filters!.from) qs.set('from', filters!.from)
      if (filters!.to) qs.set('to', filters!.to)
    } else if (sentence) {
      qs.set('q', sentence)
    }
    return qs
  }, [editing, filters, sentence, page])

  // Names for the camera chip: "Loading dock" says what "1 camera"
  // cannot, and the id alone tells an operator nothing.
  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/cameras')
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as { id: number; name: string }[]
    },
    retry: 0,
    staleTime: 5 * 60_000,
  })
  const cameraName = (id: number) =>
    camerasQuery.data?.find((c) => c.id === id)?.name ?? `cam${id}`

  // What this deployment can actually describe a visit with. The
  // endpoint asks KAI-C which skills are registered AND healthy, and
  // its docstring has always said the UI should use it "to say what
  // searching by colour or by face would even mean here, instead of
  // offering filters that can never match". Until now nothing did, so
  // the empty state guessed on the operator's behalf.
  const planQuery = useQuery({
    queryKey: ['enrichment-plan'],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/search/enrichment-plan')
      return data as { descriptor_kinds?: string[] }
    },
    retry: 0,
    staleTime: 60_000,
  })
  // undefined = not asked yet or unreachable, which is NOT the same as
  // "this box describes nothing" and must not be rendered as if it were.
  const kinds: string[] | undefined = planQuery.isSuccess
    ? (planQuery.data?.descriptor_kinds ?? [])
    : undefined

  // WHO THIS BOX HAS ACTUALLY RECOGNISED.
  //
  // "was Varun here yesterday" cannot be typed. A name is deliberately
  // kept out of the words a caption search matches (see
  // descriptor_store._UNPROJECTED_KINDS) — projecting it would make any
  // query containing that word match the person, and would widen who can
  // discover the identity from the app that recognised them to anyone
  // with search access. So the name matches nothing, silently, and no
  // amount of rephrasing helps: the box cannot express the question.
  //
  // The filter that answers it, attr=face_id:varun, has worked all
  // along. It was simply unreachable unless you already knew the exact
  // stored value. This list is what turns it into a control.
  const peopleQuery = useQuery({
    queryKey: ['search-people'],
    queryFn: async () => {
      const { data } = await api.get('/api/v1/search/people')
      return (data as { people?: Person[] })?.people ?? []
    },
    retry: 0,
    staleTime: 60_000,
  })
  const people = peopleQuery.data ?? []

  const searchQuery = useQuery({
    queryKey: ['footage-search', request.toString()],
    queryFn: async () => {
      const { data } = await api.get(`/api/v1/search?${request.toString()}`)
      return data as SearchResponse
    },
    retry: 0,
  })

  const data = searchQuery.data
  const interp = data?.interpretation

  // Keep the URL in step with the sentence, so a search can be shared or
  // reloaded. Chip edits deliberately do NOT rewrite it: they are a
  // refinement of this search, not a new one.
  useEffect(() => {
    const current = params.get('q') ?? ''
    if (current !== sentence) {
      const next = new URLSearchParams(params)
      if (sentence) next.set('q', sentence)
      else next.delete('q')
      setParams(next, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sentence])

  const run = (text: string) => {
    setSentence(text)
    setDraft(text)
    setFilters(null)      // a new sentence hands control back to the parser
    setPage(0)
  }

  /** Start editing from whatever is on screen now. */
  const asFilters = (): Filters => ({
    labels: [...(interp?.labels ?? [])],
    cameraIds: [...(interp?.camera_ids ?? [])],
    from: interp?.from ?? null,
    to: interp?.to ?? null,
    text: interp?.text ?? '',
    plate: interp?.plate ?? '',
    attrs: [...(interp?.attrs ?? [])],
  })

  const drop = (part: keyof Filters, value?: string) => {
    const base = editing ? { ...filters! } : asFilters()
    if (part === 'labels') base.labels = []
    if (part === 'cameraIds') base.cameraIds = []
    if (part === 'text') base.text = ''
    if (part === 'plate') base.plate = ''
    if (part === 'from' || part === 'to') { base.from = null; base.to = null }
    // One claim at a time: dropping "colour:red" must not also drop
    // "vehicle_type:van" that is narrowing the same search.
    if (part === 'attrs') base.attrs = base.attrs.filter((a) => a !== value)
    setFilters(base)
    setPage(0)
  }

  /** "Every other red van" — refine by something a skill actually saw. */
  const addAttr = (claim: Claim) => {
    const pair = `${claim.kind}:${claim.value}`
    const base = editing ? { ...filters! } : asFilters()
    if (!base.attrs.includes(pair)) base.attrs = [...base.attrs, pair]
    setFilters(base)
    setPage(0)
  }

  /** Pick a person, or clear the pick.
   *
   *  ONE at a time, deliberately. The attr filter ANDs, and a visit is
   *  one object: two names required at once describes a visit claimed to
   *  be two people, which is all but never a row. Adding rather than
   *  replacing would quietly guarantee an empty page and look like "she
   *  was never here". */
  const setPerson = (value: string) => {
    const base = editing ? { ...filters! } : asFilters()
    base.attrs = base.attrs.filter((a) => !a.startsWith(`${PERSON_KIND}:`))
    if (value) {
      base.attrs = [...base.attrs, `${PERSON_KIND}:${value}`]
      // And the name stops being a word. It is still sitting in the text
      // filter if that is how it got typed, where it matches nothing and
      // ANDs the whole search down to an empty page — so switching to
      // the filter that works while leaving the one that cannot would
      // answer "was Varun here" with "no" a second time.
      const parts = new Set(value.split(/[^a-z0-9]+/).filter(Boolean))
      base.text = base.text
        .split(/\s+/)
        .filter((w) => w && !parts.has(w.toLowerCase()))
        .join(' ')
    }
    setFilters(base)
    setPage(0)
  }

  /** Whoever the running search is filtered to, read back off the
   *  interpretation so the control shows the state of the QUERY rather
   *  than a copy of it that can drift. */
  const pickedPerson =
    (interp?.attrs ?? [])
      .find((a) => a.startsWith(`${PERSON_KIND}:`))
      ?.slice(PERSON_KIND.length + 1) ?? ''

  const results = data?.results ?? []
  const relax = data?.relax ?? []
  const relaxed = data?.relaxed
  const semantic = data?.semantic
  const total = data?.total ?? 0
  const pages = Math.ceil(total / PAGE)
  const nothingAsked = !sentence && !editing

  // A NAME TYPED INTO THE BOX IS A QUESTION THAT CANNOT BE ANSWERED.
  //
  // It goes to the text filter, the text filter matches words a skill
  // wrote, and a name is never one of those. The result is an empty page
  // — or, worse, a page relaxed past the name, which reads as "here is
  // everything, and she is not in it". Both say "she was never here"
  // when the truth is "you cannot ask that way".
  //
  // So when the words this search used, or the ones it had to drop,
  // carry a name this box knows, say so and offer the filter that works.
  // Matching on all of a name's parts rather than the whole string is
  // what makes "was varun singh here" and "varun" both land.
  const typedName = useMemo(() => {
    if (people.length === 0) return null
    const words = new Set(
      `${interp?.text ?? ''} ${relaxed?.dropped ?? ''} ${(interp?.ignored ?? []).join(' ')}`
        .toLowerCase()
        .split(/[^a-z0-9]+/)
        .filter(Boolean),
    )
    if (words.size === 0) return null
    return (
      people.find((p) => {
        const parts = p.value.split(/[^a-z0-9]+/).filter(Boolean)
        return parts.length > 0 && parts.every((part) => words.has(part))
      }) ?? null
    )
  }, [people, interp?.text, interp?.ignored, relaxed?.dropped])

  // WHAT THIS BOX CAN ANSWER, not what deployments in general can.
  //
  // The old sentence said "a deployment with no captioner can search
  // classes, cameras, times and plates, but not colours" — true, and
  // useless, because it never said which kind of deployment this is.
  // An operator reading it cannot tell whether their search failed
  // because nothing was red or because nothing here has ever looked at
  // a colour. Those are opposite answers.
  //
  // kinds === undefined means the registry could not be asked. That is
  // a third state and gets the old wording: claiming a box describes
  // nothing because KAI-C was briefly unreachable would be a worse lie
  // than the vague one.
  // face_id is a kind this box can produce, and it is NOT something to
  // type — listing it among the words to try sends the operator back to
  // the query that cannot work. It gets its own sentence pointing at the
  // control that does, and the rest are said in words rather than in
  // column names.
  const sayable = (kinds ?? [])
    .filter((k) => k !== PERSON_KIND)
    .map((k) => k.replace(/_/g, ' '))
  const recognisesPeople = (kinds ?? []).includes(PERSON_KIND)
  const pickerHint = recognisesPeople && people.length > 0
    ? ' A name is not one of those words — use the Person picker above to search for someone.'
    : ''
  const emptyReason =
    kinds === undefined
      ? 'Nothing matched. Words only match what a skill wrote about a frame, so a deployment with nothing describing visits can search classes, cameras, times and plates, but not colours.'
      : sayable.length === 0
        ? `Nothing matched — and nothing on this system describes visits in words, so words can only match a plate. Install a captioner or a colour/type skill from the App Catalog and searches like “red van” start working; classes, cameras, times and plates work now.${pickerHint}`
        : `Nothing matched. This system can describe a visit by ${sayable.join(', ')} — anything outside that has no words to match against, so try a class, a camera, a time or a plate.${pickerHint}`

  const chips = interp
    ? [
        interp.labels.length > 0 && {
          key: 'labels' as const,
          icon: <Tag size={12} />,
          label: interp.labels.join(' or '),
          title: 'Object class. Several classes mean "any of these" — one visit is one object.',
        },
        interp.camera_ids.length > 0 && {
          key: 'cameraIds' as const,
          icon: <CameraIcon size={12} />,
          label: interp.camera_ids.length === 1
            ? cameraName(interp.camera_ids[0])
            : `${interp.camera_ids.length} cameras`,
          title: interp.camera_ids.map(cameraName).join(', '),
        },
        (interp.from || interp.to) && {
          key: 'from' as const,
          icon: <Clock size={12} />,
          // Your word, not the machine's expansion of it: "yesterday" is
          // what you typed and what you would edit, and the range it
          // became is one hover away.
          label: interp.matched?.when || rangeLabel(interp.from, interp.to, fmt),
          title: interp.matched?.when
            ? `"${interp.matched.when}" = ${rangeLabel(interp.from, interp.to, fmt)}`
            : 'The time window searched.',
        },
        interp.text && {
          key: 'text' as const,
          icon: <Type size={12} />,
          label: `"${interp.text}"`,
          title: 'Matched against what a captioner wrote about the frame.',
        },
        ...(interp.attrs ?? []).map((pair) => {
          const person = pair.startsWith(`${PERSON_KIND}:`)
          return {
            key: 'attrs' as const,
            value: pair,
            icon: person ? <UserRound size={12} /> : <Sparkles size={12} />,
            label: pair.split(':').slice(1).join(':'),
            title: person
              ? 'Recognised as this person. A name is not a searchable word, so this chip is the only way to ask for them.'
              : `${pair.split(':')[0]} — what a skill claimed about the object.`,
          }
        }),
        interp.plate && {
          key: 'plate' as const,
          icon: <CarFront size={12} />,
          label: interp.plate,
          title: 'Plate reads containing this.',
        },
      ].filter(Boolean) as {
        key: keyof Filters; value?: string; icon: React.ReactNode; label: string; title: string
      }[]
    : []

  return (
    <section className="space-y-4">
      <PageHeader title={t('search.title')} description={t('search.description')} />

      {/* ── The box ── */}
      <Card>
        <CardContent className="py-3 space-y-3">
          <form
            className="flex items-center gap-2"
            onSubmit={(e) => { e.preventDefault(); run(draft.trim()) }}
          >
            <SearchIcon size={18} className="text-[var(--text-dim)] shrink-0" />
            <input
              ref={boxRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t('search.placeholder')}
              aria-label={t('search.title')}
              className="flex-1 bg-transparent outline-none text-sm py-1.5 placeholder:text-[var(--text-dim)]"
            />
            {draft && (
              <Button size="sm" variant="ghost" onClick={() => { setDraft(''); run('') }}
                      title="Clear">
                <X size={14} />
              </Button>
            )}
            <Button size="sm" variant="primary" type="submit">Search</Button>
          </form>

          {/* What it understood — and the way out of a wrong guess. */}
          {chips.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="text-[var(--text-dim)]">
                {editing ? 'Filters:' : 'Understood as:'}
              </span>
              {chips.map((c) => (
                <span
                  key={`${c.key}:${c.value ?? ''}`}
                  title={c.title}
                  className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-2)] pl-2 pr-1 py-0.5"
                >
                  {c.icon}
                  <span>{c.label}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${c.label}`}
                    className="ml-0.5 rounded p-0.5 text-[var(--text-dim)] hover:text-[var(--danger)]"
                    onClick={() => drop(c.key, c.value)}
                  >
                    <X size={11} />
                  </button>
                </span>
              ))}
              {editing && (
                <Button size="sm" variant="ghost" onClick={() => { setFilters(null); setPage(0) }}>
                  Reset to my words
                </Button>
              )}
            </div>
          )}

          {/* THE FILTER WITH NO WAY IN FROM THE KEYBOARD.
              Rendered only when this box has recognised somebody: an
              empty dropdown implies there might be a name in it, and a
              control that can only be set to "Anyone" is worse than no
              control at all. */}
          {people.length > 0 && (
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <label
                htmlFor="search-person"
                className="inline-flex items-center gap-1 text-[var(--text-dim)]"
              >
                <UserRound size={12} /> Person:
              </label>
              <select
                id="search-person"
                value={pickedPerson}
                onChange={(e) => setPerson(e.target.value)}
                className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 outline-none focus:border-[var(--accent)]"
              >
                <option value="">Anyone</option>
                {people.map((p) => (
                  <option key={p.value} value={p.value}>
                    {p.value} ({p.visits})
                  </option>
                ))}
              </select>
              <span className="text-[var(--text-dim)]">
                Names are kept out of the searchable words on purpose — pick one here instead of typing it.
              </span>
            </div>
          )}

          {/* A name WAS typed, and it can never match. Say it, and
              offer the one thing that does. */}
          {typedName && typedName.value !== pickedPerson && (
            <div className="flex flex-wrap items-center gap-2 rounded border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2 text-xs">
              <span className="text-[var(--text-dim)]">
                <b className="text-[var(--text)]">{typedName.value}</b> is a person, not a
                word — searching for the name matches nothing however it is phrased.{' '}
                {typedName.visits} {typedName.visits === 1 ? 'visit' : 'visits'} here{' '}
                {typedName.visits === 1 ? 'has' : 'have'} been recognised as them.
              </span>
              <Button
                variant="outline"
                size="sm"
                className="ml-auto"
                onClick={() => setPerson(typedName.value)}
              >
                Search for {typedName.value}
              </Button>
            </div>
          )}

          {interp && interp.ignored.length > 0 && (
            <div className="flex items-center gap-1.5 text-xs text-[var(--text-dim)]">
              <Info size={12} />
              Ignored: {interp.ignored.join(', ')}
              {/* The reason only holds for what the PARSER set aside. Since
                  the relax path started moving dropped words here too, the
                  line was explaining "was, varun, here" as bare numbers —
                  a sentence the operator can see is false, attached to the
                  one field whose job is honesty about what was dropped. */}
              {interp.ignored.every((w) => /^\d+$/.test(w))
                && ' — bare numbers match too much to be useful.'}
            </div>
          )}

          {nothingAsked && (
            <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--text-dim)]">
              Try:
              {EXAMPLES.map((e) => (
                <button
                  key={e}
                  type="button"
                  className="rounded border border-[var(--border)] px-2 py-0.5 hover:border-[var(--accent)] hover:text-[var(--text)]"
                  onClick={() => run(e)}
                >
                  {e}
                </button>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Results ── */}
      {searchQuery.isError ? (
        <EmptyState
          icon={<SearchIcon size={24} />}
          title={t('search.failed')}
          description="The search service did not answer. The event store is part of core, so this usually means core itself is unreachable rather than anything to do with a missing app."
        />
      ) : searchQuery.isPending ? (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
          {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-44" />)}
        </div>
      ) : results.length === 0 ? (
        <div className="space-y-3">
          <EmptyState
            icon={<SearchIcon size={24} />}
            title={nothingAsked ? t('search.startTitle') : t('search.noneTitle')}
            description={
              nothingAsked
                ? 'Ask for what you are looking for in your own words. Everything Tier-0 sees is already indexed — no app to install, and nothing is re-scanned when you search.'
                : relax.length > 0
                  ? 'One of the chips above is doing all the narrowing.'
                  : chips.length > 1
                    ? 'Nothing matched all of those at once. Remove a chip above — the time window and the words are the two that usually narrow it too far.'
                    : emptyReason
            }
          />
          {/* The server already counted what dropping each chip would
              find, so the operator does not have to guess which one to
              remove — or remove the right one and not know it. */}
          {relax.length > 0 && (
            <div className="flex flex-wrap items-center justify-center gap-2">
              {relax.map((hint) => {
                const spec = RELAXABLE[hint.drop]
                if (!spec) return null
                return (
                  <Button
                    key={hint.drop}
                    variant="outline"
                    onClick={() => drop(spec.key)}
                  >
                    Without {spec.label}
                    {hint.value ? ` (${hint.value})` : ''}: {hint.would_match}{' '}
                    {hint.would_match === 1 ? 'result' : 'results'}
                  </Button>
                )
              })}
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="flex items-center gap-2 text-xs text-[var(--text-dim)] px-0.5">
            <span>
              {total} {total === 1 ? 'result' : 'results'}
              {pages > 1 && ` · page ${page + 1} of ${pages}`}
            </span>
            {searchQuery.isFetching && <span>updating…</span>}
          </div>

          {/* THESE ARE NOT THE SEARCH THAT WAS TYPED.
              The server drops a leftover word rather than showing a
              blank page for a sentence it mostly understood — "what is
              NUMBER of it" should not cost you 421 cars. But results
              nobody asked for, handed over silently, are the same
              confident-wrong-answer failure wearing better clothes: the
              operator would later wonder why a filter did nothing. So
              it is said out loud, and it is one click to get the strict
              search back — an explicit `text` is never dropped, which
              is exactly what pinning it does. */}
          {relaxed && (
            <div className="flex flex-wrap items-center gap-2 rounded border border-[var(--border)] bg-[var(--bg-2)] px-3 py-2 text-xs">
              <span className="text-[var(--text-dim)]">
                Nothing was described as <b className="text-[var(--text)]">{relaxed.dropped}</b>,
                so {relaxed.dropped.trim().split(/\s+/).length === 1 ? 'that word was' : 'those words were'}{' '}
                set aside — showing the {relaxed.without}{' '}
                {relaxed.without === 1 ? 'result' : 'results'} the rest of your search matched.
              </span>
              <Button
                variant="outline"
                size="sm"
                className="ml-auto"
                onClick={() => {
                  const base = editing ? { ...filters! } : asFilters()
                  base.text = relaxed.dropped
                  setFilters(base)
                  setPage(0)
                }}
              >
                Search for “{relaxed.dropped}” anyway
              </Button>
            </div>
          )}

          {/* Vectors exist here and the query could not be turned into
              one, so this ranking is words-only and the operator is
              owed that rather than a quietly worse result. Absent on a
              deployment that never embedded anything — a permanent
              "semantic: off" banner would be noise about a feature
              nobody switched on. */}
          {semantic && semantic.used === false && semantic.reason === 'embedder-unreachable' && (
            <div className="rounded border border-[var(--warning,#b7791f)] px-3 py-2 text-xs text-[var(--warning,#b7791f)]">
              {semantic.note ?? 'Ranked by words only — the embedding adapter could not be reached.'}
            </div>
          )}

          <SearchAnswer answer={data?.answer} />

          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-4 gap-3">
            {results.map((h, i) => (
              <ResultCard
                key={h.id}
                hit={h}
                rank={page * PAGE + i + 1}
                onRefine={addAttr}
                onFollow={() => setFollowing(h.id)}
              />
            ))}
          </div>

          {pages > 1 && (
            <div className="flex items-center justify-center gap-2 pt-1">
              <Button size="sm" variant="outline" disabled={page === 0}
                      onClick={() => setPage((p) => Math.max(0, p - 1))}>
                Newer
              </Button>
              <Button size="sm" variant="outline" disabled={page + 1 >= pages}
                      onClick={() => setPage((p) => p + 1)}>
                Older
              </Button>
            </div>
          )}
        </>
      )}

      {/* A route is read against the results it came from — which other
          visit was the better candidate, what else was on that camera —
          so it opens beside the page rather than replacing it. */}
      <JourneyPanel eventId={following} onClose={() => setFollowing(null)} />
    </section>
  )
}

/* --------------------------- Pieces ----------------------------- */

function ResultCard(
  { hit, rank, onRefine, onFollow }: {
    hit: Hit
    rank: number
    onRefine: (c: Claim) => void
    onFollow: () => void
  },
) {
  const { t } = useTranslation()
  const fmt = useDateFormat()
  const at = hit.anchor?.at ?? hit.started_at
  // Recordings opens on this camera, this day, this instant.
  const href = at
    ? `/playback/sync?camera=${hit.camera_id}&at=${encodeURIComponent(at)}`
    : '/playback/sync'
  // Which position in the list was worth opening — the only relevance
  // judgement available without somebody labelling footage. The POSITION
  // and nothing else: not the query, not the result, not who searched.
  // Fire-and-forget, and a failure is ignored, because a metric must
  // never get between an operator and the video.
  const opened = () => {
    api.post(`/api/v1/search/opened?rank=${rank}`).catch(() => {})
  }
  return (
    <div className="rounded border border-[var(--border)] bg-[var(--panel)] overflow-hidden">
    <Link
      onClick={opened}
      to={href}
      className="group block hover:opacity-95 focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]"
      title={at ? `Open the recording at ${fmt.dateTime(at)}` : 'Open recordings'}
    >
      <div className="relative aspect-video bg-[var(--bg-2)] flex items-center justify-center">
        {hit.evidence_url ? (
          // Through the api client, not a bare <img src>. The evidence
          // endpoint is camera-scoped and needs the JWT header, which
          // an <img> cannot send — so a plain src 401s and every result
          // renders as a broken-image icon.
          <AuthedImage
            queryKey={['search-evidence', hit.id]}
            fetchBlob={(signal) =>
              api.get(`/api/v1/events/${hit.id}/evidence`, {
                responseType: 'blob',
                signal,
              })
            }
            alt={`${hit.label ?? 'object'} on ${hit.camera_name ?? hit.camera_id}`}
            className="h-full w-full object-cover"
          />
        ) : (
          <span className="text-[var(--text-dim)] flex flex-col items-center gap-1 text-[11px]">
            <ImageOff size={18} />
            no frame kept
          </span>
        )}
        <span className="absolute left-1.5 top-1.5">
          <Badge variant="neutral">{hit.label ?? hit.event_type}</Badge>
        </span>
        {hit.plate_text && (
          <span className="absolute right-1.5 top-1.5">
            <Badge variant="info">{hit.plate_text}</Badge>
          </span>
        )}
        <span className="absolute inset-x-0 bottom-0 bg-black/55 text-white text-[11px] px-2 py-1 opacity-0 group-hover:opacity-100 transition-opacity">
          Open the recording here
        </span>
      </div>
      <div className="p-2 space-y-0.5">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-medium truncate">{hit.camera_name ?? `cam${hit.camera_id}`}</span>
          <span className="ml-auto tabular-nums text-[var(--text-dim)]">{when(at, fmt)}</span>
        </div>
        {hit.caption && (
          <div className="text-[11px] text-[var(--text-dim)] line-clamp-2">{hit.caption}</div>
        )}
      </div>
    </Link>
    {/* What the skills said — outside the link, because each claim is
        itself a way to search: one click finds every other red van. The
        skill and its confidence ride the tooltip, so a result can always
        be asked who said this. */}
    <div className="flex flex-wrap items-center gap-1 px-2 pb-2">
      {/* Outside the Link as well, and first, because following an
          object is a different question from watching this moment: the
          card's own click opens the recording here. */}
      <button
        type="button"
        onClick={onFollow}
        className="inline-flex items-center gap-1 rounded border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 text-[10px] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--accent)]"
      >
        <Route size={11} />
        {t('search.follow')}
      </button>
      {hit.claims.map((c) => (
          <button
            key={`${c.kind}:${c.value}`}
            type="button"
            onClick={() => onRefine(c)}
            title={`${c.kind} = ${c.value}${c.confidence != null ? ` (${Math.round(c.confidence * 100)}%)` : ''}`
              + `${c.task ? ` · ${c.task}` : ''}${c.adapter ? ` · ${c.adapter}` : ''}`
              + ' — click to find others'}
            className="rounded border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 text-[10px] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--accent)]"
          >
            {c.value}
          </button>
      ))}
    </div>
    </div>
  )
}
