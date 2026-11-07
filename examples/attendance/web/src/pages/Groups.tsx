// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's Manage Courses, for any kind of group: a class section, a
// course, a work shift. Who is in it, and its own rules if it needs them.

import { useEffect, useMemo, useState } from 'react'
import { BookOpen, Pencil, Plus, Search, Trash2, UserMinus, UserPlus, Users } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { POLICY_LIMITS, api, policyErrors, type Group, type Person, type Policy } from '../attendance'
import { useFeedback } from '../components/feedback'
import { Badge, Button, Card, Empty, ErrorNote, Field, Loading, Modal, PageTitle, Table, ViewSwitch, inputClass, td, th, useViewMode } from '../components/ui'
import { useGroups, useLoad } from '../hooks'

/** Browsers name zones as CLDR does, which keeps names IANA has since
 * changed: Chrome offers Asia/Calcutta and no Asia/Kolkata. The list
 * shows today's IANA name for each (core accepts both). */
const RENAMED: Record<string, string> = {
  'Asia/Calcutta': 'Asia/Kolkata',
  'Asia/Katmandu': 'Asia/Kathmandu',
  'Asia/Saigon': 'Asia/Ho_Chi_Minh',
  'Asia/Rangoon': 'Asia/Yangon',
  'Asia/Thimbu': 'Asia/Thimphu',
  'Asia/Dacca': 'Asia/Dhaka',
  'Asia/Macao': 'Asia/Macau',
  'Asia/Ujung_Pandang': 'Asia/Makassar',
  'Europe/Kiev': 'Europe/Kyiv',
  'Europe/Uzhgorod': 'Europe/Kyiv',
  'Europe/Zaporozhye': 'Europe/Kyiv',
  'Africa/Asmera': 'Africa/Asmara',
  'Atlantic/Faeroe': 'Atlantic/Faroe',
  'America/Godthab': 'America/Nuuk',
  'America/Buenos_Aires': 'America/Argentina/Buenos_Aires',
  'America/Catamarca': 'America/Argentina/Catamarca',
  'America/Cordoba': 'America/Argentina/Cordoba',
  'America/Jujuy': 'America/Argentina/Jujuy',
  'America/Mendoza': 'America/Argentina/Mendoza',
  'America/Indianapolis': 'America/Indiana/Indianapolis',
  'America/Louisville': 'America/Kentucky/Louisville',
  'America/Coral_Harbour': 'America/Atikokan',
  'Pacific/Truk': 'Pacific/Chuuk',
  'Pacific/Ponape': 'Pacific/Pohnpei',
  'Pacific/Enderbury': 'Pacific/Kanton',
}

const current = (zone: string) => RENAMED[zone] ?? zone

function timeZones(): string[] {
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (k: string) => string[] }
    const zones = (intl.supportedValuesOf?.('timeZone') ?? []).map(current)
    return [...new Set(['UTC', ...zones])].sort((a, b) => (a === 'UTC' ? -1 : b === 'UTC' ? 1 : a.localeCompare(b)))
  } catch {
    return []
  }
}

