import { useEffect, useMemo, useState } from 'react'
import Layout from '../components/Layout'
import { departmentsService, staffService, type Department } from '../services/admin'
import { CalendarCheck, FileSpreadsheet } from 'lucide-react'
import { useToast } from '../components/ToastProvider'

type Row = { staff_code: string; status: 'PRESENT' | 'ABSENT'; first_seen: string | null; last_seen: string | null; sightings: number; department_name?: string | null; staff: { full_name: string; designation?: string | null; email?: string | null } }

const today = () => new Date().toISOString().slice(0, 10)
const t = (iso: string | null) => (iso ? new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '-')

export default function StaffAttendance() {
  const [date, setDate] = useState(today())
  const [departmentId, setDepartmentId] = useState('')
  const [departments, setDepartments] = useState<Department[]>([])
  const [report, setReport] = useState<{ present: number; absent: number; rows: Row[] } | null>(null)
  const [loading, setLoading] = useState(false)
  const [query, setQuery] = useState('')
  const [onlyAbsent, setOnlyAbsent] = useState(false)
  const { showToast } = useToast()

  useEffect(() => { departmentsService.list().then(r => setDepartments(Array.isArray(r.data) ? r.data : [])).catch(() => {}) }, [])

  const load = async () => {
    setLoading(true)
    try {
      const r = await staffService.attendance(date, departmentId ? Number(departmentId) : undefined)
      setReport(r.data)
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to load'))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => {
    load()
    const id = setInterval(load, 30000)
    return () => clearInterval(id)
  }, [date, departmentId])

  const rows = useMemo(() => {
    const q = query.trim().toLowerCase()
    return (report?.rows || []).filter(r => (!onlyAbsent || r.status === 'ABSENT') && (!q || r.staff.full_name.toLowerCase().includes(q) || r.staff_code.toLowerCase().includes(q)))
  }, [report, query, onlyAbsent])

  const exportCsv = () => {
    const out = [['date', 'staff_code', 'name', 'department', 'designation', 'status', 'check_in', 'check_out', 'sightings'],
      ...(report?.rows || []).map(r => [date, r.staff_code, r.staff.full_name, r.department_name || '', r.staff.designation || '', r.status, r.first_seen || '', r.last_seen || '', String(r.sightings)])]
    const blob = new Blob([out.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n')], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `staff-attendance-${date}.csv`; a.click()
  }

  return (
    <Layout title="Staff Attendance">
      <div className="space-y-4">
        <div className="bg-white border rounded-lg p-4 flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900 mr-auto">
            <CalendarCheck className="w-5 h-5 text-gray-500" />
            <span>Daily report</span>
            <span className="text-sm font-normal text-gray-500">— first and last time the department camera saw each person</span>
          </div>
          <input type="date" className="rounded border px-2 py-1.5 text-sm" value={date} onChange={e => setDate(e.target.value)} />
          <select className="rounded border px-2 py-1.5 text-sm" value={departmentId} onChange={e => setDepartmentId(e.target.value)}>
            <option value="">All departments</option>
            {departments.map(d => <option key={d.id} value={String(d.id)}>{d.name}</option>)}
          </select>
          <input className="rounded border px-2 py-1.5 text-sm w-48" placeholder="Search" value={query} onChange={e => setQuery(e.target.value)} />
          <label className="text-sm inline-flex items-center gap-1"><input type="checkbox" checked={onlyAbsent} onChange={e => setOnlyAbsent(e.target.checked)} /> Absent only</label>
          <button className="inline-flex items-center gap-1 px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200 text-sm" onClick={exportCsv} disabled={!report?.rows?.length}><FileSpreadsheet className="w-4 h-4" /> CSV</button>
        </div>

        {report && (
          <div className="grid gap-3 sm:grid-cols-3 text-sm">
            <div className="bg-green-50 border border-green-200 rounded-lg p-3"><div className="text-2xl font-bold text-green-700">{report.present}</div><div className="text-gray-600">Present</div></div>
            <div className="bg-red-50 border border-red-200 rounded-lg p-3"><div className="text-2xl font-bold text-red-700">{report.absent}</div><div className="text-gray-600">Absent</div></div>
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-3"><div className="text-2xl font-bold text-gray-800">{report.present + report.absent}</div><div className="text-gray-600">Staff</div></div>
          </div>
        )}

        <div className="bg-white border rounded-lg" aria-busy={loading}>
          <div className="overflow-auto" style={{ maxHeight: '60vh' }}>
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 sticky top-0">
                <tr>{['Code', 'Name', 'Department', 'Designation', 'Status', 'Check-in', 'Check-out', 'Seen'].map(h => <th key={h} className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">{h}</th>)}</tr>
              </thead>
              <tbody className="divide-y">
                {rows.map(r => (
                  <tr key={r.staff_code}>
                    <td className="px-3 py-1 font-mono text-xs text-gray-500">{r.staff_code}</td>
                    <td className="px-3 py-1 whitespace-nowrap">{r.staff.full_name}</td>
                    <td className="px-3 py-1 text-gray-500">{r.department_name || '-'}</td>
                    <td className="px-3 py-1 text-gray-500">{r.staff.designation || '-'}</td>
                    <td className="px-3 py-1"><span className={`px-2 py-0.5 rounded text-xs ${r.status === 'PRESENT' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>{r.status}</span></td>
                    <td className="px-3 py-1 text-gray-700">{t(r.first_seen)}</td>
                    <td className="px-3 py-1 text-gray-700">{t(r.last_seen)}</td>
                    <td className="px-3 py-1 text-gray-500">{r.sightings || '-'}</td>
                  </tr>
                ))}
                {!loading && rows.length === 0 && <tr><td colSpan={8} className="px-3 py-6 text-center text-gray-500">{report?.rows?.length ? 'No rows match.' : 'No staff enrolled yet.'}</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </Layout>
  )
}
