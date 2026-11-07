// Student × class for a course and date range, with totals and a rate.
// From PR #791's report: called-off classes are left out, a class too
// little watched is shown but counts for nothing, and spreadsheet cells
// are escaped against formula injection.
import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import Layout from '../components/Layout'
import { config } from '../config/environment'
import { BarChart3, FileSpreadsheet, FileText } from 'lucide-react'
import { useToast } from '../components/ToastProvider'
import { STATUS_LABEL, type MarkStatus } from '../components/ClassReview'
// @ts-ignore - runtime import available after install
import * as XLSX from 'xlsx'

const API = config.API_BASE_URL

type Cls = { class_id: string; section: string | null; date: string; start: string | null; end: string | null; no_coverage: boolean; total_slots: number }
type Row = { student_code: string; full_name: string; email: string | null; section: string | null; marks: Record<string, { status: MarkStatus; source: string }>; present: number; late: number; absent: number; excused: number; unwatched: number; rate: number | null }
type Rep = { course_code: string; section: string | null; classes: Cls[]; rows: Row[] }

const SHORT: Record<string, string> = { PRESENT: 'P', LATE: 'L', ABSENT: 'A', EXCUSED: 'E', UNWATCHED: '–' }
const TONE: Record<string, string> = { PRESENT: 'text-green-700', LATE: 'text-amber-700', ABSENT: 'text-red-700', EXCUSED: 'text-blue-700', UNWATCHED: 'text-gray-400' }

/** A name like =HYPERLINK(...) must not become a formula in a spreadsheet. */
const safeCell = (v: unknown) => (typeof v === 'string' && /^[=+\-@\t\r]/.test(v) ? `'${v}` : v)

const monthAgo = () => { const d = new Date(); d.setMonth(d.getMonth() - 1); return d.toISOString().slice(0, 10) }

