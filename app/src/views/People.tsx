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

// People — the face directory behind the door. Capability-keyed on
// manifest `provides` ("people"), so any app that recognises faces can
// light this page; today that is the Smart Doorbell.
//
// Everything here goes through the providing app's declared surfaces —
// /state for the live feed and the strangers wall, and its actions for
// the directory (list / enroll / enroll_stranger / update / delete) —
// via core's JWT-only proxy. The page owns no data of its own.
//
// The one workflow this page exists for, and that a generic action form
// cannot give: enrolling a person FROM a snapshot the door already took.
// A stranger on the wall becomes "Priya, contractor, until Friday" in
// one dialog, no photo to go and find.

import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Camera, Clock, ImagePlus, Pencil, RefreshCw, Search, ShieldAlert, Trash2, Upload, UserPlus, UserRound, Users,
} from 'lucide-react'
import { apiService } from '../lib/apiService'
import { extractApiError } from '../lib/apiError'
import { useSnackbar } from '../components/Snackbar'
import { useTranslation } from '../i18n'
import {
  Badge, Button, Card, CardContent, CardHeader, CardTitle,
  EmptyState, ErrorCard, PageHeader, Skeleton,
  type BadgeVariant,
} from '../components/ui'
import { Modal } from '../components/Modal'
import type { RegisteredApp } from './AppCatalog'

export const PEOPLE_CAPABILITY = 'people'

/** The enabled app providing the face directory — capability-keyed. */
export function findPeopleApp(apps: RegisteredApp[] | undefined): RegisteredApp | null {
  return (
    (apps ?? []).find(
      (a) => a.enabled && ((a.manifest as any)?.provides ?? []).includes(PEOPLE_CAPABILITY)
    ) ?? null
  )
}

type Person = {
  person_id: string
  name: string
  category: string
  notes?: string
  valid_until?: string
  expired?: boolean
  thumbnail?: string | null
  /** Face samples the adapter holds for them — more angles, fewer false strangers. */
  samples?: number
  registered_at?: number | null
  last_seen?: number | null
}

type Stranger = { id: string; image: string; label: string; time: number }

type Visit = {
  message: string
  time: number
  level: 'low' | 'high' | string
  camera?: string
  name?: string | null
  category?: string | null
  similarity?: number | null
}

type CameraHealth = { camera_id: string; status: string; last_frame_age_s: number | null; error?: string | null }

type DoorState = {
  enrolled_faces?: number | null
  visits?: { known: number; unknown: number }
  camera_health?: CameraHealth[]
  stranger_gallery?: Stranger[]
  recent?: Visit[]
}

type CameraRow = { id: number; name: string }

// Fallback vocabulary when the app's list_faces reply predates
// `categories`. Same words the app's manifest declares.
const DEFAULT_CATEGORIES = ['family', 'resident', 'friend', 'staff', 'contractor', 'visitor', 'watchlist']

// Who gets which badge. Watchlist is the one category that alarms.
const CATEGORY_VARIANT: Record<string, BadgeVariant> = {
  family: 'success', resident: 'success', friend: 'success',
  staff: 'info', contractor: 'info', visitor: 'info',
  watchlist: 'destructive',
}

function ago(ts: number | null | undefined): string {
  if (!ts) return '—'
  const age = Math.max(0, Date.now() / 1000 - ts)
  if (age < 60) return 'just now'
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  if (age < 86400) return `${Math.floor(age / 3600)}h ago`
  return `${Math.floor(age / 86400)}d ago`
}

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const dataUrl = String(reader.result || '')
      resolve(dataUrl.includes(',') ? dataUrl.split(',', 2)[1] : dataUrl)
    }
    reader.onerror = () => reject(new Error('Could not read the image.'))
    reader.readAsDataURL(blob)
  })
}

/* ----------------------------- Page ----------------------------- */

