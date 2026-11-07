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
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Camera as CameraIcon, CarFront, Clock, CornerDownLeft, ImageOff, Info, LayoutGrid, List, Play, Route,
  Search as SearchIcon, Sparkles, Tag, Type, UserRound, X,
  Users,
} from 'lucide-react'
import { api } from '../lib/api'
import { apiService } from '../lib/apiService'
import { PageTitleBar, TITLE_PANEL_BODY_CLASS } from '../components/ui/layout'
import SearchAnswer, { type SearchAnswerData } from '../components/SearchAnswer'
import { AuthedImage } from '../components/AuthedImage'
import { JourneyPanel } from '../components/JourneyPanel'
import { useTranslation, useDateFormat, type DateFormatters } from '../i18n'
import { Badge, Button, EmptyState, SeverityBadge, Skeleton } from '../components/ui'
import { DataTable, type Column } from '../components/ui/DataTable'
import { Pagination } from '../components/ui/Pagination'
import { SegmentedControl } from '../components/ui/SegmentedControl'

/** One thing the question asked of a skill, and whether this box has it
 *  (services/search_intent.py). `state` is `available`, `never-produced`
 *  (skill here, never assigned to a camera) or `not-on-this-box`. */
type Need = {
  word: string
  kind: string
  skill: string
  state: string
  fallback: string | null
  /** Installed apps whose manifest brings this skill — pick the camera there. */
  apps?: string[]
}
type Interpretation = {
  wants_plate?: boolean
  needs?: Need[]
  labels: string[]
  camera_ids: number[]
  from: string | null
  to: string | null
  text: string
  plate: string
  attrs: string[]
  matched: Record<string, string>
  ignored: string[]
  min_people?: number | null
  source: 'parsed' | 'explicit' | string
  overridden: string[]
}
type SearchAlert = {
  alert_id: string
  fired_at: string | null
  severity: string
  title: string
  description: string | null
  alert_type: string | null
  source_name: string | null
  camera_id: number | null
  camera_name: string | null
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
  /** No time in the question: how far back the whole-index search went. */
  scope?: { all_time: boolean; oldest: string; days: number; total: number } | null
  /** Why the ranking looks the way it does, when a second arm took part
   *  — or why it could not. Absent on a deployment that has never
   *  embedded anything, which is most of them. */
  semantic?: { used: boolean; reason?: string; note?: string; text_total?: number }
  /** App alerts the question's words named (loitering, occupancy, a
   *  package…), in the same window and cameras. Null when the question
   *  named nothing alert-shaped; [] when it did and nothing fired. */
  alerts?: SearchAlert[] | null
  /** What the question needed that the matching visits do not have yet,
   *  and what this search queued to be described now. "Not yet" is a
   *  different answer from "no": an empty page over undescribed visits
   *  must say so, and say when to look again. Null when the question
   *  needed nothing a description would add. */
  pending?: {
    missing: number
    requested: number
    already_queued: number
    eta_s: number
    request_id?: string | null
    capped_at?: number
    needs?: { caption: boolean; kinds: string[] }
  } | null
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
  /** "three people together": at least this many people in frame. */
  minPeople: number | null
}

/** The server names the field; the UI already has a dropper keyed by
 *  Filters. "when" maps to `from` because drop() clears both ends of a
 *  window together — a half-open range is not a thing anyone meant. */