const browserZone = current(Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC')

export default function Groups() {
  const { can } = useAuth()
  const { toast, confirm } = useFeedback()
  const groups = useGroups()
  const [editing, setEditing] = useState<Group | 'new' | null>(null)
  const [members, setMembers] = useState<Group | null>(null)
  const [view, setView] = useViewMode('groups', 'table')

  const remove = async (g: Group) => {
    const ok = await confirm({
      title: `Delete ${g.name}?`,
      body: 'Only a group that never had a session can be deleted. One with attendance history can be made inactive instead.',
      confirm: 'Delete',
      danger: true,
    })
    if (!ok) return
    try {
      await api.deleteGroup(g.id)
      toast('success', `${g.name} deleted`)
      groups.reload()
    } catch (e) {
      toast('error', errorText(e))
    }
  }


  const actions = (g: Group) => (
    <>
      <Button variant="secondary" size="sm" onClick={() => setMembers(g)}>
        <Users className="h-3.5 w-3.5" /> Members
      </Button>
      {can.manage && (
        <>
          <Button variant="ghost" size="sm" onClick={() => setEditing(g)}>
            <Pencil className="h-3.5 w-3.5" /> Edit
          </Button>
          <Button variant="ghost" size="sm" onClick={() => remove(g)} aria-label={`Delete ${g.name}`}>
            <Trash2 className="h-3.5 w-3.5 text-red-600" />
          </Button>
        </>
      )}
    </>
  )

  return (
    <>
      <PageTitle
        title="Groups"
        subtitle="Classes, sections or shifts. Attendance is taken per group, at the times on the schedule."
        actions={
          <div className="flex items-center gap-2">
            <ViewSwitch value={view} onChange={setView} />
            {can.manage && (
              <Button onClick={() => setEditing('new')}>
                <Plus className="h-4 w-4" /> New group
              </Button>
            )}
          </div>
        }
      />
      {groups.error ? (
        <ErrorNote message={groups.error} onRetry={groups.reload} />
      ) : !groups.data ? (
        <Loading />
      ) : !groups.data.length ? (
        <Card>
          <Empty icon={<BookOpen className="h-10 w-10" />} title="No groups yet">
            Create a group for each class section or shift you take attendance for.
          </Empty>
        </Card>
      ) : view === 'table' ? (
        <Card>
          <Table>
            <thead>
              <tr>
                <th className={th}>Group</th>
                <th className={th}>Members</th>
                <th className={th}>Time zone</th>
                <th className={th}>Rules</th>
                <th className={th}>Status</th>
                <th className={th}>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100 bg-white">
              {groups.data.map((g) => (
                <tr key={g.id} className={`hover:bg-gray-50 ${g.active ? '' : 'text-gray-400'}`}>
                  <td className={td}>
                    <div className="font-medium text-gray-900">{g.name}</div>
                    <div className="text-xs text-gray-500">{[g.code, g.kind_label].filter(Boolean).join(' · ') || 'No code'}</div>
                  </td>
                  <td className={td}>
                    <Badge tone="indigo">
                      <Users className="h-3 w-3" /> {g.members}
                    </Badge>
                  </td>
                  <td className={`${td} whitespace-nowrap`}>{g.timezone}</td>
                  <td className={td}>
                    {Object.keys(g.policy ?? {}).length > 0 ? <Badge tone="amber">Own rules</Badge> : <span className="text-gray-400">Site rules</span>}
                  </td>
                  <td className={td}>{g.active ? <Badge tone="green">Active</Badge> : <Badge>Inactive</Badge>}</td>
                  <td className={`${td} whitespace-nowrap text-right`}>
                    <div className="inline-flex gap-1">{actions(g)}</div>
                  </td>
                </tr>
              ))}
            </tbody>
          </Table>
        </Card>
      ) : (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {groups.data.map((g) => (
            <Card key={g.id} className={`flex flex-col p-4 ${g.active ? '' : 'opacity-60'}`}>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <h3 className="text-base font-semibold text-gray-900">{g.name}</h3>
                  <div className="mt-0.5 text-xs text-gray-500">
                    {[g.code, g.kind_label].filter(Boolean).join(' · ') || 'No code'}
                  </div>
                </div>
                {!g.active && <Badge>Inactive</Badge>}
              </div>
              <div className="mt-3 flex flex-wrap gap-2 text-xs text-gray-600">
                <Badge tone="indigo">
                  <Users className="h-3 w-3" /> {g.members} members
                </Badge>
                <Badge>{g.timezone}</Badge>
                {Object.keys(g.policy ?? {}).length > 0 && <Badge tone="amber">Own rules</Badge>}
              </div>
              <div className="mt-4 flex flex-wrap gap-2 border-t pt-3">{actions(g)}</div>
            </Card>
          ))}
        </div>
      )}

      {editing && (
        <GroupForm
          group={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            groups.reload()
          }}
        />
      )}
      {members && (
        <Members
          group={members}
          onClose={() => {
            setMembers(null)
            groups.reload()
          }}
        />
      )}
    </>
  )
}

