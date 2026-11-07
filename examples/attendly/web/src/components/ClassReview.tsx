// One class, reviewed: each student's mark and who set it, how often and
// when first they were seen, how much of the class was watched, the
// evidence frames, and the controls — correct a mark, recompute from the
// cameras, call the class off or restore it.
import { useEffect, useState } from 'react'
import axios from 'axios'
import { Ban, RefreshCw, RotateCcw, X as XIcon } from 'lucide-react'
import { config } from '../config/environment'
import { useToast } from '../components/ToastProvider'

const API = config.API_BASE_URL

export type MarkStatus = 'PRESENT' | 'LATE' | 'ABSENT' | 'EXCUSED' | 'UNWATCHED'
export const STATUS_LABEL: Record<MarkStatus, string> = {
  PRESENT: 'Present', LATE: 'Late', ABSENT: 'Absent', EXCUSED: 'Excused', UNWATCHED: 'Not watched',
}
export const STATUS_TONE: Record<MarkStatus, string> = {
  PRESENT: 'bg-green-100 text-green-800', LATE: 'bg-amber-100 text-amber-800', ABSENT: 'bg-red-100 text-red-800',
  EXCUSED: 'bg-blue-100 text-blue-800', UNWATCHED: 'bg-gray-100 text-gray-600',
}
const CORRECTIONS: MarkStatus[] = ['PRESENT', 'LATE', 'ABSENT', 'EXCUSED']

type Row = {
  student: { student_code: string; full_name: string; course_code?: string | null; section?: string | null } | null
  slots_present: number; total_slots: number; ratio: number; status: MarkStatus; source: 'auto' | 'manual'
  note: string | null; first_seen: string | null
}
type Review = {
  class_id: string
  meta: { finalized_at: string; total_slots: number; unwatched_slots: number; no_coverage: boolean; called_off: boolean } | null
  counts: Record<string, number>
  rows: Row[]
  evidence: Array<{ index: number; slot_time: string }>
}