const RELAXABLE: Record<RelaxHint['drop'], { key: keyof Filters; label: string }> = {
  text: { key: 'text', label: 'the words' },
  when: { key: 'from', label: 'the time' },
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

const PAGE_SIZES = [24, 48, 96]

/** Thumbnails or a table — a preference, so it is remembered per browser. */
type View = 'grid' | 'table'
const VIEW_KEY = 'opennvr.search.view'
function storedView(): View {
  try { return localStorage.getItem(VIEW_KEY) === 'table' ? 'table' : 'grid' } catch { return 'grid' }
}

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
  const [pageSize, setPageSize] = useState(PAGE_SIZES[0])
  const [view, setViewState] = useState<View>(storedView)
  const navigate = useNavigate()
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
    qs.set('limit', String(pageSize))
    qs.set('skip', String(page * pageSize))
    if (editing) {
      qs.set('parse', 'false')
      for (const l of filters!.labels) qs.append('label', l)
      for (const c of filters!.cameraIds) qs.append('camera_id', String(c))
      for (const a of filters!.attrs) qs.append('attr', a)
      if (filters!.text) qs.set('text', filters!.text)
      if (filters!.plate) qs.set('plate', filters!.plate)
      if (filters!.minPeople) qs.set('min_people', String(filters!.minPeople))
      if (filters!.from) qs.set('from', filters!.from)
      if (filters!.to) qs.set('to', filters!.to)
    } else if (sentence) {
      qs.set('q', sentence)
    }
    return qs
  }, [editing, filters, sentence, page, pageSize])

  // Names for the camera chip: "Loading dock" says what "1 camera"
  // cannot, and the id alone tells an operator nothing.
  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
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
    minPeople: interp?.min_people ?? null,
  })

  const drop = (part: keyof Filters, value?: string) => {
    const base = editing ? { ...filters! } : asFilters()
    if (part === 'labels') base.labels = []
    if (part === 'cameraIds') base.cameraIds = []
    if (part === 'text') base.text = ''
    if (part === 'plate') base.plate = ''
    if (part === 'minPeople') base.minPeople = null
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
  const pending = data?.pending
  const semantic = data?.semantic
  const total = data?.total ?? 0
  const pages = Math.ceil(total / pageSize)
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
    ? ' To find someone, choose them from the Person list.'
    : ''
  const emptyReason =
    kinds === undefined
      ? 'Nothing matched. Try an object (car, person), a camera, a time, or a number plate.'
      : sayable.length === 0
        ? `Nothing matched. You can search by object, camera, time and number plate. To search by colour or description (like “red van”), add a description app from the App Catalog.${pickerHint}`
        : `Nothing matched. You can search by object, camera, time, number plate and ${sayable.join(', ')}.${pickerHint}`

  const chips = interp
    ? [
        interp.labels.length > 0 && {
          key: 'labels' as const,
          icon: <Tag size={12} />,
          label: interp.labels.join(' or '),
          title: 'Type of object. Several mean “any of these”.',
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
            : 'The time range searched.',
        },
        interp.text && {
          key: 'text' as const,
          icon: <Type size={12} />,
          label: `"${interp.text}"`,
          title: 'Words to look for in the scene description.',
        },
        ...(interp.attrs ?? []).map((pair) => {
          const person = pair.startsWith(`${PERSON_KIND}:`)
          return {
            key: 'attrs' as const,
            value: pair,
            icon: person ? <UserRound size={12} /> : <Sparkles size={12} />,
            label: pair.split(':').slice(1).join(':'),
            title: person
              ? 'Only visits recognised as this person.'
              : `${pair.split(':')[0].replace(/_/g, ' ')}: ${pair.split(':').slice(1).join(':')}`,
          }
        }),
        interp.plate && {
          key: 'plate' as const,
          icon: <CarFront size={12} />,
          label: interp.plate,
          title: 'Number plates containing this.',
        },
        interp.min_people && interp.min_people > 1 && {
          key: 'minPeople' as const,
          icon: <Users size={12} />,
          label: `${interp.min_people}+ people together`,
          title: interp.matched?.count
            ? `"${interp.matched.count}" = at least ${interp.min_people} people in frame at once`
            : `At least ${interp.min_people} people in frame at once.`,
        },
      ].filter(Boolean) as {
        key: keyof Filters; value?: string; icon: React.ReactNode; label: string; title: string
      }[]
    : []

  // Notices sit between the box and the results, one line each. They used
  // to be stacked paragraphs that pushed the results off the screen; the
  // honesty they carry is kept, the length is not.
  const notices: { key: string; tone: 'info' | 'warn'; icon: React.ReactNode; text: React.ReactNode; action?: React.ReactNode }[] = []
  if (typedName && typedName.value !== pickedPerson) {
    notices.push({
      key: 'typed-name',
      tone: 'info',
      icon: <UserRound size={13} />,
      text: <>Looking for <b>{typedName.value}</b>? Names can’t be typed into search — pick the person instead.</>,
      action: (
        <Button variant="outline" size="sm" onClick={() => setPerson(typedName.value)}>
          Show {typedName.value} ({typedName.visits})
        </Button>
      ),
    })
  }
  for (const n of (interp?.needs ?? []).filter((x) => x.state !== 'available')) {
    notices.push({
      key: `need:${n.kind}:${n.word}`,
      tone: 'warn',
      icon: <Sparkles size={13} />,
      text: (
        <>
          {t(
            n.state === 'never-produced' && (n.apps?.length ?? 0) > 0
              ? 'search.needs.never-produced-app'
              : `search.needs.${n.state}`,
            {
              word: n.word,
              kind: t(`search.kind.${n.kind}`),
              skill: t(`search.skill.${n.skill}`),
              apps: (n.apps ?? []).join(', '),
            },
          )}
          {n.fallback === 'captions' && <> {t('search.needs.captionsFallback')}</>}
        </>
      ),
    })
  }
  if (interp?.wants_plate) {
    notices.push({ key: 'plate', tone: 'info', icon: <CarFront size={13} />, text: t('search.needs.wantsPlate') })
  }
  // A question with no time searches everything the box holds. Cheap for
  // the database, expensive for the person scrolling: say how far back it
  // reached and offer the usual windows in one click. An explicit window
  // (even an edited one) means the operator chose, so no notice.
  if (data?.scope?.all_time && data.scope.days > 1 && !(editing && filters?.from)) {
    const narrow = (days: number) => {
      const base = editing ? { ...filters! } : asFilters()
      const now = new Date()
      base.from = new Date(now.getTime() - days * 86400000).toISOString()
      base.to = null
      setFilters(base)
      setPage(0)
    }
    notices.push({
      key: 'scope',
      tone: 'info',
      icon: <Clock size={13} />,
      text: (
        <>
          No time in the question, so this searched all {data.scope.days} days the box holds
          ({data.scope.total} visits). Add a time to narrow it:
        </>
      ),
      action: (
        <span className="flex gap-1">
          <Button variant="outline" size="sm" onClick={() => narrow(1)}>last 24 h</Button>
          <Button variant="outline" size="sm" onClick={() => narrow(7)}>7 days</Button>
          <Button variant="outline" size="sm" onClick={() => narrow(30)}>30 days</Button>
        </span>
      ),
    })
  }
  if (interp && interp.ignored.length > 0) {
    notices.push({
      key: 'ignored',
      tone: 'info',
      icon: <Info size={13} />,
      text: <>Skipped: {interp.ignored.join(', ')}</>,
    })
  }
  // THESE ARE NOT THE SEARCH THAT WAS TYPED. The server drops a leftover
  // word rather than showing a blank page for a sentence it mostly
  // understood; the operator is told, and gets the strict search back in
  // one click — an explicit `text` is never dropped.
  if (relaxed && results.length > 0) {
    notices.push({
      key: 'relaxed',
      tone: 'warn',
      icon: <Info size={13} />,
      text: (
        <>
          <b>These are not what you searched for.</b> Nothing matched “<b>{relaxed.dropped}</b>”, so
          these are the {relaxed.without} visits that match the rest of your search.
        </>
      ),
      action: (
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            const base = editing ? { ...filters! } : asFilters()
            base.text = relaxed.dropped
            setFilters(base)
            setPage(0)
          }}
        >
          Search “{relaxed.dropped}” only
        </Button>
      ),
    })
  }
  // NOT YET IS NOT NO. The question needed a description the matching
  // visits do not have; the server queued them to be described now.
  // Say how many and roughly when, and give the operator the refetch.
  if (pending && pending.missing > 0 && pending.requested + pending.already_queued > 0) {
    const queued = pending.requested + pending.already_queued
    const eta =
      pending.eta_s >= 90
        ? t('search.pending.minutes', { n: Math.max(1, Math.round(pending.eta_s / 60)) })
        : t('search.pending.seconds', { n: Math.max(5, Math.round(pending.eta_s / 5) * 5) })
    notices.push({
      key: 'pending',
      tone: 'info',
      icon: <Clock size={13} />,
      text: t(pending.missing > queued ? 'search.pending.describingSome' : 'search.pending.describing', {
        missing: pending.missing,
        queued,
        eta,
      }),
      action: (
        <Button variant="outline" size="sm" onClick={() => void searchQuery.refetch()}>
          {t('search.pending.checkAgain')}
        </Button>
      ),
    })
  }
  // Only when vectors exist here and the query could not use them — a
  // permanent banner about a feature nobody switched on would be noise.
  if (semantic && semantic.used === false && semantic.reason === 'embedder-unreachable') {
    notices.push({
      key: 'semantic',
      tone: 'warn',
      icon: <Info size={13} />,
      text: 'Smart matching is offline, so results match your exact words only.',
    })
  }

  const setView = (v: View) => {
    setViewState(v)
    try { localStorage.setItem(VIEW_KEY, v) } catch { /* private window */ }
  }

  const openRecording = (h: Hit) => {
    navigate(recordingHref(h))
  }

  const toolbar = (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 px-3 py-2 text-xs">
      <span className="font-medium text-[var(--text)]">
        {searchQuery.isPending ? 'Searching…' : `${total} ${total === 1 ? 'result' : 'results'}`}
      </span>
      {results.length > 0 && <SearchAnswer answer={data?.answer} />}
      <span className="ml-auto flex items-center gap-2">
        {searchQuery.isFetching && !searchQuery.isPending && (
          <span className="text-[var(--text-dim)]">Updating…</span>
        )}
        <SegmentedControl<View>
          label="View"
          value={view}
          onChange={setView}
          options={[
            { value: 'grid', label: <LayoutGrid size={14} />, title: 'Thumbnails' },
            { value: 'table', label: <List size={14} />, title: 'Table' },
          ]}
        />
      </span>
    </div>
  )

  const footer = pages > 0 ? (
    <Pagination
      page={page + 1}
      pageSize={pageSize}
      total={total}
      rowCount={results.length}
      pageSizeOptions={PAGE_SIZES}
      onPageChange={(p) => setPage(p - 1)}
      onPageSizeChange={(n) => { setPageSize(n); setPage(0) }}
      isFetching={searchQuery.isFetching}
      label="results"
    />
  ) : null

  const emptyBody = searchQuery.isError ? (
    <EmptyState
      icon={<SearchIcon size={24} />}
      title={t('search.failed')}
      description="Search isn’t responding right now. Check that the OpenNVR server is running, then try again."
      action={<Button variant="outline" onClick={() => searchQuery.refetch()}>Try again</Button>}
    />
  ) : (
    <div className="space-y-3">
      <EmptyState
        icon={<SearchIcon size={24} />}
        title={nothingAsked ? t('search.startTitle') : t('search.noneTitle')}
        description={
          nothingAsked
            ? 'Describe what you want to find in everyday words — an object, a camera, a time, or a number plate.'
            : relax.length > 0
              ? 'One of your filters is ruling everything out. Try removing it:'
              : chips.length > 1
                ? 'Nothing matched all of those together. Try removing a filter above — the time and the words usually narrow it most.'
                : emptyReason
        }
      />
      {/* The server already counted what dropping each chip would find,
          so the operator does not have to guess which one to remove. */}
      {relax.length > 0 && (
        <div className="flex flex-wrap items-center justify-center gap-2">
          {relax.map((hint) => {
            const spec = RELAXABLE[hint.drop]
            if (!spec) return null
            return (
              <Button key={hint.drop} variant="outline" onClick={() => drop(spec.key)}>
                Without {hint.value ? `“${hint.value}”` : spec.label} — {hint.would_match}{' '}
                {hint.would_match === 1 ? 'result' : 'results'}
              </Button>
            )
          })}
        </div>
      )}
    </div>
  )

  const shell = 'flex min-h-0 flex-1 flex-col border border-[var(--border)] bg-[var(--panel-2)]'

  return (
    // Bounded to the viewport, like Live View: the page itself never
    // scrolls; the results do, under a toolbar and pager that stay put.
    // 5rem = the 3rem top bar + the shell's p-4.
    <section className="flex h-[calc(100vh-5rem)] min-h-[480px] flex-col gap-2">
      <div className="shrink-0 border border-[var(--border)] bg-[var(--panel-2)]">
        <PageTitleBar icon={<SearchIcon size={16} />} title={t('search.title')} description={t('search.description')} />

        {/* ── The box ── */}
        {/* Title and field in one panel: the title is the panel's header, the
            field its body, so the two no longer read as the same control. The
            submit sits inside the field's end as a quiet Enter key — the box is
            the control, not a button beside it. */}
        <div className={`space-y-2 ${TITLE_PANEL_BODY_CLASS}`}>
          <form
            className="flex items-stretch"
            onSubmit={(e) => { e.preventDefault(); run(draft.trim()) }}
          >
            <div className="relative min-w-0 flex-1">
              <SearchIcon
                size={15}
                className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-dim)]"
              />
              <input
                ref={boxRef}
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={t('search.placeholder')}
                aria-label={t('search.title')}
                className="h-9 w-full border border-[var(--border)] bg-[var(--bg-2)] pl-9 pr-16 text-[13px] outline-none transition-colors placeholder:text-[color-mix(in_oklab,var(--text-dim)_50%,transparent)] focus:border-[var(--accent)] focus:placeholder:text-[color-mix(in_oklab,var(--text-dim)_20%,transparent)]"
              />
              <span className="absolute right-1.5 top-1/2 flex -translate-y-1/2 items-center gap-0.5">
                {draft && (
                  <button
                    type="button"
                    onClick={() => { setDraft(''); run('') }}
                    title="Clear"
                    aria-label="Clear"
                    className="grid h-6 w-6 place-items-center text-[var(--text-dim)] hover:bg-[var(--panel)] hover:text-[var(--text)]"
                  >
                    <X size={14} />
                  </button>
                )}
                <button
                  type="submit"
                  title="Search (Enter)"
                  aria-label="Search"
                  className={`grid h-6 w-7 place-items-center border transition-colors ${
                    draft.trim()
                      ? 'border-[var(--accent)] text-[var(--accent)] hover:bg-[var(--accent)] hover:text-white'
                      : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)]'
                  }`}
                >
                  <CornerDownLeft size={13} />
                </button>
              </span>
            </div>
          </form>

          {/* What it understood — each chip removable, which is the way out
              of a wrong guess — and the person picker on the same line. */}
          {(chips.length > 0 || people.length > 0 || nothingAsked) && (
            <div className="flex flex-wrap items-center gap-2 px-1 text-xs">
              {chips.length > 0 && (
                <span className="text-[var(--text-dim)]">
                  {editing ? 'Filters:' : 'Searching for:'}
                </span>
              )}
              {chips.map((c) => (
                <span
                  key={`${c.key}:${c.value ?? ''}`}
                  title={c.title}
                  className="inline-flex items-center gap-1 border border-[var(--border)] bg-[var(--bg-2)] pl-2 pr-1 py-0.5"
                >
                  {c.icon}
                  <span>{c.label}</span>
                  <button
                    type="button"
                    aria-label={`Remove ${c.label}`}
                    className="ml-0.5 p-0.5 text-[var(--text-dim)] hover:text-[var(--danger)]"
                    onClick={() => drop(c.key, c.value)}
                  >
                    <X size={11} />
                  </button>
                </span>
              ))}
              {editing && (
                <Button size="sm" variant="ghost" onClick={() => { setFilters(null); setPage(0) }}>
                  Undo changes
                </Button>
              )}

              {nothingAsked && chips.length === 0 && (
                <>
                  <span className="text-[var(--text-dim)]">Try</span>
                  {EXAMPLES.map((e, i) => (
                    <span key={e} className="inline-flex items-center gap-2">
                      {i > 0 && <span aria-hidden className="text-[var(--border)]">·</span>}
                      <button
                        type="button"
                        className="text-[var(--accent)] hover:underline"
                        onClick={() => run(e)}
                      >
                        {e}
                      </button>
                    </span>
                  ))}
                </>
              )}

              {/* Only when this box has recognised somebody: a picker that
                  can only be set to "Anyone" is worse than none. Names are
                  deliberately not searchable words, so this is the way in. */}
              {people.length > 0 && (
                <label className="ml-auto inline-flex items-center gap-1.5 text-[var(--text-dim)]">
                  <UserRound size={12} /> Person
                  <select
                    value={pickedPerson}
                    onChange={(e) => setPerson(e.target.value)}
                    className="border border-[var(--border)] bg-[var(--bg-2)] px-2 py-1 text-[var(--text)] outline-none focus:border-[var(--accent)]"
                  >
                    <option value="">Anyone</option>
                    {people.map((p) => (
                      <option key={p.value} value={p.value}>
                        {p.value} ({p.visits})
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
          )}
        </div>
      </div>

      {notices.length > 0 && (
        <div className="shrink-0 max-h-28 space-y-1 overflow-y-auto thin-scroll">
          {notices.map((n) => (
            <div
              key={n.key}
              className={`flex flex-wrap items-center gap-2 border px-3 py-1.5 text-xs ${
                n.tone === 'warn'
                  ? 'border-[var(--badge-warning-bg)] bg-[var(--badge-warning-bg)]/40 text-[var(--badge-warning-text)]'
                  : 'border-[var(--border)] bg-[var(--bg-2)] text-[var(--text-dim)]'
              }`}
            >
              <span className="shrink-0">{n.icon}</span>
              <span className="min-w-0 flex-1">{n.text}</span>
              {n.action}
            </div>
          ))}
        </div>
      )}

      {/* ── Results ── */}
      {view === 'table' && !searchQuery.isError && (searchQuery.isPending || results.length > 0) ? (
        <DataTable<Hit>
          fillParent
          dense
          fixed
          minWidth="min-w-[760px]"
          caption="Search results"
          columns={tableColumns(fmt, addAttr, setFollowing, page * pageSize)}
          rows={results}
          rowKey={(h) => h.id}
          isPending={searchQuery.isPending}
          isFetching={searchQuery.isFetching}
          skeletonRows={10}
          onRowClick={(h) => {
            reportOpened(results.indexOf(h) + 1 + page * pageSize)
            openRecording(h)
          }}
          toolbar={toolbar}
          footer={footer}
        />
      ) : (
        <div className={shell}>
          <div className="shrink-0 border-b border-[var(--border)]">{toolbar}</div>
          <div className="min-h-0 flex-1 overflow-y-auto thin-scroll p-3">
            {data?.alerts && data.alerts.length > 0 && (
              <div className="mb-3 rounded-md border border-border bg-muted/30 p-2" data-testid="search-alerts">
                <div className="mb-1 text-xs font-medium text-muted-foreground">
                  {data.alerts.length === 1 ? '1 app alert matches' : `${data.alerts.length} app alerts match`}
                </div>
                <ul className="space-y-1">
                  {data.alerts.map((a) => (
                    <li key={a.alert_id} className="flex flex-wrap items-baseline gap-x-2 text-sm">
                      <SeverityBadge severity={a.severity} />
                      <span className="font-medium">{a.title}</span>
                      {a.camera_id != null && <span className="text-muted-foreground">{a.camera_name ?? `camera ${a.camera_id}`}</span>}
                      {a.fired_at && <span className="text-muted-foreground">{fmt.dateTime(new Date(a.fired_at))}</span>}
                      {a.source_name && <span className="text-xs text-muted-foreground">{a.source_name}</span>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {data?.alerts && data.alerts.length === 0 && results.length === 0 && (
              <div className="mb-3 text-sm text-muted-foreground" data-testid="search-alerts-none">No app alert matched in this window either.</div>
            )}
            {searchQuery.isPending ? (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] items-start gap-2">
                {Array.from({ length: 12 }).map((_, i) => <Skeleton key={i} className="aspect-video" />)}
              </div>
            ) : searchQuery.isError || results.length === 0 ? (
              emptyBody
            ) : (
              <div className="grid grid-cols-[repeat(auto-fill,minmax(180px,1fr))] items-start gap-2">
                {results.map((h, i) => (
                  <ResultCard
                    key={h.id}
                    hit={h}
                    rank={page * pageSize + i + 1}
                    onRefine={addAttr}
                    onFollow={() => setFollowing(h.id)}
                  />
                ))}
              </div>
            )}
          </div>
          {footer && results.length > 0 && (
            <div className="shrink-0 border-t border-[var(--border)]">{footer}</div>
          )}
        </div>
      )}

      {/* A route is read against the results it came from, so it opens
          beside the page rather than replacing it. */}
      <JourneyPanel eventId={following} onClose={() => setFollowing(null)} />
    </section>
  )
}

/* --------------------------- Pieces ----------------------------- */

/** Recordings opens on this camera, this day, this instant. */
function recordingHref(hit: Hit): string {
  const at = hit.anchor?.at ?? hit.started_at
  return at
    ? `/playback/sync?camera=${hit.camera_id}&at=${encodeURIComponent(at)}`
    : '/playback/sync'
}

/** Which position in the list was worth opening — the only relevance
 *  judgement available without somebody labelling footage. The POSITION
 *  and nothing else. Fire-and-forget: a metric must never get between an
 *  operator and the video. */
function reportOpened(rank: number) {
  api.post(`/api/v1/search/opened?rank=${rank}`).catch(() => {})
}

/** A claim, said the way a person would read it. */
function claimTitle(c: Claim): string {
  const sure = c.confidence != null ? ` (${Math.round(c.confidence * 100)}% sure)` : ''
  return `${c.kind.replace(/_/g, ' ')}: ${c.value}${sure} — click to find more like this`
}

function Thumb({ hit, className }: { hit: Hit; className: string }) {
  return hit.evidence_url ? (
    // Through the api client, not a bare <img src>: the evidence endpoint
    // is camera-scoped and needs the JWT header, which an <img> cannot send.
    <AuthedImage
      queryKey={['search-evidence', hit.id]}
      fetchBlob={(signal) =>
        api.get(`/api/v1/events/${hit.id}/evidence`, { responseType: 'blob', signal })
      }
      alt={`${hit.label ?? 'object'} on ${hit.camera_name ?? hit.camera_id}`}
      className={className}
    />
  ) : (
    <span className="flex h-full w-full flex-col items-center justify-center gap-1 text-[11px] text-[var(--text-dim)]">
      <ImageOff size={16} />
      No image
    </span>
  )
}

function ClaimChips({ claims, onRefine, max }: { claims: Claim[]; onRefine: (c: Claim) => void; max: number }) {
  const shown = claims.slice(0, max)
  const rest = claims.length - shown.length
  return (
    <>
      {shown.map((c) => (
        <button
          key={`${c.kind}:${c.value}`}
          type="button"
          onClick={() => onRefine(c)}
          title={claimTitle(c)}
          className="border border-[var(--border)] bg-[var(--bg-2)] px-1.5 py-0.5 text-[10px] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--accent)]"
        >
          {c.value}
        </button>
      ))}
      {rest > 0 && (
        <span className="text-[10px] text-[var(--text-dim)]" title={claims.slice(max).map((c) => c.value).join(', ')}>
          +{rest}
        </span>
      )}
    </>
  )
}

function FollowButton({ onFollow, compact, className = '' }: { onFollow: () => void; compact?: boolean; className?: string }) {
  const { t } = useTranslation()
  return (
    // Following an object is a different question from watching this
    // moment, so it is its own control, outside the link to the video.
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); onFollow() }}
      title={t('search.follow')}
      aria-label={t('search.follow')}
      className={className || 'inline-flex items-center gap-1 border border-[var(--border)] px-1.5 py-0.5 text-[11px] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--accent)]'}
    >
      <Route size={12} />
      {!compact && t('search.follow')}
    </button>
  )
}

function ResultCard(
  { hit, rank, onRefine, onFollow }: {
    hit: Hit
    rank: number
    onRefine: (c: Claim) => void
    onFollow: () => void
  },
) {
  const fmt = useDateFormat()
  const at = hit.anchor?.at ?? hit.started_at
  return (
    <div className="group/card relative flex flex-col overflow-hidden border border-[var(--border)] bg-black transition-colors hover:border-[var(--accent)]">
      <Link
        onClick={() => reportOpened(rank)}
        to={recordingHref(hit)}
        className="group relative block aspect-video focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-[var(--accent)]"
        title={at ? `Play the recording from ${fmt.dateTime(at)}` : 'Open recordings'}
      >
        {/* The whole crop, on black: filling the box cut objects off and
            blew a small crop up past what it can hold. Absolute, because an
            aspect-ratio box is only a minimum — a tall crop in normal flow
            grows it, and the grid then stretches the whole row. */}
        <span className="absolute inset-0">
          <Thumb hit={hit} className="h-full w-full object-contain" />
        </span>
        <span className="absolute left-1.5 top-1.5 flex max-w-[calc(100%-2.5rem)] flex-wrap items-center gap-1">
          <Badge variant="neutral" className="capitalize">{hit.label ?? hit.event_type}</Badge>
          {hit.plate_text && <Badge variant="info" className="font-mono">{hit.plate_text}</Badge>}
        </span>
        <span className="absolute inset-0 flex items-center justify-center bg-black/30 opacity-0 transition-opacity group-hover:opacity-100">
          <span className="rounded-full bg-black/60 p-2 text-white"><Play size={16} /></span>
        </span>
        {/* Label band, as on Live View: camera and time over the picture. */}
        <span className="pointer-events-none absolute inset-x-0 bottom-0 h-9 bg-gradient-to-t from-black/80 to-transparent" />
        <span className="pointer-events-none absolute inset-x-2 bottom-1 flex items-center gap-2 text-[11px] text-white [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">
          <span className="truncate font-medium">{hit.camera_name ?? `Camera ${hit.camera_id}`}</span>
          <span className="ml-auto shrink-0 tabular-nums text-white/75">{when(at, fmt)}</span>
        </span>
      </Link>
      {/* Below the picture: a caption and claims on the left — each claim
          is itself a search, one click finds every other red van — and on
          the right "where did it go", outside the link to the video
          because following an object is a different question from
          watching this moment. */}
      <div className="flex items-start gap-1.5 border-t border-[var(--border)] bg-[var(--panel-2)] px-2 py-1">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
          {hit.caption && (
            <span className="w-full truncate text-[11px] text-[var(--text-dim)]" title={hit.caption}>{hit.caption}</span>
          )}
          <ClaimChips claims={hit.claims} onRefine={onRefine} max={3} />
        </div>
        <FollowButton onFollow={onFollow} compact />
      </div>
    </div>
  )
}

function tableColumns(
  fmt: DateFormatters,
  onRefine: (c: Claim) => void,
  onFollow: (id: number) => void,
  offset: number,
): Column<Hit>[] {
  return [
    {
      key: 'thumb',
      header: 'Snapshot',
      width: 'w-28',
      cell: (h) => (
        <div className="h-12 w-20 overflow-hidden bg-black">
          <Thumb hit={h} className="h-full w-full object-contain" />
        </div>
      ),
    },
    {
      key: 'what',
      header: 'Object',
      width: 'w-28',
      cell: (h) => <Badge variant="neutral" className="capitalize">{h.label ?? h.event_type}</Badge>,
    },
    {
      key: 'camera',
      header: 'Camera',
      width: 'w-40',
      cellClassName: 'truncate',
      cell: (h) => h.camera_name ?? `Camera ${h.camera_id}`,
    },
    {
      key: 'time',
      header: 'Time',
      width: 'w-36',
      cellClassName: 'tabular-nums text-[var(--text-dim)]',
      cell: (h) => when(h.anchor?.at ?? h.started_at, fmt),
    },
    {
      key: 'plate',
      header: 'Plate',
      width: 'w-32',
      hideBelow: 'md',
      cellClassName: 'font-mono',
      cell: (h) => h.plate_text ?? <span className="text-[var(--text-dim)]">—</span>,
    },
    {
      key: 'details',
      header: 'Details',
      hideBelow: 'lg',
      isAction: true,
      cell: (h) => (
        <div className="flex min-w-0 flex-wrap items-center gap-1">
          <ClaimChips claims={h.claims} onRefine={onRefine} max={4} />
          {h.caption && (
            <span className="min-w-0 truncate text-xs text-[var(--text-dim)]" title={h.caption}>{h.caption}</span>
          )}
          {h.claims.length === 0 && !h.caption && <span className="text-[var(--text-dim)]">—</span>}
        </div>
      ),
    },
    {
      key: 'actions',
      header: '',
      srHeader: 'Actions',
      width: 'w-40',
      align: 'right',
      isAction: true,
      cell: (h, i) => (
        <div className="flex items-center justify-end gap-1">
          <Link
            to={recordingHref(h)}
            onClick={() => reportOpened(offset + i + 1)}
            className="inline-flex items-center gap-1 border border-[var(--border)] px-1.5 py-0.5 text-[11px] text-[var(--text-dim)] hover:text-[var(--text)] hover:border-[var(--accent)]"
            title="Play the recording from this moment"
          >
            <Play size={12} /> Play
          </Link>
          <FollowButton onFollow={() => onFollow(h.id)} compact />
        </div>
      ),
    },
  ]
}