const POLICY_FIELDS: { key: keyof Policy; label: string; hint: string; step: number }[] = [
  { key: 'present_ratio', label: 'Present when seen in at least (0–1)', hint: '0.6 = 60% of the session', step: 0.05 },
  { key: 'late_grace_minutes', label: 'Late after (minutes)', hint: 'First seen later than this is LATE', step: 1 },
  { key: 'match_threshold', label: 'Face match threshold (0.3–0.95)', hint: 'Higher = stricter', step: 0.05 },
  { key: 'bucket_minutes', label: 'Slice length (minutes)', hint: 'The session is counted in slices', step: 1 },
  { key: 'attempt_interval_s', label: 'Look every (seconds)', hint: 'At most half a slice', step: 10 },
  { key: 'finalize_grace_minutes', label: 'Decide after the end (minutes)', hint: 'At least 1', step: 1 },
  { key: 'min_coverage_ratio', label: 'Counted when watched for at least (0–1)', hint: 'Less is left out of the rates', step: 0.05 },
]

function GroupForm({ group, onClose, onSaved }: { group: Group | null; onClose: () => void; onSaved: () => void }) {
  const { toast } = useFeedback()
  const zones = useMemo(timeZones, [])
  const [name, setName] = useState(group?.name ?? '')
  const [code, setCode] = useState(group?.code ?? '')
  const [kind, setKind] = useState(group?.kind_label ?? '')
  const [tz, setTz] = useState(group?.timezone ?? browserZone)
  const [active, setActive] = useState(group?.active ?? true)
  const [policy, setPolicy] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(group?.policy ?? {}).map(([k, v]) => [k, String(v)])),
  )
  const [advanced, setAdvanced] = useState(Object.keys(group?.policy ?? {}).length > 0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const wrong = policyErrors(policy)
  const anyWrong = Object.keys(wrong).length > 0

  const save = async () => {
    if (anyWrong) {
      setAdvanced(true)
      return
    }
    setBusy(true)
    setError(null)
    const overrides: Partial<Policy> = {}
    for (const [k, v] of Object.entries(policy)) if (v.trim() !== '') overrides[k as keyof Policy] = Number(v)
    const body = { name: name.trim(), code: code.trim() || null, kind_label: kind.trim() || null, timezone: tz, active, policy: overrides }
    try {
      if (group) await api.updateGroup(group.id, body)
      else await api.createGroup(body)
      toast('success', group ? 'Group saved' : `${body.name} created`)
      onSaved()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      title={group ? `Edit ${group.name}` : 'New group'}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} busy={busy} disabled={!name.trim() || !tz || anyWrong}>
            {group ? 'Save' : 'Create'}
          </Button>
        </>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="sm:col-span-2">
          <Field label="Name" hint="For example: B.Tech CSE 3rd sem, Section A">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} autoFocus />
          </Field>
        </div>
        <Field label="Code" hint="Optional, unique">
          <input className={inputClass} value={code} onChange={(e) => setCode(e.target.value)} />
        </Field>
        <Field label="Kind" hint="Class, section, shift…">
          <input className={inputClass} value={kind} onChange={(e) => setKind(e.target.value)} />
        </Field>
        <div className="sm:col-span-2">
          <Field label="Time zone" hint="The schedule's times are in this zone">
            {zones.length ? (
              <select className={inputClass} value={tz} onChange={(e) => setTz(e.target.value)}>
                {!zones.includes(tz) && <option value={tz}>{tz}</option>}
                {zones.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
            ) : (
              <input className={inputClass} value={tz} onChange={(e) => setTz(e.target.value)} />
            )}
          </Field>
        </div>
        <label className="flex items-center gap-2 text-sm text-gray-700 sm:col-span-2">
          <input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={active} onChange={(e) => setActive(e.target.checked)} />
          Active — sessions are scheduled for it
        </label>
      </div>
      <div className="mt-5 border-t pt-4">
        <button className="text-sm font-medium text-indigo-700" onClick={() => setAdvanced((a) => !a)}>
          {advanced ? '▾' : '▸'} This group's own rules
        </button>
        {anyWrong && !advanced && <p className="mt-1 text-xs text-red-600">A rule here needs fixing before saving.</p>}
        {advanced && (
          <>
            <p className="mt-1 text-xs text-gray-500">Leave a field empty to use the site's rule (Settings).</p>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              {POLICY_FIELDS.map((f) => (
                <Field key={f.key} label={f.label} hint={f.hint} error={wrong[f.key]}>
                  <input
                    type="number"
                    step={f.step}
                    min={POLICY_LIMITS[f.key].min}
                    max={POLICY_LIMITS[f.key].max}
                    className={inputClass}
                    value={policy[f.key] ?? ''}
                    onChange={(e) => setPolicy((p) => ({ ...p, [f.key]: e.target.value }))}
                  />
                </Field>
              ))}
            </div>
          </>
        )}
      </div>
      {error && (
        <div className="mt-4">
          <ErrorNote message={error} />
        </div>
      )}
    </Modal>
  )
}