export default function Report() {
  const [courses, setCourses] = useState<Array<{ code: string; name?: string }>>([])
  const [course, setCourse] = useState('')
  const [section, setSection] = useState('')
  const [sections, setSections] = useState<string[]>([])
  const [from, setFrom] = useState(monthAgo())
  const [to, setTo] = useState(new Date().toISOString().slice(0, 10))
  const [rep, setRep] = useState<Rep | null>(null)
  const [loading, setLoading] = useState(false)
  const { showToast } = useToast()

  useEffect(() => {
    axios.get(`${API}/planner/courses`).then(r => {
      const list = Array.isArray(r.data) ? r.data : []
      setCourses(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined })))
    }).catch(() => setCourses([]))
  }, [])
  useEffect(() => {
    setSection(''); setSections([])
    if (!course) return
    axios.get(`${API}/courses/${encodeURIComponent(course)}/sections`).then(r => setSections(Array.isArray(r.data?.sections) ? r.data.sections.map(String) : [])).catch(() => setSections([]))
  }, [course])

  const load = async () => {
    if (!course) return
    setLoading(true)
    try {
      const r = await axios.get<Rep>(`${API}/attendance/report`, { params: { course, section: section || undefined, date_from: from || undefined, date_to: to || undefined } })
      setRep(r.data)
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Could not build the report'))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [course, section, from, to]) // eslint-disable-line react-hooks/exhaustive-deps

  const table = useMemo(() => {
    if (!rep) return null
    const head = ['Student code', 'Name', 'Section', ...rep.classes.map(c => `${c.date} ${c.start || ''}`.trim()), 'Present', 'Late', 'Absent', 'Excused', 'Not watched', 'Rate %']
    const lines = rep.rows.map(r => [
      r.student_code, safeCell(r.full_name), safeCell(r.section || ''),
      ...rep.classes.map(c => { const m = r.marks[c.class_id]; return m ? SHORT[m.status] + (m.source === 'manual' ? '*' : '') : '' }),
      r.present, r.late, r.absent, r.excused, r.unwatched, r.rate == null ? '' : Math.round(r.rate * 1000) / 10,
    ])
    return [head, ...lines]
  }, [rep])

  const name = () => `attendance-${course}${section ? '-' + section : ''}-${from}-${to}`
  const exportXlsx = () => {
    if (!table) return
    const ws = XLSX.utils.aoa_to_sheet(table)
    const wb = XLSX.utils.book_new()
    XLSX.utils.book_append_sheet(wb, ws, 'Report')
    XLSX.writeFile(wb, `${name()}.xlsx`)
  }
  const exportCsv = () => {
    window.open(`${API}/attendance/report?course=${encodeURIComponent(course)}${section ? `&section=${encodeURIComponent(section)}` : ''}&date_from=${from}&date_to=${to}&format=csv`, '_blank')
  }
  const exportCsvLocal = () => {
    if (!table) return
    const text = table.map(r => r.map(v => `"${String(v ?? '').replace(/"/g, '""')}"`).join(',')).join('\n')
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' })); a.download = `${name()}.csv`; a.click()
  }

  const totals = useMemo(() => rep ? rep.rows.reduce((t, r) => ({ present: t.present + r.present, late: t.late + r.late, absent: t.absent + r.absent }), { present: 0, late: 0, absent: 0 }) : null, [rep])

  return (
    <Layout title="Attendance Report">
      <div className="space-y-4">
        <div className="bg-white border rounded-lg p-4 flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900 mr-auto">
            <BarChart3 className="w-5 h-5 text-gray-500" />
            <span>Student × class</span>
            <span className="text-sm font-normal text-gray-500">— rate = (present + late) / (marked − excused); classes too little watched count for nothing</span>
          </div>
          <select className="rounded border px-2 py-1.5 text-sm max-w-[16rem]" value={course} onChange={e => setCourse(e.target.value)}>
            <option value="">Course</option>
            {courses.map(c => <option key={c.code} value={c.code}>{c.code}{c.name ? ` — ${c.name}` : ''}</option>)}
          </select>
          <select className="rounded border px-2 py-1.5 text-sm" value={section} onChange={e => setSection(e.target.value)} disabled={!course}>
            <option value="">All sections</option>
            {sections.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
          <input type="date" className="rounded border px-2 py-1.5 text-sm" value={from} onChange={e => setFrom(e.target.value)} />
          <span className="text-gray-400 text-sm">to</span>
          <input type="date" className="rounded border px-2 py-1.5 text-sm" value={to} onChange={e => setTo(e.target.value)} />
          <button className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200 text-sm disabled:opacity-50" onClick={exportXlsx} disabled={!table || !rep?.rows.length}><FileSpreadsheet className="w-4 h-4" /> Excel</button>
          <button className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200 text-sm disabled:opacity-50" onClick={table && rep?.rows.length ? exportCsvLocal : exportCsv} disabled={!table || !rep?.rows.length}><FileText className="w-4 h-4" /> CSV</button>
        </div>

        {rep && totals && (
          <div className="grid gap-3 sm:grid-cols-4 text-sm">
            <div className="bg-gray-50 border rounded-lg p-3"><div className="text-2xl font-bold text-gray-800">{rep.classes.length}</div><div className="text-gray-600">classes decided{rep.classes.some(c => c.no_coverage) ? `, ${rep.classes.filter(c => c.no_coverage).length} too little watched` : ''}</div></div>
            <div className="bg-green-50 border border-green-200 rounded-lg p-3"><div className="text-2xl font-bold text-green-700">{totals.present}</div><div className="text-gray-600">present marks</div></div>
            <div className="bg-amber-50 border border-amber-200 rounded-lg p-3"><div className="text-2xl font-bold text-amber-700">{totals.late}</div><div className="text-gray-600">late marks</div></div>
            <div className="bg-red-50 border border-red-200 rounded-lg p-3"><div className="text-2xl font-bold text-red-700">{totals.absent}</div><div className="text-gray-600">absent marks</div></div>
          </div>
        )}

        <div className="bg-white border rounded-lg" aria-busy={loading}>
          <div className="overflow-auto" style={{ maxHeight: '62vh' }}>
            {!course ? <div className="p-8 text-center text-sm text-gray-500">Pick a course.</div> : !rep ? <div className="p-8 text-center text-sm text-gray-500">{loading ? 'Building…' : 'No report yet.'}</div> : (
              <table className="min-w-full text-sm">
                <thead className="bg-gray-50 sticky top-0">
                  <tr>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider sticky left-0 bg-gray-50">Student</th>
                    {rep.classes.map(c => (
                      <th key={c.class_id} className={`px-2 py-1 text-center text-[10px] font-medium uppercase tracking-wider whitespace-nowrap ${c.no_coverage ? 'text-gray-400' : 'text-gray-500'}`} title={`${c.class_id}${c.no_coverage ? ' — too little watched' : ''}`}>
                        {c.date.slice(5)}<br /><span className="font-normal normal-case">{c.start}{c.section && !section ? ` ${c.section}` : ''}</span>
                      </th>
                    ))}
                    {['P', 'L', 'A', 'E', '–', 'Rate'].map(h => <th key={h} className="px-2 py-1 text-right text-[10px] font-medium text-gray-500 uppercase tracking-wider" title={{ P: 'Present', L: 'Late', A: 'Absent', E: 'Excused', '–': 'Not watched', Rate: '(present + late) / (marked − excused)' }[h]}>{h}</th>)}
                  </tr>
                </thead>
                <tbody className="divide-y">
                  {rep.rows.map(r => (
                    <tr key={r.student_code} className="hover:bg-gray-50">
                      <td className="px-3 py-1 whitespace-nowrap sticky left-0 bg-white">{r.full_name} <span className="text-xs text-gray-400">{r.student_code}</span></td>
                      {rep.classes.map(c => { const m = r.marks[c.class_id]; return <td key={c.class_id} className={`px-2 py-1 text-center font-mono ${m ? TONE[m.status] : 'text-gray-300'}`} title={m ? `${STATUS_LABEL[m.status]}${m.source === 'manual' ? ' (set by a person)' : ''}` : 'no mark'}>{m ? SHORT[m.status] + (m.source === 'manual' ? '*' : '') : '·'}</td> })}
                      <td className="px-2 py-1 text-right tabular-nums text-green-700">{r.present}</td>
                      <td className="px-2 py-1 text-right tabular-nums text-amber-700">{r.late}</td>
                      <td className="px-2 py-1 text-right tabular-nums text-red-700">{r.absent}</td>
                      <td className="px-2 py-1 text-right tabular-nums text-blue-700">{r.excused}</td>
                      <td className="px-2 py-1 text-right tabular-nums text-gray-400">{r.unwatched}</td>
                      <td className="px-2 py-1 text-right tabular-nums font-medium">{r.rate == null ? '—' : `${Math.round(r.rate * 100)}%`}</td>
                    </tr>
                  ))}
                  {rep.rows.length === 0 && <tr><td colSpan={rep.classes.length + 7} className="px-3 py-6 text-center text-gray-500">No decided classes for this course in the range.</td></tr>}
                </tbody>
              </table>
            )}
          </div>
          {rep && rep.rows.length > 0 && <div className="px-3 py-2 text-xs text-gray-500 border-t">P present · L late · A absent · E excused · – not watched · * set by a person</div>}
        </div>
      </div>
    </Layout>
  )
}