export function People() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { showSuccess, showError } = useSnackbar()

  const appsQuery = useQuery({
    queryKey: ['apps'],
    queryFn: async () => {
      const { data } = await apiService.getApps()
      return (Array.isArray(data) ? data : []) as RegisteredApp[]
    },
    retry: 0,
  })
  const app = findPeopleApp(appsQuery.data)

  // Live door state — the same key the catalog's status chip uses.
  const statusQuery = useQuery({
    queryKey: ['app-status', app?.id],
    queryFn: async () => {
      const { data } = await apiService.getAppStatus(app!.id)
      return data as { state?: DoorState }
    },
    enabled: Boolean(app),
    retry: 0,
    refetchInterval: 5000,
  })
  const state: DoorState = statusQuery.data?.state ?? {}

  // The directory, through the app's list_faces action.
  const peopleQuery = useQuery({
    queryKey: ['people-directory', app?.id],
    queryFn: async () => {
      const { data } = await apiService.invokeAppAction(app!.id, 'list_faces', {})
      const rows = (data?.results ?? data?.result?.results ?? []) as Person[]
      const categories = (data?.categories ?? data?.result?.categories ?? DEFAULT_CATEGORIES) as string[]
      return { rows, categories }
    },
    enabled: Boolean(app),
    retry: 0,
    // The directory changes when someone here changes it (we invalidate),
    // or when a CLI does — a minute is plenty for that, and each answer
    // carries every avatar.
    refetchInterval: 60_000,
    refetchOnWindowFocus: true,
  })
  const people = peopleQuery.data?.rows ?? []
  const categories = peopleQuery.data?.categories ?? DEFAULT_CATEGORIES

  const camerasQuery = useQuery({
    queryKey: ['cameras'],
    queryFn: async () => {
      const { data } = await apiService.getCameras()
      const list = Array.isArray(data) ? data : (data as any)?.cameras
      return (Array.isArray(list) ? list : []) as CameraRow[]
    },
    enabled: Boolean(app),
    retry: 0,
  })

  const refreshDirectory = () => queryClient.invalidateQueries({ queryKey: ['people-directory', app?.id] })

  // ── Directory filters ──
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState<string>('all')
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    return people
      .filter((p) => category === 'all' || p.category === category)
      .filter((p) =>
        !q ||
        p.name.toLowerCase().includes(q) ||
        p.person_id.toLowerCase().includes(q) ||
        (p.notes ?? '').toLowerCase().includes(q)
      )
      .sort((a, b) => a.name.localeCompare(b.name))
  }, [people, search, category])

  const counts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const p of people) c[p.category] = (c[p.category] ?? 0) + 1
    return c
  }, [people])

  // ── Dialogs ──
  const [editor, setEditor] = useState<
    | { mode: 'add' }
    | { mode: 'edit'; person: Person }
    | { mode: 'add-photo'; person: Person }
    | { mode: 'stranger'; stranger: Stranger }
    | null
  >(null)
  const [removing, setRemoving] = useState<Person | null>(null)
  const [viewing, setViewing] = useState<Stranger | null>(null)

  const removeMutation = useMutation({
    mutationFn: async (p: Person) => apiService.invokeAppAction(app!.id, 'delete_face', { person_id: p.person_id }),
    onSuccess: (_d, p) => {
      showSuccess(`Removed ${p.name}`)
      setRemoving(null)
      refreshDirectory()
    },
    onError: (e) => showError(extractApiError(e, 'Could not remove that person.')),
  })

  if (!appsQuery.isPending && !app) {
    return (
      <section className="space-y-4">
        <PageHeader title={t('people.title')} description={t('people.description')} />
        <EmptyState
          icon={<UserRound size={28} />}
          title={t('people.noApp')}
          description="Install and enable Smart Doorbell from the App Catalog. It recognises faces at your door cameras and this page is where you tell it who is who."
        />
      </section>
    )
  }

  const cams = state.camera_health ?? []
  const camsOk = cams.filter((c) => c.status === 'ok').length
  const strangers = [...(state.stranger_gallery ?? [])].reverse()
  const visits = [...(state.recent ?? [])].reverse().slice(0, 15)

  return (
    <section className="space-y-4">
      <PageHeader
        title={t('people.title')}
        description={t('people.description')}
        actions={
          <>
            <Button size="sm" variant="outline" onClick={() => { refreshDirectory(); statusQuery.refetch() }}>
              <RefreshCw size={14} /> Refresh
            </Button>
            <Button size="sm" variant="primary" onClick={() => setEditor({ mode: 'add' })}>
              <UserPlus size={14} /> Add person
            </Button>
          </>
        }
      />

      {/* ── Headline numbers ── */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <Stat icon={<Users size={16} />} value={state.enrolled_faces ?? people.length} label="enrolled" />
        <Stat icon={<UserRound size={16} />} value={state.visits?.known ?? 0} label="known visits" />
        <Stat icon={<ShieldAlert size={16} />} value={state.visits?.unknown ?? 0} label="strangers" tone={(state.visits?.unknown ?? 0) > 0 ? 'warn' : undefined} />
        <Stat
          icon={<Camera size={16} />}
          value={cams.length ? `${camsOk}/${cams.length}` : '—'}
          label="cameras answering"
          tone={cams.length && camsOk < cams.length ? 'bad' : undefined}
        />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-5 gap-4">
        {/* ── Directory ── */}
        <Card className="xl:col-span-3">
          <CardHeader>
            <Users size={16} className="text-[var(--text-dim)]" />
            <CardTitle>Directory</CardTitle>
            <span className="ml-auto text-xs text-[var(--text-dim)]">{people.length} people</span>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <label className="relative flex-1 min-w-[180px]">
                <Search size={14} className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" />
                <input
                  className="w-full pl-7 pr-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
                  placeholder="Search by name, id or notes"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
              <div className="flex flex-wrap gap-1">
                <Chip active={category === 'all'} onClick={() => setCategory('all')}>All</Chip>
                {categories.map((c) => (
                  <Chip key={c} active={category === c} onClick={() => setCategory(c)}>
                    {c}{counts[c] ? ` · ${counts[c]}` : ''}
                  </Chip>
                ))}
              </div>
            </div>

            {peopleQuery.isPending ? (
              <div className="space-y-2">
                <Skeleton className="h-12" /><Skeleton className="h-12" /><Skeleton className="h-12" />
              </div>
            ) : peopleQuery.isError ? (
              <ErrorCard
                title="Directory unavailable"
                message={extractApiError(peopleQuery.error, 'The face adapter did not answer. Is insightface-adapter running?')}
                onRetry={() => peopleQuery.refetch()}
              />
            ) : filtered.length === 0 ? (
              <EmptyState
                icon={<UserPlus size={24} />}
                title={people.length === 0 ? 'Nobody enrolled yet' : 'No one matches'}
                description={
                  people.length === 0
                    ? 'Add the people who should be greeted rather than flagged. A clear, front-facing photo in good light is all it takes — or wait for the door to see them and enrol from the strangers wall.'
                    : 'Try another name or clear the category filter.'
                }
                action={people.length === 0 ? (
                  <Button size="sm" variant="primary" onClick={() => setEditor({ mode: 'add' })}>
                    <UserPlus size={14} /> Add person
                  </Button>
                ) : undefined}
              />
            ) : (
              <ul className="divide-y divide-[var(--border)]">
                {filtered.map((p) => (
                  <li key={p.person_id} className="flex items-center gap-3 py-2">
                    <Avatar src={p.thumbnail} name={p.name} />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium truncate">{p.name}</span>
                        <Badge variant={CATEGORY_VARIANT[p.category] ?? 'neutral'}>{p.category}</Badge>
                        {p.expired && <Badge variant="critical">pass expired</Badge>}
                        {!p.expired && p.valid_until && (
                          <span className="text-xs text-[var(--text-dim)] inline-flex items-center gap-1">
                            <Clock size={12} /> until {p.valid_until}
                          </span>
                        )}
                      </div>
                      <div className="text-xs text-[var(--text-dim)] truncate">
                        {p.notes ? <span>{p.notes} · </span> : null}
                        <span title={p.person_id}>last seen {ago(p.last_seen)}</span>
                        <span title="Face samples on file. Five or more, from the door camera itself, is where recognition gets reliable."> · {p.samples ?? 1} {(p.samples ?? 1) === 1 ? 'photo' : 'photos'}</span>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 shrink-0">
                      <Button size="sm" variant="ghost" title="Add a photo" onClick={() => setEditor({ mode: 'add-photo', person: p })}>
                        <ImagePlus size={14} />
                      </Button>
                      <Button size="sm" variant="ghost" title="Edit" onClick={() => setEditor({ mode: 'edit', person: p })}>
                        <Pencil size={14} />
                      </Button>
                      <Button size="sm" variant="ghost" title="Remove" onClick={() => setRemoving(p)}>
                        <Trash2 size={14} />
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {/* ── At the door ── */}
        <div className="xl:col-span-2 space-y-4">
          <Card>
            <CardHeader>
              <ShieldAlert size={16} className="text-[var(--text-dim)]" />
              <CardTitle>Strangers at the door</CardTitle>
              {statusQuery.isFetching && <span className="ml-auto text-xs text-[var(--text-dim)]">updating…</span>}
            </CardHeader>
            <CardContent>
              {strangers.length === 0 ? (
                <div className="text-sm text-[var(--text-dim)] py-4 text-center">
                  No unrecognised faces yet. When the door sees someone it does not know, they appear here — click one to enrol them.
                </div>
              ) : (
                <div className="grid grid-cols-3 sm:grid-cols-4 gap-2">
                  {strangers.map((s, i) => (
                    <button
                      key={s.id ?? i}
                      type="button"
                      className="text-left rounded border border-[var(--border)] bg-[var(--bg-2)] overflow-hidden hover:border-[var(--text-dim)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)] disabled:cursor-default"
                      // An app older than stranger ids can show the wall but
                      // cannot serve the crop behind it.
                      disabled={!s.id}
                      onClick={() => s.id && setViewing(s)}
                      title={s.id ? 'Review or enrol' : 'Update the app to enrol from here'}
                    >
                      <img src={s.image} alt="unrecognised face" className="w-full h-20 object-cover" loading="lazy" />
                      <div className="px-1.5 py-1 text-[11px] leading-tight">
                        <div className="truncate">{s.label}</div>
                        <div className="text-[var(--text-dim)]">{ago(s.time)}</div>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <Clock size={16} className="text-[var(--text-dim)]" />
              <CardTitle>Recent visitors</CardTitle>
            </CardHeader>
            <CardContent>
              {visits.length === 0 ? (
                <div className="text-sm text-[var(--text-dim)] py-4 text-center">Nobody yet.</div>
              ) : (
                <ul className="divide-y divide-[var(--border)] text-sm">
                  {visits.map((v, i) => (
                    <li key={i} className="flex items-center gap-2 py-1.5">
                      <span className={`font-medium ${v.level === 'high' ? 'text-[var(--danger)]' : 'text-[var(--ok)]'}`}>
                        {v.name ?? 'Unknown visitor'}
                      </span>
                      {v.category && <Badge variant={CATEGORY_VARIANT[v.category] ?? 'neutral'}>{v.category}</Badge>}
                      <span className="text-[var(--text-dim)] truncate">{v.camera}</span>
                      {typeof v.similarity === 'number' && (
                        <span className="text-xs text-[var(--text-dim)]">{v.similarity.toFixed(2)}</span>
                      )}
                      <span className="ml-auto text-xs text-[var(--text-dim)] shrink-0">{ago(v.time)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </CardContent>
          </Card>

          {cams.some((c) => c.status !== 'ok') && (
            <Card>
              <CardHeader>
                <Camera size={16} className="text-[var(--text-dim)]" />
                <CardTitle>Cameras</CardTitle>
              </CardHeader>
              <CardContent>
                <ul className="text-sm divide-y divide-[var(--border)]">
                  {cams.map((c) => (
                    <li key={c.camera_id} className="flex items-center gap-2 py-1.5">
                      <span className="font-medium">{c.camera_id}</span>
                      <Badge variant={c.status === 'ok' ? 'success' : c.status === 'waiting' ? 'neutral' : c.status === 'stalled' ? 'warning' : 'destructive'}>
                        {c.status}
                      </Badge>
                      <span className="text-xs text-[var(--text-dim)] truncate">{c.error ?? (c.last_frame_age_s != null ? `${c.last_frame_age_s}s ago` : '')}</span>
                    </li>
                  ))}
                </ul>
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      {editor && app && (
        <PersonEditor
          appId={app.id}
          categories={categories}
          cameras={camerasQuery.data ?? []}
          initial={editor}
          onClose={() => setEditor(null)}
          onSaved={(msg) => { showSuccess(msg); setEditor(null); refreshDirectory(); statusQuery.refetch() }}
        />
      )}

      {viewing && app && (
        <StrangerDialog
          appId={app.id}
          stranger={viewing}
          people={people}
          onClose={() => setViewing(null)}
          onEnrol={() => { const s = viewing; setViewing(null); setEditor({ mode: 'stranger', stranger: s }) }}
          onAssigned={(msg) => { showSuccess(msg); setViewing(null); refreshDirectory(); statusQuery.refetch() }}
        />
      )}

      <Modal
        open={Boolean(removing)}
        title="Remove this person?"
        onClose={() => setRemoving(null)}
        widthClassName="max-w-md"
        footer={
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={() => setRemoving(null)}>Cancel</Button>
            <Button variant="danger" disabled={removeMutation.isPending} onClick={() => removing && removeMutation.mutate(removing)}>
              <Trash2 size={14} /> Remove
            </Button>
          </div>
        }
      >
        {removing && (
          <div className="flex items-center gap-3">
            <Avatar src={removing.thumbnail} name={removing.name} size="lg" />
            <p className="text-sm">
              <b>{removing.name}</b> will be treated as a stranger from the next visit. The face data is deleted from the adapter; this cannot be undone.
            </p>
          </div>
        )}
      </Modal>
    </section>
  )
}

/* --------------------------- Pieces ---------------------------- */

function Stat({ icon, value, label, tone }: { icon: React.ReactNode; value: React.ReactNode; label: string; tone?: 'warn' | 'bad' }) {
  const color = tone === 'bad' ? 'text-[var(--danger)]' : tone === 'warn' ? 'text-[var(--warn)]' : 'text-[var(--text)]'
  return (
    <Card>
      <CardContent className="flex items-center gap-3 py-3">
        <span className="text-[var(--text-dim)]">{icon}</span>
        <div>
          <div className={`text-xl font-semibold leading-none ${color}`}>{value}</div>
          <div className="text-xs text-[var(--text-dim)] mt-1">{label}</div>
        </div>
      </CardContent>
    </Card>
  )
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-2 py-1 text-xs rounded-full border transition-colors ${
        active
          ? 'border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--text)]'
          : 'border-[var(--border)] bg-[var(--bg-2)] text-[var(--text-dim)] hover:text-[var(--text)]'
      }`}
    >
      {children}
    </button>
  )
}

function Avatar({ src, name, size = 'md' }: { src?: string | null; name: string; size?: 'md' | 'lg' }) {
  const dim = size === 'lg' ? 'w-16 h-16 text-xl' : 'w-10 h-10 text-sm'
  if (src) {
    return <img src={src} alt={name} className={`${dim} rounded-full object-cover border border-[var(--border)] shrink-0`} />
  }
  const initials = name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]?.toUpperCase()).join('')
  return (
    <div className={`${dim} rounded-full grid place-items-center bg-[var(--bg-2)] border border-[var(--border)] text-[var(--text-dim)] font-medium shrink-0`}>
      {initials || '?'}
    </div>
  )
}

/* ----------------------- Stranger review ------------------------ */

function StrangerDialog({ appId, stranger, people, onClose, onEnrol, onAssigned }: {
  appId: string; stranger: Stranger; people: Person[]
  onClose: () => void; onEnrol: () => void; onAssigned: (message: string) => void
}) {
  // "This is Alice": the capture joins Alice's samples — the door learns
  // its own angle and light — and the tile leaves the wall.
  const [assignTo, setAssignTo] = useState<string>('')
  const assign = useMutation({
    mutationFn: async () => {
      if (!assignTo) throw new Error('Pick who this is.')
      await apiService.invokeAppAction(appId, 'enroll_stranger', { stranger_id: stranger.id, person_id: assignTo })
      const who = people.find((p) => p.person_id === assignTo)?.name ?? assignTo
      return `Added this capture to ${who}`
    },
    onSuccess: (msg) => onAssigned(msg),
  })
  const sorted = [...people].sort((a, b) => a.name.localeCompare(b.name))
  // The wall carries a small thumbnail; the action returns the real crop.
  const crop = useQuery({
    queryKey: ['stranger-image', appId, stranger.id],
    queryFn: async () => {
      const { data } = await apiService.invokeAppAction(appId, 'stranger_image', { stranger_id: stranger.id })
      const b64 = data?.image ?? data?.result?.image
      return b64 ? `data:image/jpeg;base64,${b64}` : stranger.image
    },
    retry: 0,
    staleTime: 60_000,
  })
  return (
    <Modal
      open
      title={`Unrecognised face · ${stranger.label}`}
      onClose={onClose}
      widthClassName="max-w-md"
      footer={
        <div className="flex justify-between gap-2">
          <span className="text-xs text-[var(--text-dim)] self-center">{ago(stranger.time)}</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose}>Close</Button>
            <Button variant="primary" onClick={onEnrol}><UserPlus size={14} /> New person</Button>
          </div>
        </div>
      }
    >
      <div className="space-y-3">
        <img
          src={crop.data ?? stranger.image}
          alt="unrecognised face"
          className="w-full max-h-72 object-contain rounded border border-[var(--border)] bg-black"
        />
        {sorted.length > 0 && (
          <div className="rounded border border-[var(--border)] bg-[var(--bg-2)] p-2 space-y-1.5">
            <div className="text-xs font-medium">Someone already enrolled?</div>
            <div className="flex gap-1">
              <select
                className="flex-1 min-w-0 px-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg)] text-[var(--text)]"
                value={assignTo}
                onChange={(e) => setAssignTo(e.target.value)}
              >
                <option value="">This is…</option>
                {sorted.map((p) => (
                  <option key={p.person_id} value={p.person_id}>{p.name} · {p.category} · {p.samples ?? 1} {(p.samples ?? 1) === 1 ? 'photo' : 'photos'}</option>
                ))}
              </select>
              <Button size="sm" variant="primary" disabled={!assignTo || assign.isPending} onClick={() => assign.mutate()}>
                <ImagePlus size={14} /> {assign.isPending ? 'Adding…' : 'Add to them'}
              </Button>
            </div>
            <p className="text-[11px] text-[var(--text-dim)]">
              The door missed them — so this capture is exactly the angle and light it needs. Adding it makes the next visit a match.
              {assign.isError && <span className="text-[var(--danger)]"> {extractApiError(assign.error, 'Could not add.')}</span>}
            </p>
          </div>
        )}
        <p className="text-xs text-[var(--text-dim)]">
          Not enrolled yet? <b>New person</b> starts them from this photo. If they are a genuine stranger, nothing to do: the snapshot is only kept until it scrolls off the wall.
        </p>
      </div>
    </Modal>
  )
}

/* ------------------------ Person editor ------------------------ */

type EditorInitial =
  | { mode: 'add' }
  | { mode: 'edit'; person: Person }
  | { mode: 'add-photo'; person: Person }
  | { mode: 'stranger'; stranger: Stranger }

function PersonEditor({ appId, categories, cameras, initial, onClose, onSaved }: {
  appId: string
  categories: string[]
  cameras: CameraRow[]
  initial: EditorInitial
  onClose: () => void
  onSaved: (message: string) => void
}) {
  const editing = initial.mode === 'edit' || initial.mode === 'add-photo' ? initial.person : null
  const photoOnly = initial.mode === 'add-photo'
  // A new photo on an existing person ADDS a sample by default; "start
  // over" replaces their set (the haircut case, or a bad first enrolment).
  const [replace, setReplace] = useState(false)
  const [name, setName] = useState(editing?.name ?? '')
  const [category, setCategory] = useState(editing?.category ?? (initial.mode === 'stranger' ? 'visitor' : categories[0] ?? 'family'))
  const [notes, setNotes] = useState(editing?.notes ?? '')
  const [validUntil, setValidUntil] = useState(editing?.valid_until ?? '')
  // Photo: base64 (no data: prefix) + a preview. Not needed for a
  // metadata-only edit, and the stranger flow brings its own crop.
  const [photoB64, setPhotoB64] = useState<string | null>(null)
  const [preview, setPreview] = useState<string | null>(initial.mode === 'stranger' ? initial.stranger.image : editing?.thumbnail ?? null)
  const [photoSource, setPhotoSource] = useState<'upload' | 'camera'>('upload')
  const [cameraId, setCameraId] = useState<number | ''>(cameras[0]?.id ?? '')
  const [snapping, setSnapping] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => { if (cameraId === '' && cameras[0]) setCameraId(cameras[0].id) }, [cameras, cameraId])

  const needsPhoto = initial.mode === 'add' || (initial.mode === 'edit' && photoB64 !== null)
  const wantsExpiry = category === 'contractor' || category === 'visitor'

  const onFile = async (file: File | undefined) => {
    setErr(null)
    if (!file) return
    if (!file.type.startsWith('image/')) return setErr('Please choose an image file.')
    if (file.size > 6 * 1024 * 1024) return setErr('Image is too large (max ~6 MB).')
    try {
      const b64 = await blobToBase64(file)
      setPhotoB64(b64)
      setPreview(`data:${file.type};base64,${b64}`)
    } catch (e) {
      setErr(extractApiError(e, 'Could not read the file.'))
    }
  }

  const snap = async () => {
    if (cameraId === '') return
    setSnapping(true); setErr(null)
    try {
      const { data } = await apiService.getCameraSnapshot(Number(cameraId))
      const b64 = await blobToBase64(data as Blob)
      setPhotoB64(b64)
      setPreview(`data:image/jpeg;base64,${b64}`)
    } catch (e) {
      setErr(extractApiError(e, 'Could not take a snapshot from that camera.'))
    } finally {
      setSnapping(false)
    }
  }

  const save = useMutation({
    mutationFn: async () => {
      const trimmed = name.trim()
      if (!trimmed && initial.mode !== 'add-photo') throw new Error('A name is required.')
      if (validUntil && !/^\d{4}-\d{2}-\d{2}$/.test(validUntil)) throw new Error('Valid until must be a date.')
      if (initial.mode === 'stranger') {
        await apiService.invokeAppAction(appId, 'enroll_stranger', {
          stranger_id: initial.stranger.id, name: trimmed, category, notes, valid_until: validUntil,
        })
        return `Enrolled ${trimmed} from the door snapshot`
      }
      if (initial.mode === 'add-photo') {
        if (!photoB64) throw new Error('Add a photo — upload one or take it from a camera.')
        await apiService.invokeAppAction(appId, 'enroll_face', {
          person_id: initial.person.person_id, image: photoB64, append: !replace,
          name: replace ? trimmed : '', category: replace ? category : '',
        })
        return replace ? `Replaced ${initial.person.name}'s photos` : `Added a photo to ${initial.person.name}`
      }
      if (initial.mode === 'edit' && photoB64 === null) {
        await apiService.invokeAppAction(appId, 'update_face', {
          person_id: initial.person.person_id, name: trimmed, category, notes, valid_until: validUntil,
        })
        return `Updated ${trimmed}`
      }
      if (!photoB64) throw new Error('Add a photo — upload one or take it from a camera.')
      if (initial.mode === 'edit') {
        await apiService.invokeAppAction(appId, 'update_face', {
          person_id: initial.person.person_id, name: trimmed, category, notes, valid_until: validUntil,
        })
        await apiService.invokeAppAction(appId, 'enroll_face', {
          person_id: initial.person.person_id, image: photoB64, append: !replace,
          name: trimmed, category, notes, valid_until: validUntil,
        })
        return replace ? `Re-enrolled ${trimmed} from a fresh photo` : `Updated ${trimmed} and added a photo`
      }
      await apiService.invokeAppAction(appId, 'enroll_face', {
        name: trimmed, image: photoB64, category, notes, valid_until: validUntil, person_id: '',
      })
      return `Enrolled ${trimmed}`
    },
    onSuccess: (msg) => onSaved(msg),
    onError: (e) => setErr(extractApiError(e, 'Could not save.')),
  })

  const title = initial.mode === 'add' ? 'Add a person'
    : initial.mode === 'edit' ? `Edit ${initial.person.name}`
    : initial.mode === 'add-photo' ? `Add a photo of ${initial.person.name}`
    : `Enrol from ${initial.stranger.label}`

  return (
    <Modal
      open
      title={title}
      onClose={onClose}
      widthClassName="max-w-2xl"
      footer={
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-[var(--danger)]">{err}</span>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose}>Cancel</Button>
            <Button variant="primary" disabled={save.isPending} onClick={() => save.mutate()}>
              {save.isPending ? 'Saving…' : initial.mode === 'edit' ? 'Save' : initial.mode === 'add-photo' ? 'Add photo' : 'Enrol'}
            </Button>
          </div>
        </div>
      }
    >
      <div className="grid grid-cols-1 md:grid-cols-5 gap-4">
        {/* ── Photo ── */}
        <div className="md:col-span-2 space-y-2">
          <div className="aspect-square w-full rounded border border-[var(--border)] bg-black overflow-hidden grid place-items-center">
            {preview ? (
              <img src={preview} alt="face" className="w-full h-full object-cover" />
            ) : (
              <span className="text-xs text-[var(--text-dim)] px-4 text-center">No photo yet</span>
            )}
          </div>
          {initial.mode !== 'stranger' && (
            <>
              <div className="flex gap-1">
                <Chip active={photoSource === 'upload'} onClick={() => setPhotoSource('upload')}>Upload</Chip>
                <Chip active={photoSource === 'camera'} onClick={() => setPhotoSource('camera')}>From a camera</Chip>
              </div>
              {photoSource === 'upload' ? (
                <>
                  <input ref={fileRef} type="file" accept="image/*" className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
                  <Button size="sm" variant="outline" className="w-full" onClick={() => fileRef.current?.click()}>
                    <Upload size={14} /> {photoB64 ? 'Choose another' : 'Choose a photo'}
                  </Button>
                </>
              ) : (
                <div className="flex gap-1">
                  <select
                    className="flex-1 min-w-0 px-2 py-1 text-xs rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
                    value={cameraId}
                    onChange={(e) => setCameraId(e.target.value === '' ? '' : Number(e.target.value))}
                  >
                    {cameras.length === 0 && <option value="">No cameras</option>}
                    {cameras.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                  </select>
                  <Button size="sm" variant="outline" disabled={snapping || cameraId === ''} onClick={snap}>
                    <Camera size={14} /> {snapping ? '…' : 'Snap'}
                  </Button>
                </div>
              )}
              {initial.mode === 'edit' && photoB64 === null && (
                <p className="text-[11px] text-[var(--text-dim)]">Keeping their {editing?.samples ?? 1} {(editing?.samples ?? 1) === 1 ? 'photo' : 'photos'}. Add one to cover a new angle, glasses, a beard.</p>
              )}
              {editing && photoB64 !== null && (
                <label className="flex items-start gap-2 text-[11px] text-[var(--text-dim)]">
                  <input type="checkbox" className="mt-0.5" checked={replace} onChange={(e) => setReplace(e.target.checked)} />
                  <span>Start over — replace their existing {editing.samples ?? 1} {(editing.samples ?? 1) === 1 ? 'photo' : 'photos'} with this one. Off: this photo is added to them.</span>
                </label>
              )}
            </>
          )}
          <p className="text-[11px] text-[var(--text-dim)]">
            Best results: one person, facing the camera, good light, no sunglasses or hat, face at least a third of the frame. Skip infrared night shots.
          </p>
        </div>

        {/* ── Details ── */}
        {photoOnly && editing ? (
          <div className="md:col-span-3 space-y-3">
            <div className="flex items-center gap-3">
              <Avatar src={editing.thumbnail} name={editing.name} size="lg" />
              <div>
                <div className="font-medium">{editing.name}</div>
                <div className="text-xs text-[var(--text-dim)]">{editing.category} · {editing.samples ?? 1} {(editing.samples ?? 1) === 1 ? 'photo' : 'photos'} on file</div>
              </div>
            </div>
            <p className="text-sm text-[var(--text-dim)]">
              Each photo is another view the door can match against. The most useful ones are the ones this camera actually sees: from the door camera, at the usual angle, in evening light, with and without glasses. Five is where recognition gets reliable; twenty covers most conditions.
            </p>
            <p className="text-xs text-[var(--text-dim)]">
              Tip: the fastest way to build this up is the strangers wall — when the door misses them, click the capture and choose their name.
            </p>
          </div>
        ) : (
        <div className="md:col-span-3 space-y-3">
          <Field label="Name">
            <input
              autoFocus
              className="w-full px-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Alex Rivera"
            />
          </Field>
          <Field label="Who they are" hint="Sets how the door reacts: family, resident and friend are greeted quietly; staff, contractor and visitor are noted; watchlist alarms.">
            <div className="flex flex-wrap gap-1">
              {categories.map((c) => (
                <Chip key={c} active={category === c} onClick={() => setCategory(c)}>{c}</Chip>
              ))}
            </div>
          </Field>
          <Field label="Notes" hint="Flat or unit, department, vehicle, who to call — what a guard should see next to the name.">
            <input
              className="w-full px-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Flat 4B · white Swift KA01AB1234"
            />
          </Field>
          <Field
            label="Valid until"
            hint={wantsExpiry
              ? 'A pass. After this date they are recognised but flagged as expired instead of greeted.'
              : 'Optional. Leave blank for people who are always welcome.'}
          >
            <input
              type="date"
              className="px-2 py-1.5 text-sm rounded border border-[var(--border)] bg-[var(--bg-2)] text-[var(--text)]"
              value={validUntil}
              onChange={(e) => setValidUntil(e.target.value)}
            />
            {validUntil && (
              <Button size="sm" variant="ghost" className="ml-1" onClick={() => setValidUntil('')}>clear</Button>
            )}
          </Field>
          {category === 'watchlist' && (
            <div className="text-xs rounded border border-[var(--danger)]/40 bg-[var(--danger)]/5 p-2 text-[var(--text)]">
              A watchlist match raises a <b>high</b> alert every time this face is seen. Use it for people who must not be let in unnoticed, not for anyone you would rather just not greet.
            </div>
          )}
          {needsPhoto && !photoB64 && initial.mode === 'add' && (
            <p className="text-xs text-[var(--text-dim)]">A photo is required to enrol.</p>
          )}
        </div>
        )}
      </div>
    </Modal>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="text-xs font-medium text-[var(--text-dim)] mb-1">{label}</div>
      {children}
      {hint && <div className="text-[11px] text-[var(--text-dim)] mt-1">{hint}</div>}
    </label>
  )
}
