// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's Manage Schedule: the week as a grid, one block per period.
// A period names the group, the times (in the group's zone) and the
// cameras that see the room — attendly's "room" is just those cameras.

import { useMemo, useState } from 'react'
import { CalendarDays, Camera as CameraIcon, Plus } from 'lucide-react'
import { errorText } from '../api'
import { useAuth } from '../auth'
import { WEEKDAYS, api, cameraNames, groupName, type Slot, type SlotIn } from '../attendance'
import { useFeedback } from '../components/feedback'
import { Button, Card, Empty, ErrorNote, Field, Loading, Modal, PageTitle, inlineInputClass, inputClass } from '../components/ui'
import { useCameras, useGroups, useLoad } from '../hooks'

const COLOURS = [
  'bg-indigo-50 border-indigo-200 text-indigo-900',
  'bg-emerald-50 border-emerald-200 text-emerald-900',
  'bg-amber-50 border-amber-200 text-amber-900',
  'bg-sky-50 border-sky-200 text-sky-900',
  'bg-rose-50 border-rose-200 text-rose-900',
  'bg-violet-50 border-violet-200 text-violet-900',
]

export default function Schedule() {
  const { can } = useAuth()
  const groups = useGroups()
  const cameras = useCameras()
  const [groupId, setGroupId] = useState('')
  const slots = useLoad(() => api.listSlots(groupId ? Number(groupId) : undefined), [groupId])
  const [editing, setEditing] = useState<Slot | { weekday: number } | null>(null)

  const byDay = useMemo(() => {
    const days: Slot[][] = WEEKDAYS.map(() => [])
    for (const s of slots.data ?? []) days[s.weekday]?.push(s)
    days.forEach((d) => d.sort((a, b) => a.start_time.localeCompare(b.start_time)))
    return days
  }, [slots.data])

  const colour = (gid: number) => COLOURS[(groups.data?.findIndex((g) => g.id === gid) ?? 0) % COLOURS.length]

  return (
    <>
      <PageTitle
        title="Schedule"
        subtitle="The weekly timetable. OpenNVR takes attendance on each period's cameras while it runs."
        actions={
          <>
            <select className={`${inlineInputClass} min-w-[12rem]`} value={groupId} onChange={(e) => setGroupId(e.target.value)} aria-label="Group">
              <option value="">Every group</option>
              {(groups.data ?? []).map((g) => (
                <option key={g.id} value={g.id}>
                  {g.name}
                </option>
              ))}
            </select>
            {can.manage && (
              <Button onClick={() => setEditing({ weekday: 0 })} disabled={!groups.data?.length}>
                <Plus className="h-4 w-4" /> Add period
              </Button>
            )}
          </>
        }
      />
      {slots.error || groups.error ? (
        <ErrorNote message={slots.error ?? groups.error ?? ''} onRetry={slots.reload} />
      ) : !slots.data || !groups.data ? (
        <Loading />
      ) : !groups.data.length ? (
        <Card>
          <Empty icon={<CalendarDays className="h-10 w-10" />} title="Create a group first">
            Periods belong to a group.
          </Empty>
        </Card>
      ) : (
        <div className="overflow-x-auto">
          <div className="grid min-w-[56rem] grid-cols-7 gap-2">
            {WEEKDAYS.map((day, i) => (
              <div key={day} className="flex min-h-[16rem] flex-col rounded-xl border border-gray-200 bg-white">
                <div className="flex items-center justify-between border-b px-3 py-2">
                  <span className="text-sm font-semibold text-gray-800">{day}</span>
                  {can.manage && (
                    <button className="rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-indigo-600" onClick={() => setEditing({ weekday: i })} aria-label={`Add a period on ${day}`}>
                      <Plus className="h-4 w-4" />
                    </button>
                  )}
                </div>
                <div className="flex flex-1 flex-col gap-2 p-2">
                  {byDay[i].map((s) => (
                    <button
                      key={s.id}
                      onClick={() => can.manage && setEditing(s)}
                      className={`rounded-lg border p-2 text-left text-xs ${colour(s.group_id)} ${s.active ? '' : 'opacity-50'} ${can.manage ? 'hover:shadow' : 'cursor-default'}`}
                    >
                      <div className="font-semibold">
                        {s.start_time}–{s.end_time}
                      </div>
                      <div className="truncate">{groupName(groups.data, s.group_id)}</div>
                      <div className="mt-1 flex items-center gap-1 truncate opacity-80">
                        <CameraIcon className="h-3 w-3 shrink-0" />
                        <span className="truncate">{cameraNames(cameras.data, s.camera_ids)}</span>
                      </div>
                      {!s.active && <div className="mt-1 italic">Paused</div>}
                    </button>
                  ))}
                  {!byDay[i].length && <div className="m-auto text-xs text-gray-300">—</div>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {editing && (
        <SlotForm
          slot={'id' in editing ? editing : null}
          weekday={editing.weekday}
          defaultGroup={groupId ? Number(groupId) : groups.data?.[0]?.id}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null)
            slots.reload()
          }}
        />
      )}
    </>
  )
}

function SlotForm({
  slot,
  weekday,
  defaultGroup,
  onClose,
  onSaved,
}: {
  slot: Slot | null
  weekday: number
  defaultGroup?: number
  onClose: () => void
  onSaved: () => void
}) {
  const { toast, confirm } = useFeedback()
  const groups = useGroups()
  const cameras = useCameras()
  const [form, setForm] = useState<SlotIn>({
    group_id: slot?.group_id ?? defaultGroup ?? 0,
    weekday: slot?.weekday ?? weekday,
    start_time: slot?.start_time ?? '09:00',
    end_time: slot?.end_time ?? '09:50',
    camera_ids: slot?.camera_ids ?? [],
    valid_from: slot?.valid_from ?? null,
    valid_to: slot?.valid_to ?? null,
    active: slot?.active ?? true,
  })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const set = <K extends keyof SlotIn>(k: K, v: SlotIn[K]) => setForm((f) => ({ ...f, [k]: v }))
  const toggle = (id: number) =>
    set('camera_ids', form.camera_ids.includes(id) ? form.camera_ids.filter((c) => c !== id) : [...form.camera_ids, id])
  const invalid = !form.group_id || !form.camera_ids.length || form.start_time >= form.end_time

  const save = async () => {
    setBusy(true)
    setError(null)
    try {
      if (slot) await api.updateSlot(slot.id, form)
      else await api.createSlot(form)
      toast('success', 'Period saved')
      onSaved()
    } catch (e) {
      setError(errorText(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!slot) return
    const ok = await confirm({
      title: 'Delete this period?',
      body: 'Sessions not yet started are cancelled. Past sessions and their marks are kept.',
      confirm: 'Delete',
      danger: true,
    })
    if (!ok) return
    try {
      await api.deleteSlot(slot.id)
      toast('success', 'Period deleted')
      onSaved()
    } catch (e) {
      toast('error', errorText(e))
    }
  }

  const tz = groups.data?.find((g) => g.id === form.group_id)?.timezone

  return (
    <Modal
      title={slot ? 'Edit period' : 'Add period'}
      onClose={onClose}
      footer={
        <>
          {slot && (
            <Button variant="danger" className="mr-auto" onClick={remove}>
              Delete
            </Button>
          )}
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button onClick={save} busy={busy} disabled={invalid}>
            Save
          </Button>
        </>
      }
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Group">
          <select className={inputClass} value={form.group_id} onChange={(e) => set('group_id', Number(e.target.value))}>
            {(groups.data ?? []).map((g) => (
              <option key={g.id} value={g.id}>
                {g.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Day">
          <select className={inputClass} value={form.weekday} onChange={(e) => set('weekday', Number(e.target.value))}>
            {WEEKDAYS.map((d, i) => (
              <option key={d} value={i}>
                {d}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Starts" hint={tz ? `In ${tz}` : undefined}>
          <input type="time" className={inputClass} value={form.start_time} onChange={(e) => set('start_time', e.target.value)} />
        </Field>
        <Field label="Ends" error={form.start_time >= form.end_time ? 'Must be after the start' : undefined}>
          <input type="time" className={inputClass} value={form.end_time} onChange={(e) => set('end_time', e.target.value)} />
        </Field>
        <div className="sm:col-span-2">
          <Field label="Cameras that see the room" error={!form.camera_ids.length ? 'Pick at least one' : undefined}>
            <div className="grid max-h-48 gap-1 overflow-y-auto rounded-md border p-2 sm:grid-cols-2">
              {cameras.error && <span className="text-sm text-red-600">{cameras.error}</span>}
              {(cameras.data ?? []).map((c) => (
                <label key={c.id} className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-gray-50">
                  <input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={form.camera_ids.includes(c.id)} onChange={() => toggle(c.id)} />
                  <span className="truncate">
                    {c.name}
                    {c.location && <span className="text-gray-500"> · {c.location}</span>}
                  </span>
                </label>
              ))}
              {cameras.data && !cameras.data.length && <span className="text-sm text-gray-500">No cameras you can see.</span>}
            </div>
          </Field>
        </div>
        <Field label="From" hint="Optional: the term's first day">
          <input type="date" className={inputClass} value={form.valid_from ?? ''} onChange={(e) => set('valid_from', e.target.value || null)} />
        </Field>
        <Field label="Until" hint="Optional: the term's last day">
          <input type="date" className={inputClass} value={form.valid_to ?? ''} onChange={(e) => set('valid_to', e.target.value || null)} />
        </Field>
        <label className="flex items-center gap-2 text-sm text-gray-700 sm:col-span-2">
          <input type="checkbox" className="h-4 w-4 accent-indigo-600" checked={form.active} onChange={(e) => set('active', e.target.checked)} />
          Active — take attendance in this period
        </label>
      </div>
      {error && (
        <div className="mt-4">
          <ErrorNote message={error} />
        </div>
      )}
    </Modal>
  )
}
