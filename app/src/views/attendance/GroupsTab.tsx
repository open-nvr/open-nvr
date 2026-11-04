/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// Groups: the people whose attendance is taken together — a class
// section, a shift — and the timezone their timetable is written in.

import { useMemo, useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus, Users } from 'lucide-react'
import { Modal } from '../../components/Modal'
import { Badge, Button, EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { Checkbox, Field, Input, Select } from '../../components/ui/form'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import { attendanceService, type Group } from '../../services/attendanceService'
import { useAttendancePermissions, useGroups } from './shared'

function browserZone(): string {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' } catch { return 'UTC' }
}

function zones(): string[] {
  const fn = (Intl as any).supportedValuesOf
  try { return typeof fn === 'function' ? (fn('timeZone') as string[]) : [] } catch { return [] }
}

export function GroupsTab() {
  const groups = useGroups()
  const { canManage } = useAttendancePermissions()
  const [editing, setEditing] = useState<Group | 'new' | null>(null)
  const [members, setMembers] = useState<Group | null>(null)

  return (
    <div className="space-y-3">
      {canManage && (
        <div className="flex justify-end">
          <Button variant="primary" onClick={() => setEditing('new')}><Plus size={14} /> New group</Button>
        </div>
      )}
      {groups.isLoading && <Skeleton className="h-32" />}
      {groups.isError && <ErrorCard message={extractApiError(groups.error, 'Could not load groups')} />}
      {groups.isSuccess && groups.data.length === 0 && (
        <EmptyState title="No groups yet"
                    description="A group is the people whose attendance is taken together: a class section, a shift." />
      )}
      {(groups.data ?? []).length > 0 && (
        <table className="w-full text-sm [&_th]:px-2 [&_td]:px-2">
          <thead className="text-left text-[var(--text-dim)]">
            <tr><th className="py-1">Name</th><th>Code</th><th>Members</th><th>Timezone</th><th /><th /></tr>
          </thead>
          <tbody>
            {(groups.data ?? []).map((g) => (
              <tr key={g.id} className="border-t border-[var(--border)]">
                <td className="py-1">{g.name} {g.kind_label && <span className="text-[var(--text-dim)]">· {g.kind_label}</span>}</td>
                <td>{g.code ?? ''}</td>
                <td>{g.members}</td>
                <td>{g.timezone}</td>
                <td>{!g.active && <Badge variant="neutral">inactive</Badge>}
                  {Object.keys(g.policy ?? {}).length > 0 && <Badge variant="info" className="ml-1">own rules</Badge>}</td>
                <td className="flex justify-end gap-1 py-1">
                  {canManage && <Button size="sm" variant="outline" onClick={() => setMembers(g)}><Users size={14} /> Members</Button>}
                  {canManage && <Button size="sm" variant="ghost" aria-label={`Edit ${g.name}`} onClick={() => setEditing(g)}><Pencil size={14} /></Button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editing && <GroupForm group={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />}
      {members && <MembersForm group={members} onClose={() => setMembers(null)} />}
    </div>
  )
}

function GroupForm({ group, onClose }: { group: Group | null; onClose: () => void }) {
  const qc = useQueryClient()
  const { showError } = useSnackbar()
  const [name, setName] = useState(group?.name ?? '')
  const [code, setCode] = useState(group?.code ?? '')
  const [label, setLabel] = useState(group?.kind_label ?? '')
  const [tz, setTz] = useState(group?.timezone ?? browserZone())
  const [active, setActive] = useState(group?.active ?? true)
  const allZones = useMemo(zones, [])
  const done = () => { qc.invalidateQueries({ queryKey: ['attendance'] }); onClose() }
  const save = useMutation({
    mutationFn: () => {
      const body = { name, code: code || null, kind_label: label || null, timezone: tz, active }
      return group ? attendanceService.updateGroup(group.id, body) : attendanceService.createGroup(body)
    },
    onSuccess: done,
    onError: (e) => showError(extractApiError(e, 'Could not save the group')),
  })
  const del = useMutation({
    mutationFn: () => attendanceService.deleteGroup(group!.id),
    onSuccess: done,
    onError: (e) => showError(extractApiError(e, 'Could not delete the group')),
  })
  return (
    <Modal open title={group ? `Edit ${group.name}` : 'New group'} onClose={onClose}
           footer={<>
             {group && <Button variant="danger" className="mr-auto" onClick={() => del.mutate()}>Delete</Button>}
             <Button variant="primary" disabled={!name.trim() || !tz || save.isPending} onClick={() => save.mutate()}>Save</Button>
           </>}>
      <div className="space-y-3">
        <Field label="Name" required><Input autoFocus value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Code" hint="Short, unique: for files and partner systems"><Input value={code} onChange={(e) => setCode(e.target.value)} /></Field>
        <Field label="Called" hint='What a group is here, e.g. "Section" or "Shift"'><Input value={label} onChange={(e) => setLabel(e.target.value)} /></Field>
        <Field label="Timezone" hint="The timetable's times are in this zone">
          {allZones.length
            ? <Select value={tz} onChange={(e) => setTz(e.target.value)}>
                {allZones.map((z) => <option key={z} value={z}>{z}</option>)}
              </Select>
            : <Input value={tz} onChange={(e) => setTz(e.target.value)} />}
        </Field>
        <Checkbox checked={active} onChange={(e) => setActive(e.target.checked)} label="Active" />
        {group && <p className="text-xs text-[var(--text-dim)]">A group with attendance history cannot be deleted; make it inactive instead.</p>}
      </div>
    </Modal>
  )
}

function MembersForm({ group, onClose }: { group: Group; onClose: () => void }) {
  const qc = useQueryClient()
  const { showError, showSuccess } = useSnackbar()
  const [q, setQ] = useState('')
  // The server searches: filtering the first page in the browser made
  // everyone past it impossible to add on a large site.
  const everyone = useQuery({
    queryKey: ['attendance', 'people', 'pick', q],
    queryFn: () => attendanceService.listPeople({ q: q.trim() || undefined, limit: 200 }),
    placeholderData: keepPreviousData,
  })
  const current = useQuery({
    queryKey: ['attendance', 'members', group.id],
    queryFn: () => attendanceService.members(group.id),
  })
  const [picked, setPicked] = useState<Set<number> | null>(null)
  const chosen = picked ?? new Set((current.data ?? []).map((p) => p.id))
  const save = useMutation({
    mutationFn: () => attendanceService.setMembers(group.id, [...chosen]),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['attendance'] }); showSuccess('Members saved'); onClose() },
    onError: (e) => showError(extractApiError(e, 'Could not save the members')),
  })
  // Members first, so the ones already in the group are never off-page.
  const memberRows = (current.data ?? []).filter((p) =>
    !q.trim() || `${p.display_name} ${p.external_ref ?? ''}`.toLowerCase().includes(q.trim().toLowerCase()))
  const memberIds = new Set(memberRows.map((p) => p.id))
  const shown = [...memberRows, ...(everyone.data?.people ?? []).filter((p) => !memberIds.has(p.id))]
  // Saving replaces the whole member list: never from a list that failed to load.
  const unsafe = current.isLoading || current.isError
  const toggle = (id: number) => {
    const next = new Set(chosen)
    if (next.has(id)) next.delete(id); else next.add(id)
    setPicked(next)
  }
  return (
    <Modal open title={`Members of ${group.name}`} onClose={onClose} widthClassName="max-w-xl"
           footer={<Button variant="primary" disabled={save.isPending || unsafe} onClick={() => save.mutate()}>
             Save {chosen.size} member{chosen.size === 1 ? '' : 's'}</Button>}>
      <div className="space-y-2">
        <Input placeholder="Search by name or reference" value={q} onChange={(e) => setQ(e.target.value)} />
        {(everyone.isLoading || current.isLoading) && <Skeleton className="h-32" />}
        {current.isError && (
          <ErrorCard message={extractApiError(current.error, 'Could not load the current members, so nothing can be saved')} />
        )}
        {(everyone.data?.total ?? 0) > (everyone.data?.people.length ?? 0) && (
          <p className="text-xs text-[var(--text-dim)]">
            {everyone.data?.total} people match; search to narrow it down.
          </p>
        )}
        <div className="max-h-80 overflow-y-auto">
          {shown.map((p) => (
            <div key={p.id} className="py-0.5">
              <Checkbox checked={chosen.has(p.id)} onChange={() => toggle(p.id)}
                        label={`${p.display_name}${p.external_ref ? ` (${p.external_ref})` : ''}`} />
            </div>
          ))}
        </div>
      </div>
    </Modal>
  )
}