export function MarkBadge({ status, manual }: { status: MarkStatus; manual?: boolean }) {
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${STATUS_TONE[status] || STATUS_TONE.UNWATCHED}`} title={manual ? 'Set by a person' : 'From the cameras'}>
      {STATUS_LABEL[status] || status}{manual ? <span aria-label="manual">✎</span> : null}
    </span>
  )
}

const clock = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—')

export default function ClassReview({ classId, canManage, onClose, onChanged }: {
  classId: string
  canManage: boolean
  onClose: () => void
  onChanged?: () => void
}) {
  const [data, setData] = useState<Review | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [frames, setFrames] = useState<Record<number, string>>({})
  const [notes, setNotes] = useState<Record<string, string>>({})
  const { showToast } = useToast()

  const load = async () => {
    try {
      const r = await axios.get<Review>(`${API}/attendance/${encodeURIComponent(classId)}/review`)
      setData(r.data)
      setError(null)
      setNotes(Object.fromEntries(r.data.rows.filter(x => x.student).map(x => [x.student!.student_code, x.note || ''])))
    } catch (e: any) {
      setError(String(e?.response?.data?.detail || e?.message || 'Could not load the class'))
    }
  }
  useEffect(() => { load() }, [classId])

  // Evidence frames carry the user's token, so an <img src> cannot fetch
  // them: each is fetched once and shown from a blob URL.
  useEffect(() => {
    let live = true
    const urls: string[] = []
    ;(async () => {
      for (const e of data?.evidence || []) {
        if (frames[e.index]) continue
        try {
          const r = await axios.get(`${API}/attendance/${encodeURIComponent(classId)}/evidence/${e.index}`, { responseType: 'blob' })
          if (!live) return
          const u = URL.createObjectURL(r.data)
          urls.push(u)
          setFrames(f => ({ ...f, [e.index]: u }))
        } catch {}
      }
    })()
    return () => { live = false }
  }, [data?.evidence?.length, classId]) // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (what: 'recompute' | 'call_off' | 'restore') => {
    if (busy) return
    setBusy(true)
    try {
      await axios.post(`${API}/attendance/${encodeURIComponent(classId)}/${what}`)
      showToast('success', what === 'recompute' ? 'Marks recomputed from the cameras' : what === 'call_off' ? 'Class called off' : 'Class restored')
      await load()
      onChanged?.()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed'))
    } finally {
      setBusy(false)
    }
  }

  const correct = async (code: string, status: MarkStatus | 'AUTO') => {
    try {
      await axios.put(`${API}/attendance/${encodeURIComponent(classId)}/marks/${encodeURIComponent(code)}`, { status, note: notes[code] || null })
      await load()
      onChanged?.()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Could not change the mark'))
    }
  }

  const meta = data?.meta
  const decided = !!meta && !meta.called_off
  const watched = meta ? `${meta.total_slots} of ${meta.total_slots + meta.unwatched_slots} slots watched` : null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-5xl max-h-[90vh] overflow-auto rounded-lg bg-white border shadow-lg">
        <div className="sticky top-0 bg-white border-b px-5 py-3 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-lg font-semibold truncate" title={classId}>{classId}</div>
            <div className="text-xs text-gray-500 flex flex-wrap gap-x-3 gap-y-1 mt-0.5">
              {meta?.called_off ? <span className="text-gray-700 font-medium">Called off</span> : meta ? <span>Decided {new Date(meta.finalized_at).toLocaleString()}</span> : <span>Not decided yet</span>}
              {watched && <span>{watched}</span>}
              {meta?.no_coverage && <span className="text-red-700">Too little was watched (the camera or recognition was down): the marks are left out of every rate.</span>}
              {data && Object.entries(data.counts).map(([k, n]) => <span key={k}>{n} {STATUS_LABEL[k as MarkStatus]?.toLowerCase() || k}</span>)}
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {canManage && decided && <button className="inline-flex items-center gap-1 px-2.5 py-1.5 text-sm rounded-md bg-gray-100 hover:bg-gray-200 disabled:opacity-50" disabled={busy} onClick={() => act('recompute')}><RefreshCw className="w-4 h-4" /> Recompute</button>}
            {canManage && !meta?.called_off && <button className="inline-flex items-center gap-1 px-2.5 py-1.5 text-sm rounded-md bg-gray-100 hover:bg-gray-200 disabled:opacity-50" disabled={busy} onClick={() => act('call_off')}><Ban className="w-4 h-4" /> Call off</button>}
            {canManage && meta?.called_off && <button className="inline-flex items-center gap-1 px-2.5 py-1.5 text-sm rounded-md bg-gray-100 hover:bg-gray-200 disabled:opacity-50" disabled={busy} onClick={() => act('restore')}><RotateCcw className="w-4 h-4" /> Restore</button>}
            <button className="p-1 text-gray-500 hover:text-gray-700" onClick={onClose} aria-label="Close"><XIcon className="w-5 h-5" /></button>
          </div>
        </div>

        {error && <div className="p-5 text-sm text-red-600">{error}</div>}
        {!data && !error && <div className="p-8 text-center text-sm text-gray-500">Loading…</div>}
        {data && (
          <div className="p-5 space-y-4">
            <table className="min-w-full text-sm">
              <thead className="text-left text-[10px] uppercase tracking-wider text-gray-500">
                <tr><th className="py-1 pr-3">Student</th><th className="pr-3">Mark</th><th className="pr-3">Seen in</th><th className="pr-3">First seen</th>{canManage && <th>Correct</th>}</tr>
              </thead>
              <tbody className="divide-y">
                {data.rows.map(r => {
                  const code = r.student?.student_code || ''
                  return (
                    <tr key={code || Math.random()}>
                      <td className="py-1.5 pr-3 whitespace-nowrap">{r.student?.full_name || <span className="text-gray-400">deleted</span>} <span className="text-xs text-gray-400">{code}</span></td>
                      <td className="pr-3"><MarkBadge status={r.status} manual={r.source === 'manual'} /></td>
                      <td className="pr-3 text-gray-600">{r.total_slots ? `${r.slots_present} / ${r.total_slots}` : '—'}</td>
                      <td className="pr-3 text-gray-600">{clock(r.first_seen)}</td>
                      {canManage && (
                        <td className="py-1">
                          <div className="flex items-center gap-1">
                            <select className="rounded border px-2 py-1 text-xs" value={r.source === 'manual' ? r.status : ''} onChange={e => correct(code, (e.target.value || 'AUTO') as MarkStatus | 'AUTO')} aria-label={`Mark for ${r.student?.full_name || code}`}>
                              <option value="">From the cameras</option>
                              {CORRECTIONS.map(st => <option key={st} value={st}>{STATUS_LABEL[st]}</option>)}
                            </select>
                            <input className="w-36 rounded border px-2 py-1 text-xs" placeholder="Note" maxLength={500} value={notes[code] ?? ''} onChange={e => setNotes(n => ({ ...n, [code]: e.target.value }))} onBlur={() => { if (r.source === 'manual' && (notes[code] || '') !== (r.note || '')) correct(code, r.status) }} />
                          </div>
                        </td>
                      )}
                    </tr>
                  )
                })}
                {data.rows.length === 0 && <tr><td colSpan={5} className="py-6 text-center text-gray-500">No students in this class's course and section.</td></tr>}
              </tbody>
            </table>

            {data.evidence.length > 0 && (
              <div>
                <div className="text-sm font-medium text-gray-800 mb-2">Evidence — one frame per slot in which somebody was recognised</div>
                <div className="flex flex-wrap gap-3">
                  {data.evidence.map(e => (
                    <figure key={e.index} className="w-56">
                      {frames[e.index] ? <img src={frames[e.index]} alt="" className="w-56 h-32 object-cover rounded border" /> : <div className="w-56 h-32 rounded border bg-gray-100 flex items-center justify-center text-xs text-gray-400">no longer kept</div>}
                      <figcaption className="text-xs text-gray-500 mt-1">slot ending {clock(e.slot_time)}</figcaption>
                    </figure>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
