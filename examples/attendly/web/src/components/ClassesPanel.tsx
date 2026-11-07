// The published classes: upcoming, running, decided, called off — each
// with its room, a Review button, and call-off / restore; plus a one-off
// class (an extra lecture, an exam) on a room.
import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import { Ban, CalendarPlus, ClipboardList, RotateCcw } from 'lucide-react'
import { config } from '../config/environment'
import { useToast } from '../components/ToastProvider'

const API = config.API_BASE_URL

export type ScheduledClass = {
  class_id: string; course: string | null; section: string | null; start_iso: string | null; end_iso: string | null
  is_finalized: boolean; called_off: boolean; stream_id: number; room: string | null
}
type Room = { id: number; name: string }

const when = (iso: string | null) => (iso ? new Date(iso) : null)
const fmtDay = (d: Date) => d.toLocaleDateString([], { weekday: 'short', day: '2-digit', month: 'short' })
const fmtTime = (d: Date | null) => (d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '')

export function classState(c: ScheduledClass, now = new Date()): { label: string; tone: string } {
  if (c.called_off) return { label: 'Called off', tone: 'bg-gray-100 text-gray-600' }
  if (c.is_finalized) return { label: 'Decided', tone: 'bg-indigo-100 text-indigo-800' }
  const s = when(c.start_iso), e = when(c.end_iso)
  if (s && e && now >= s && now < e) return { label: 'Running', tone: 'bg-green-100 text-green-800' }
  if (e && now >= e) return { label: 'Deciding', tone: 'bg-amber-100 text-amber-800' }
  return { label: 'Scheduled', tone: 'bg-gray-100 text-gray-700' }
}

