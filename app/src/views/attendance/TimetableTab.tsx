/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

// The weekly timetable: which group meets when, on which cameras. Each
// period becomes a session on its day; a session not yet started follows
// edits here, one already running keeps what it started with.

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Pencil, Plus } from 'lucide-react'
import { Modal } from '../../components/Modal'
import { Badge, Button, EmptyState, ErrorCard, Skeleton } from '../../components/ui'
import { Checkbox, Field, Input, Select } from '../../components/ui/form'
import { useSnackbar } from '../../components/Snackbar'
import { extractApiError } from '../../lib/apiError'
import { attendanceService, WEEKDAYS, type Slot, type SlotIn } from '../../services/attendanceService'
import { cameraNames, useAttendancePermissions, useCameras, useGroups } from './shared'

export function TimetableTab() {
  const groups = useGroups()
  const cameras = useCameras()
  const { canManage } = useAttendancePermissions()
  const [groupId, setGroupId] = useState<number | ''>('')
  const [editing, setEditing] = useState<Slot | 'new' | null>(null)
  const gid = groupId === '' ? groups.data?.[0]?.id : groupId
  const slots = useQuery({
    queryKey: ['attendance', 'slots', gid],
    queryFn: () => attendanceService.listSlots(gid),
    enabled: gid !== undefined,
  })

  if (groups.isSuccess && groups.data.length === 0) {
    return <EmptyState title="No groups yet" description="Create a group under Groups first." />
  }
  const group = groups.data?.find((g) => g.id === gid)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Group">
          <Select value={gid ?? ''} onChange={(e) => setGroupId(Number(e.target.value))}>
            {(groups.data ?? []).map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
          </Select>
        </Field>
        {group && <span className="pb-2 text-sm text-[var(--text-dim)]">Times are in {group.timezone}</span>}
        {canManage && gid !== undefined && (
          <Button variant="primary" className="ml-auto" onClick={() => setEditing('new')}>
            <Plus size={14} /> Add period
          </Button>
        )}
      </div>
      {slots.isLoading && <Skeleton className="h-32" />}
      {slots.isError && <ErrorCard message={extractApiError(slots.error, 'Could not load the timetable')} />}
      {slots.isSuccess && slots.data.length === 0 && (
        <EmptyState title="No periods yet" description="Add the weekly periods this group meets in, and the cameras that see the room." />
      )}
      {(slots.data ?? []).length > 0 && (
        <table className="w-full text-sm [&_th]:px-2 [&_td]:px-2">
          <thead className="text-left text-[var(--text-dim)]">
            <tr><th className="py-1">Day</th><th>Time</th><th>Cameras</th><th>Term</th><th /><th /></tr>
          </thead>
          <tbody>
            {(slots.data ?? []).map((s) => (
              <tr key={s.id} className="border-t border-[var(--border)]">
                <td className="py-1">{WEEKDAYS[s.weekday]}</td>
                <td>{s.start_time}–{s.end_time}</td>
                <td>{cameraNames(cameras.data, s.camera_ids)}</td>
                <td>{s.valid_from || s.valid_to ? `${s.valid_from ?? '…'} to ${s.valid_to ?? '…'}` : 'Every week'}</td>
                <td>{!s.active && <Badge variant="neutral">off</Badge>}</td>
                <td className="text-right">
                  {canManage && <Button size="sm" variant="ghost" aria-label="Edit period" onClick={() => setEditing(s)}><Pencil size={14} /></Button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {editing && gid !== undefined && (
        <SlotForm groupId={gid} slot={editing === 'new' ? null : editing} onClose={() => setEditing(null)} />
      )}
    </div>
  )
}

function SlotForm({ groupId, slot, onClose }: { groupId: number; slot: Slot | null; onClose: () => void }) {
  const qc = useQueryClient()
  const cameras = useCameras()
  const { showError } = useSnackbar()
  const [form, setForm] = useState<SlotIn>({
    group_id: groupId, weekday: slot?.weekday ?? 0, start_time: slot?.start_time ?? '09:00',
    end_time: slot?.end_time ?? '09:50', camera_ids: slot?.camera_ids ?? [],
    valid_from: slot?.valid_from ?? null, valid_to: slot?.valid_to ?? null, active: slot?.active ?? true,
  })
  const set = (patch: Partial<SlotIn>) => setForm((f) => ({ ...f, ...patch }))
  const done = () => { qc.invalidateQueries({ queryKey: ['attendance'] }); onClose() }
  const save = useMutation({
    mutationFn: () => slot ? attendanceService.updateSlot(slot.id, form) : attendanceService.createSlot(form),
    onSuccess: done,
    onError: (e) => showError(extractApiError(e, 'Could not save the period')),
  })
  const del = useMutation({
    mutationFn: () => attendanceService.deleteSlot(slot!.id),
    onSuccess: done,
    onError: (e) => showError(extractApiError(e, 'Could not delete the period')),
  })
  const toggleCam = (id: number) => set({
    camera_ids: form.camera_ids.includes(id) ? form.camera_ids.filter((c) => c !== id) : [...form.camera_ids, id],
  })
  const invalid = !form.camera_ids.length || form.start_time >= form.end_time
  return (
    <Modal open title={slot ? 'Edit period' : 'Add period'} onClose={onClose}
           footer={<>
             {slot && <Button variant="danger" className="mr-auto" onClick={() => del.mutate()}>Delete</Button>}
             <Button variant="primary" disabled={invalid || save.isPending} onClick={() => save.mutate()}>Save</Button>
           </>}>
      <div className="space-y-3">
        <Field label="Day">
          <Select value={form.weekday} onChange={(e) => set({ weekday: Number(e.target.value) })}>
            {WEEKDAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
          </Select>
        </Field>
        <div className="flex gap-3">
          <Field label="Starts"><Input type="time" value={form.start_time} onChange={(e) => set({ start_time: e.target.value })} /></Field>
          <Field label="Ends" error={form.start_time >= form.end_time ? 'Ends before it starts (a night shift is two periods)' : undefined}>
            <Input type="time" value={form.end_time} onChange={(e) => set({ end_time: e.target.value })} />
          </Field>
        </div>
        <Field label="Cameras that see the room" error={!form.camera_ids.length ? 'Pick at least one' : undefined}>
          <div className="max-h-48 overflow-y-auto">
            {(cameras.data ?? []).map((c) => (
              <div key={c.id}>
                <Checkbox checked={form.camera_ids.includes(c.id)} onChange={() => toggleCam(c.id)}
                          label={c.location ? `${c.name} · ${c.location}` : c.name} />
              </div>
            ))}
          </div>
        </Field>
        <div className="flex gap-3">
          <Field label="Term starts" hint="Optional"><Input type="date" value={form.valid_from ?? ''} onChange={(e) => set({ valid_from: e.target.value || null })} /></Field>
          <Field label="Term ends" hint="Optional"><Input type="date" value={form.valid_to ?? ''} onChange={(e) => set({ valid_to: e.target.value || null })} /></Field>
        </div>
        <Checkbox checked={form.active} onChange={(e) => set({ active: e.target.checked })} label="On" />
        <p className="text-xs text-[var(--text-dim)]">
          The attendance app must be allowed these cameras too (App Catalog → Attendance → Cameras).
        </p>
      </div>
    </Modal>
  )
}
