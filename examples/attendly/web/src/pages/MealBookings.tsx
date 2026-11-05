import { useEffect, useMemo, useRef, useState } from 'react'
import axios from 'axios'
import Layout from '../components/Layout'
import { mealsService, type MealBooking } from '../services/admin'
import { config } from '../config/environment'
import { UtensilsCrossed, Plus, Trash2, Upload } from 'lucide-react'
import { useToast } from '../components/ToastProvider'
import { useConfirm } from '../components/ConfirmProvider'

const MEALS = ['breakfast', 'lunch', 'dinner'] as const
type Student = { student_code: string; full_name: string; email?: string | null; course_code?: string | null; section?: string | null }

const today = () => new Date().toISOString().slice(0, 10)
const tomorrow = () => { const d = new Date(); d.setDate(d.getDate() + 1); return d.toISOString().slice(0, 10) }

export default function MealBookings() {
  const [date, setDate] = useState(today())
  const [meal, setMeal] = useState<string>('')
  const [bookings, setBookings] = useState<MealBooking[]>([])
  const [summary, setSummary] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  const [students, setStudents] = useState<Student[]>([])
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<Student | null>(null)
  const [bookDate, setBookDate] = useState(tomorrow())
  const [bookMeals, setBookMeals] = useState<string[]>(['lunch'])
  const [saving, setSaving] = useState(false)
  const [importResult, setImportResult] = useState<any>(null)
  const fileRef = useRef<HTMLInputElement | null>(null)
  const { showToast } = useToast()
  const { confirm } = useConfirm()

  const load = async () => {
    setLoading(true)
    try {
      const [b, s] = await Promise.all([mealsService.bookings(date, meal || undefined), mealsService.summary(date)])
      setBookings(b.data.bookings || [])
      setSummary(s.data.meals)
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to load bookings'))
    } finally {
      setLoading(false)
    }
  }
  useEffect(() => { load() }, [date, meal])
  useEffect(() => {
    axios.get(`${config.API_BASE_URL}/students/`).then(r => setStudents(Array.isArray(r.data) ? r.data : [])).catch(() => setStudents([]))
  }, [])

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return []
    return students.filter(s => s.full_name.toLowerCase().includes(q) || s.student_code.toLowerCase().includes(q) || (s.email || '').toLowerCase().includes(q)).slice(0, 8)
  }, [students, query])

  const book = async () => {
    if (!picked || bookMeals.length === 0 || saving) return
    try {
      setSaving(true)
      const r = await mealsService.book({ student_code: picked.student_code, date: bookDate, meals: bookMeals })
      showToast('success', r.data.created ? `Booked ${r.data.created} meal(s) for ${picked.full_name}` : 'Already booked')
      if (bookDate === date) await load()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to book'))
    } finally {
      setSaving(false)
    }
  }

  const remove = async (b: MealBooking) => {
    const ok = await confirm({ description: `Remove ${b.meal} booking for ${b.student?.full_name || b.student_code}?`, confirmText: 'Remove' })
    if (!ok) return
    try {
      await mealsService.remove(b.id)
      await load()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to remove'))
    }
  }

  const importCsv = async (file: File) => {
    try {
      const r = await mealsService.importCsv(file, bookDate)
      setImportResult(r.data)
      showToast('success', `Imported ${r.data.created} booking(s)`)
      if (bookDate === date) await load()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Import failed'))
    } finally {
      if (fileRef.current) fileRef.current.value = ''
    }
  }

  const exportCsv = () => {
    const rows = [['student_code', 'name', 'course', 'section', 'meal', 'served_at'],
      ...bookings.map(b => [b.student_code, b.student?.full_name || '', b.student?.course_code || '', b.student?.section || '', b.meal, b.consumed_at || ''])]
    const blob = new Blob([rows.map(r => r.map(v => `"${String(v).replace(/"/g, '""')}"`).join(',')).join('\n')], { type: 'text/csv' })
    const a = document.createElement('a'); a.href = URL.createObjectURL(blob); a.download = `meal-bookings-${date}.csv`; a.click()
  }

  return (
    <Layout title="Meal Bookings">
      <div className="space-y-4">
        <div className="bg-white border rounded-lg p-4 flex flex-wrap items-center gap-3">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900 mr-auto">
            <UtensilsCrossed className="w-5 h-5 text-gray-500" />
            <span>Bookings</span>
            <span className="text-sm font-normal text-gray-500">— the mess gate admits a booked student once per meal</span>
          </div>
          <input type="date" className="rounded border px-2 py-1.5 text-sm" value={date} onChange={e => setDate(e.target.value)} />
          <select className="rounded border px-2 py-1.5 text-sm" value={meal} onChange={e => setMeal(e.target.value)}>
            <option value="">All meals</option>
            {MEALS.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
          <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200 text-sm" onClick={exportCsv} disabled={bookings.length === 0}>Export CSV</button>
        </div>

        {summary && (
          <div className="grid gap-3 sm:grid-cols-3 text-sm">
            {MEALS.map(m => (
              <div key={m} className="bg-amber-50 border border-amber-200 rounded-lg p-3">
                <div className="capitalize font-semibold text-amber-900">{m}</div>
                <div className="text-2xl font-bold text-amber-800">{summary[m]?.served ?? 0} <span className="text-base font-normal text-gray-600">/ {summary[m]?.booked ?? 0} booked</span></div>
                <div className="text-xs text-gray-600">served at the gate</div>
              </div>
            ))}
          </div>
        )}

        <div className="grid gap-4 lg:grid-cols-3">
          <div className="lg:col-span-2 bg-white border rounded-lg" aria-busy={loading}>
            <div className="overflow-auto" style={{ maxHeight: '55vh' }}>
              <table className="min-w-full text-sm">
                <thead className="bg-gray-50 sticky top-0">
                  <tr>{['Student', 'Course', 'Meal', 'Source', 'Served', ''].map((h, i) => <th key={i} className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">{h}</th>)}</tr>
                </thead>
                <tbody className="divide-y">
                  {bookings.map(b => (
                    <tr key={b.id}>
                      <td className="px-3 py-1 whitespace-nowrap">{b.student?.full_name || <span className="text-gray-400">deleted</span>} <span className="text-xs text-gray-400">{b.student_code}</span></td>
                      <td className="px-3 py-1 text-gray-500">{b.student?.course_code || '-'}{b.student?.section ? ` / ${b.student.section}` : ''}</td>
                      <td className="px-3 py-1 capitalize">{b.meal}</td>
                      <td className="px-3 py-1 text-gray-500">{b.source}</td>
                      <td className="px-3 py-1">{b.consumed_at ? <span className="text-green-700">{new Date(b.consumed_at).toLocaleTimeString()}</span> : <span className="text-gray-400">not yet</span>}</td>
                      <td className="px-3 py-1 text-right"><button className="p-1.5 rounded bg-red-50 text-red-700 hover:bg-red-100" onClick={() => remove(b)} title="Remove"><Trash2 className="w-4 h-4" /></button></td>
                    </tr>
                  ))}
                  {!loading && bookings.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-500">No bookings for this day.</td></tr>}
                </tbody>
              </table>
            </div>
          </div>

          <div className="space-y-4">
            <div className="bg-white border rounded-lg p-4 space-y-3">
              <div className="text-sm font-medium text-gray-800 flex items-center gap-2"><Plus className="w-4 h-4" /> Book a meal</div>
              <div className="relative">
                <input className="w-full rounded border px-3 py-2 text-sm" placeholder="Student name, code or email" value={picked ? `${picked.full_name} (${picked.student_code})` : query}
                  onChange={e => { setPicked(null); setQuery(e.target.value) }} />
                {!picked && matches.length > 0 && (
                  <div className="absolute z-10 mt-1 w-full bg-white border rounded shadow max-h-48 overflow-auto">
                    {matches.map(s => <button key={s.student_code} className="block w-full text-left px-3 py-1.5 text-sm hover:bg-gray-50" onClick={() => { setPicked(s); setQuery('') }}>{s.full_name} <span className="text-gray-400">{s.student_code}</span></button>)}
                  </div>
                )}
              </div>
              <input type="date" className="w-full rounded border px-3 py-2 text-sm" value={bookDate} onChange={e => setBookDate(e.target.value)} />
              <div className="flex gap-3 text-sm">
                {MEALS.map(m => (
                  <label key={m} className="inline-flex items-center gap-1 capitalize">
                    <input type="checkbox" checked={bookMeals.includes(m)} onChange={e => setBookMeals(e.target.checked ? [...bookMeals, m] : bookMeals.filter(x => x !== m))} /> {m}
                  </label>
                ))}
              </div>
              <button className="w-full px-3 py-2 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50 text-sm" disabled={!picked || bookMeals.length === 0 || saving} onClick={book}>{saving ? 'Booking…' : 'Book'}</button>
            </div>

            <div className="bg-white border rounded-lg p-4 space-y-2">
              <div className="text-sm font-medium text-gray-800 flex items-center gap-2"><Upload className="w-4 h-4" /> Import CSV for {bookDate}</div>
              <p className="text-xs text-gray-500">Header row, then <code>student_code</code> or <code>email</code>, and <code>meals</code> like <code>breakfast|lunch</code>. A <code>date</code> column overrides the date above. Your booking system can also call <code>POST /meals/bookings</code>.</p>
              <input ref={fileRef} type="file" accept=".csv,text/csv" className="text-sm" onChange={e => { const f = e.target.files?.[0]; if (f) importCsv(f) }} />
              {importResult && (
                <div className="text-xs text-gray-700">
                  Created {importResult.created}, already booked {importResult.already_booked}.
                  {importResult.errors?.length ? <ul className="mt-1 list-disc pl-4 text-red-700">{importResult.errors.slice(0, 10).map((e: string, i: number) => <li key={i}>{e}</li>)}</ul> : null}
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </Layout>
  )
}