function Members({ group, onClose }: { group: Group; onClose: () => void }) {
  const { can } = useAuth()
  const { toast } = useFeedback()
  const current = useLoad(() => api.members(group.id), [group.id])
  const [q, setQ] = useState('')
  const [term, setTerm] = useState('')
  const [busy, setBusy] = useState<number | null>(null)
  useEffect(() => {
    const t = window.setTimeout(() => setTerm(q.trim()), 250)
    return () => window.clearTimeout(t)
  }, [q])
  // Searched on the server, so a roster of any size can be found.
  const found = useLoad(() => (term ? api.listPeople({ q: term, limit: 20 }) : Promise.resolve(null)), [term])
  const ids = new Set((current.data ?? []).map((p) => p.id))

  const change = async (p: Person, add: boolean) => {
    setBusy(p.id)
    try {
      if (add) await api.addMembers(group.id, [p.id])
      else await api.removeMember(group.id, p.id)
      current.reload()
    } catch (e) {
      toast('error', errorText(e))
    } finally {
      setBusy(null)
    }
  }

  return (
    <Modal title={`${group.name}: members`} onClose={onClose} wide footer={<Button onClick={onClose}>Done</Button>}>
      <div className="grid gap-5 md:grid-cols-2">
        <div>
          <div className="mb-2 text-sm font-medium text-gray-700">In the group ({current.data?.length ?? '…'})</div>
          {current.error ? (
            <ErrorNote message={current.error} onRetry={current.reload} />
          ) : !current.data ? (
            <Loading />
          ) : !current.data.length ? (
            <p className="text-sm text-gray-500">Nobody yet.</p>
          ) : (
            <ul className="max-h-80 divide-y overflow-y-auto rounded-lg border">
              {current.data.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2 px-3 py-2 text-sm">
                  <span>
                    {p.display_name}
                    {p.external_ref && <span className="ml-2 text-gray-500">{p.external_ref}</span>}
                    {!p.face_samples && <Badge tone="amber">no face</Badge>}
                  </span>
                  {can.manage && (
                    <Button variant="ghost" size="sm" busy={busy === p.id} onClick={() => change(p, false)} aria-label={`Remove ${p.display_name}`}>
                      <UserMinus className="h-3.5 w-3.5 text-red-600" />
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
        {can.manage && (
          <div>
            <div className="mb-2 text-sm font-medium text-gray-700">Add people</div>
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-gray-400" />
              <input className={`${inputClass} pl-9`} placeholder="Search by name or ID" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
            <ul className="mt-2 max-h-72 divide-y overflow-y-auto">
              {found.data?.people.map((p) => (
                <li key={p.id} className="flex items-center justify-between gap-2 py-2 text-sm">
                  <span>
                    {p.display_name}
                    {p.external_ref && <span className="ml-2 text-gray-500">{p.external_ref}</span>}
                  </span>
                  {ids.has(p.id) ? (
                    <Badge tone="green">In group</Badge>
                  ) : (
                    <Button variant="secondary" size="sm" busy={busy === p.id} onClick={() => change(p, true)}>
                      <UserPlus className="h-3.5 w-3.5" /> Add
                    </Button>
                  )}
                </li>
              ))}
              {term && found.data && !found.data.people.length && <li className="py-2 text-sm text-gray-500">Nobody matches.</li>}
              {!term && <li className="py-2 text-sm text-gray-500">Type a name to find people.</li>}
            </ul>
          </div>
        )}
      </div>
    </Modal>
  )
}