export default function ClassesPanel({ canManage, onReview, refreshKey }: {
  canManage: boolean
  onReview: (classId: string) => void
  refreshKey?: number
}) {
  const [classes, setClasses] = useState<ScheduledClass[]>([])
  const [rooms, setRooms] = useState<Room[]>([])
  const [day, setDay] = useState(new Date().toISOString().slice(0, 10))
  const [open, setOpen] = useState(false)
  const [adding, setAdding] = useState(false)
  const [form, setForm] = useState({ stream_id: '', course: '', section: '', date: new Date().toISOString().slice(0, 10), start: '', end: '' })
  const [busy, setBusy] = useState(false)
  const { showToast } = useToast()

  const load = async () => {
    try {
      const [c, r] = await Promise.all([axios.get<ScheduledClass[]>(`${API}/attendance/scheduled`), axios.get(`${API}/classrooms/`)])
      setClasses(Array.isArray(c.data) ? c.data : [])
      setRooms(Array.isArray(r.data) ? r.data.map((x: any) => ({ id: Number(x.id), name: String(x.name) })) : [])
    } catch {}
  }
  useEffect(() => { load() }, [refreshKey])
  useEffect(() => {
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [])

  const onDay = useMemo(() => classes.filter(c => (c.start_iso || '').slice(0, 10) === day || (() => { const s = when(c.start_iso); return !!s && s.toISOString().slice(0, 10) === day })()), [classes, day])

  const setOff = async (c: ScheduledClass, off: boolean) => {
    if (busy) return
    setBusy(true)
    try {
      await axios.post(`${API}/attendance/${encodeURIComponent(c.class_id)}/${off ? 'call_off' : 'restore'}`)
      showToast('success', off ? 'Class called off' : 'Class restored')
      await load()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed'))
    } finally {
      setBusy(false)
    }
  }

  const addClass = async () => {
    if (busy) return
    if (!form.stream_id || !form.course.trim() || !form.section.trim() || !form.date || !form.start || !form.end) { showToast('error', 'Room, course, section, date and times are required'); return }
    setBusy(true)
    try {
      const r = await axios.post(`${API}/streams/${form.stream_id}/classes`, { course: form.course.trim(), section: form.section.trim(), date: form.date, start: form.start, end: form.end })
      showToast('success', `Class ${r.data.class_id} added`)
      setAdding(false)
      setDay(form.date)
      await load()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Could not add the class'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="bg-white border rounded-lg p-4 mb-2">
      <div className="flex flex-wrap items-center gap-3">
        <button className="flex items-center gap-2 text-base font-medium text-gray-900" onClick={() => setOpen(o => !o)} aria-expanded={open}>
          <ClipboardList className="w-5 h-5 text-gray-500" />
          <span>Classes</span>
          <span className="text-sm font-normal text-gray-500">— {onDay.length} on {fmtDay(new Date(day + 'T00:00:00'))}; open one to review the marks</span>
        </button>
        <div className="ml-auto flex items-center gap-2">
          <input type="date" className="rounded border px-2 py-1 text-sm" value={day} onChange={e => { if (e.target.value) { setDay(e.target.value); setOpen(true) } }} />
          {canManage && <button className="inline-flex items-center gap-1 px-2.5 py-1.5 text-sm rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => { setAdding(a => !a); setOpen(true) }}><CalendarPlus className="w-4 h-4" /> One-off class</button>}
          <button className="px-2 py-1 text-sm rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => setOpen(o => !o)}>{open ? 'Hide' : 'Show'}</button>
        </div>
      </div>

      {open && adding && (
        <div className="mt-3 grid gap-2 sm:grid-cols-6 items-end border rounded-lg p-3 bg-gray-50">
          <label className="text-xs text-gray-600">Room<select className="mt-1 w-full rounded border px-2 py-1.5 text-sm" value={form.stream_id} onChange={e => setForm({ ...form, stream_id: e.target.value })}><option value="">Pick a room</option>{rooms.map(r => <option key={r.id} value={String(r.id)}>{r.name}</option>)}</select></label>
          <label className="text-xs text-gray-600">Course<input className="mt-1 w-full rounded border px-2 py-1.5 text-sm" placeholder="MBA-1" value={form.course} onChange={e => setForm({ ...form, course: e.target.value })} /></label>
          <label className="text-xs text-gray-600">Section<input className="mt-1 w-full rounded border px-2 py-1.5 text-sm" placeholder="A" value={form.section} onChange={e => setForm({ ...form, section: e.target.value })} /></label>
          <label className="text-xs text-gray-600">Date<input type="date" className="mt-1 w-full rounded border px-2 py-1.5 text-sm" value={form.date} onChange={e => setForm({ ...form, date: e.target.value })} /></label>
          <label className="text-xs text-gray-600">Starts<input type="time" className="mt-1 w-full rounded border px-2 py-1.5 text-sm" value={form.start} onChange={e => setForm({ ...form, start: e.target.value })} /></label>
          <div className="flex gap-2">
            <label className="text-xs text-gray-600 flex-1">Ends<input type="time" className="mt-1 w-full rounded border px-2 py-1.5 text-sm" value={form.end} onChange={e => setForm({ ...form, end: e.target.value })} /></label>
            <button className="self-end px-3 py-1.5 text-sm rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50" disabled={busy} onClick={addClass}>Add</button>
          </div>
        </div>
      )}

      {open && (
        <div className="mt-3 overflow-auto" style={{ maxHeight: '32vh' }}>
          <table className="min-w-full text-sm">
            <thead className="bg-gray-50 sticky top-0"><tr>{['Class', 'Room', 'Time', 'State', ''].map((h, i) => <th key={i} className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">{h}</th>)}</tr></thead>
            <tbody className="divide-y">
              {onDay.map(c => {
                const st = classState(c)
                return (
                  <tr key={c.class_id} className="hover:bg-gray-50">
                    <td className="px-3 py-1 whitespace-nowrap"><button className="text-indigo-700 hover:underline font-medium" onClick={() => onReview(c.class_id)} title={c.class_id}>{c.course || c.class_id}{c.section ? ` / ${c.section}` : ''}</button></td>
                    <td className="px-3 py-1 text-gray-600">{c.room || `Room ${c.stream_id}`}</td>
                    <td className="px-3 py-1 text-gray-600 whitespace-nowrap">{fmtTime(when(c.start_iso))}–{fmtTime(when(c.end_iso))}</td>
                    <td className="px-3 py-1"><span className={`px-2 py-0.5 rounded text-xs ${st.tone}`}>{st.label}</span></td>
                    <td className="px-3 py-1 text-right whitespace-nowrap">
                      <button className="text-xs px-2 py-1 rounded bg-gray-100 hover:bg-gray-200 mr-1" onClick={() => onReview(c.class_id)}>Review</button>
                      {canManage && !c.called_off && <button className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-gray-100 hover:bg-gray-200" disabled={busy} onClick={() => setOff(c, true)}><Ban className="w-3 h-3" /> Call off</button>}
                      {canManage && c.called_off && <button className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded bg-gray-100 hover:bg-gray-200" disabled={busy} onClick={() => setOff(c, false)}><RotateCcw className="w-3 h-3" /> Restore</button>}
                    </td>
                  </tr>
                )
              })}
              {onDay.length === 0 && <tr><td colSpan={5} className="px-3 py-4 text-center text-gray-500">No classes on this day. Publish the schedule, or add a one-off class.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
