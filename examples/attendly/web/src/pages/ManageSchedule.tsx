import { useEffect, useMemo, useState, useRef } from 'react'
import axios from 'axios'
import { config } from '../config/environment'
import Layout from '../components/Layout'
import { coursesService, roomsService, schedulesService, type DayName } from '../services/admin'
import { Plus, SlidersHorizontal, X as XIcon, Clock, Building2, ChevronDown, Pencil, Trash2 } from 'lucide-react'
import { useConfirm } from '../components/ConfirmProvider'
import { messages } from '../messages'

type Room = { id: number; name: string }
type Course = { code: string; name?: string; sections: string[] }
type Template = { id: string; day_of_week: DayName; course: string; section: string; room_id: number; start: string; end: string; active: boolean }

export default function ManageSchedule() {
  const API = config.API_BASE_URL
  const allDays: DayName[] = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
  const [activeDay, setActiveDay] = useState<DayName>('Monday')
  const [rooms, setRooms] = useState<Room[]>([])
  const [courses, setCourses] = useState<Course[]>([])
  const [templates, setTemplates] = useState<Template[]>([])
  const [templatesLoading, setTemplatesLoading] = useState<boolean>(false)

  const [course, setCourse] = useState('')
  const [section, setSection] = useState('')
  const [roomId, setRoomId] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [dayInForm, setDayInForm] = useState<DayName>('Monday')
  const [isFormOpen, setIsFormOpen] = useState<boolean>(false)
  const [showFilters, setShowFilters] = useState<boolean>(false)
  const filterContentRef = useRef<HTMLDivElement>(null)
  const [filterHeight, setFilterHeight] = useState<number>(0)
  const lastScrollYRef = useRef<number>(0)
  const [isSaving, setIsSaving] = useState<boolean>(false)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const toastTimeoutRef = useRef<number | null>(null)
  const { confirm } = useConfirm()
  // Track in-flight loads to avoid race conditions and flicker
  const loadSeqRef = useRef<number>(0)
  const lastLoadRef = useRef<{ id: number; day: DayName } | null>(null)

  // Filters for viewing schedule list
  const [filterDay, setFilterDay] = useState<DayName>('Monday')
  const [filterCourse, setFilterCourse] = useState<string>('')
  const [filterSection, setFilterSection] = useState<string>('')
  const [filterRoomId, setFilterRoomId] = useState<string>('')

  const loadRooms = async () => {
    const res = await roomsService.list()
    const list = Array.isArray(res.data) ? res.data : []
    setRooms(list.map((r: any) => ({ id: Number(r.id), name: String(r.name) })))
  }

  const loadCourses = async () => {
    const res = await coursesService.list()
    const list = Array.isArray(res.data) ? res.data : []
    setCourses(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined, sections: Array.isArray(c.sections_list) ? c.sections_list.map(String) : [] })))
  }

  const abortRef = useRef<AbortController | null>(null)
  const loadTemplates = async (day: DayName) => {
    const id = ++loadSeqRef.current
    lastLoadRef.current = { id, day }
    // Cancel previous in-flight request
    try { abortRef.current?.abort() } catch {}
    abortRef.current = new AbortController()
    try { setTemplatesLoading(true) } catch {}
    try {
      const res = await schedulesService.listByDay(day, { signal: abortRef.current.signal })
      const list = Array.isArray(res.data) ? res.data : []
      // Only apply if this is the latest request and for the current day
      const isLatest = lastLoadRef.current?.id === id && lastLoadRef.current?.day === day
      if (isLatest) {
      setTemplates(list.map((t: any) => ({ id: String(t.id), day_of_week: day, course: String(t.course), section: String(t.section), room_id: Number(t.room_id), start: String(t.start), end: String(t.end), active: Boolean(t.active) })))
      }
    } finally {
      // Only clear loading if this is the latest request
      if (lastLoadRef.current?.id === id) {
      try { setTemplatesLoading(false) } catch {}
      }
    }
  }

  useEffect(() => { 
    loadRooms(); 
    loadCourses(); 
    setDayInForm(activeDay)
    setFilterDay(activeDay)
    // Do not call loadTemplates here; the effect below will run on mount
  }, [])
  useEffect(() => { 
    // Keep default day in form aligned when switching tabs (unless editing)
    if (!editing) setDayInForm(activeDay)
    setFilterDay(activeDay)
    // Clear current view immediately to avoid showing stale data
    setTemplates([])
    try { setTemplatesLoading(true) } catch {}
    // Load templates whenever the active day changes
    loadTemplates(activeDay) 
    return () => {
      // Cancel any in-flight request when day changes or component unmounts
      try { abortRef.current?.abort() } catch {}
    }
  }, [activeDay])

  // Sections for the Add/Edit form (fetched on course change)
  const [formSections, setFormSections] = useState<string[]>([])
  const [formSectionsLoading, setFormSectionsLoading] = useState<boolean>(false)
  const sections = useMemo(() => (courses.find(c => c.code === course)?.sections || []), [courses, course])

  const loadFormSections = async (code: string) => {
    try {
      setFormSectionsLoading(true)
      if (!code) { setFormSections([]); return }
      const res = await coursesService.sections(code)
      const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((s: any) => String(s)) : []
      setFormSections(arr)
    } catch {
      setFormSections([])
    } finally {
      setFormSectionsLoading(false)
    }
  }

  useEffect(() => {
    if (isFormOpen && course) {
      loadFormSections(course)
    } else if (!course) {
      setFormSections([])
    }
  }, [isFormOpen, course])

  // Toast helper
  const showToast = (type: 'success' | 'error', message: string) => {
    if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current)
    setToast({ type, message })
    toastTimeoutRef.current = window.setTimeout(() => setToast(null), 2500)
  }
  useEffect(() => {
    return () => {
      if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current)
    }
  }, [])

  const onStartChange = (v: string) => {
    setStart(v)
    // Default end = start + 1 hour if empty or before start
    if (!end || end <= v) {
      try {
        const [hh, mm] = v.split(':').map(Number)
        const d = new Date()
        d.setHours(hh, mm, 0, 0)
        d.setHours(d.getHours() + 1)
        const eh = String(d.getHours()).padStart(2, '0')
        const em = String(d.getMinutes()).padStart(2, '0')
        setEnd(`${eh}:${em}`)
      } catch {}
    }
  }

  // --- Custom time picker (selects) to avoid browser-specific glitches ---
  const hours: string[] = useMemo(() => Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0')), [])
  const minutes: string[] = useMemo(() => Array.from({ length: 60 }, (_, i) => String(i).padStart(2, '0')), [])
  const startHour = start?.slice(0, 2) || '09'
  const startMinute = start?.slice(3, 5) || '00'
  const endHour = end?.slice(0, 2) || '10'
  const endMinute = end?.slice(3, 5) || '00'
  const setStartHM = (h: string, m: string) => {
    const s = `${h}:${m}`
    onStartChange(s)
  }
  const setEndHM = (h: string, m: string) => {
    const e = `${h}:${m}`
    setEnd(e)
  }

  const resetForm = () => {
    setCourse(''); setSection(''); setRoomId(''); setStart(''); setEnd(''); setEditing(null); setError(null); setDayInForm(activeDay)
  }

  const save = async () => {
    if (isSaving) return
    setError(null)
    const rid = Number(roomId || '0')
    if (!course || !section || !rid || !/^\d{2}:\d{2}$/.test(start) || !/^\d{2}:\d{2}$/.test(end) || !(start < end)) {
      setError('All fields are required and times must be valid (start < end).')
      setIsSaving(false)
      return
    }
    try {
      setIsSaving(true)
      if (editing) {
        await schedulesService.updateTemplate(editing, { day_of_week: dayInForm, course, section, room_id: rid, start, end, active: true })
      } else {
        await schedulesService.createTemplate({ day_of_week: dayInForm, course, section, room_id: rid, start, end, active: true })
      }
      // Ensure the room has this course whitelisted (engine may ignore schedules for non-listed courses)
      try {
        await axios.post(`${API}/streams/${rid}/courses`, null, { params: { course_code: course } })
      } catch (e) {
        console.warn('Failed to add course to room (may already exist):', e)
      }
      // Immediately create a concrete schedule entry so StreamEngine picks it up now
      try {
        await axios.post(`${API}/schedules`, {
          day_of_week: dayInForm,
          course_code: course,
          section: section,
          room_id: rid,
          start_time: start,
          end_time: end,
        })
      } catch (e) {
        // Non-fatal: show a hint and continue
        console.warn('Failed to create concrete schedule for engine:', e)
      }
      // Reload view based on filter day
      await loadTemplates(filterDay)
      resetForm()
      setIsFormOpen(false)
      showToast('success', editing ? 'Schedule updated' : 'Schedule added')
    } catch (e: any) {
      setError(String(e?.response?.data?.detail || e?.message || 'Failed to save'))
      showToast('error', 'Failed to save schedule')
    } finally {
      setIsSaving(false)
    }
  }

  const onEdit = (tpl: Template) => {
    setEditing(tpl.id)
    setCourse(tpl.course)
    setSection(tpl.section)
    setRoomId(String(tpl.room_id))
    setStart(tpl.start)
    setEnd(tpl.end)
    setDayInForm(activeDay)
    setError(null)
    setIsFormOpen(true)
  }

  const onDelete = async (tpl: Template) => {
    const ok = await confirm({ description: messages.manage.confirmDeleteSchedule, confirmText: messages.common.delete })
    if (!ok) return
    await schedulesService.deleteTemplate(tpl.id)
    await loadTemplates(activeDay)
  }

  // Compute section options for filter based on selected filterCourse
  const filterSections = useMemo(() => (courses.find(c => c.code === filterCourse)?.sections || []), [courses, filterCourse])

  // Apply filters to templates list
  const filteredTemplates = useMemo(() => {
    return templates.filter(t => {
      if (filterCourse && t.course !== filterCourse) return false
      if (filterSection && t.section !== filterSection) return false
      if (filterRoomId && String(t.room_id) !== filterRoomId) return false
      return true
    })
  }, [templates, filterCourse, filterSection, filterRoomId])

  const sortedTemplates = useMemo(() => {
    const toNum = (t: string) => parseInt(t.slice(0, 2)) * 60 + parseInt(t.slice(3, 5))
    return [...filteredTemplates].sort((a, b) => toNum(a.start) - toNum(b.start))
  }, [filteredTemplates])

  const isFormValid = useMemo(() => {
    return Boolean(
      course && section && roomId && /\d{2}:\d{2}/.test(start) && /\d{2}:\d{2}/.test(end) && start < end
    )
  }, [course, section, roomId, start, end])

  // Smooth filter expand/collapse without shifting the viewport abruptly
  useEffect(() => {
    const measure = () => setFilterHeight(filterContentRef.current ? filterContentRef.current.scrollHeight : 0)
    // Measure after layout changes
    const id = window.setTimeout(measure, 0)
    window.addEventListener('resize', measure)
    return () => { window.clearTimeout(id); window.removeEventListener('resize', measure) }
  }, [showFilters, courses, rooms, filterCourse, filterSection])

  // No JS height; match student data table's CSS-only approach using calc(100vh - 290px)

  return (
    <Layout title="Manage Class Schedule">
      {toast && (
        <div className="fixed right-4 top-4 z-[60]">
          <div className={`px-4 py-2 rounded-md shadow border text-sm ${toast.type === 'success' ? 'bg-green-50 text-green-800 border-green-200' : 'bg-red-50 text-red-800 border-red-200'}`}>
            {toast.message}
          </div>
        </div>
      )}
      <div className="space-y-2">
        <div className="bg-white border rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900">
            <Clock className="w-5 h-5 text-gray-500" />
            <span>Schedule</span>
            <span className="text-sm font-normal text-gray-500">— Manage weekly schedules</span>
          </div>
          <div className="flex items-center gap-2">
            <button
              className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm bg-indigo-600 text-white hover:bg-indigo-700"
              onClick={() => { resetForm(); setIsFormOpen(true) }}
            >
              <Plus className="w-4 h-4" /> Add Schedule
              </button>
          </div>
        </div>
        {/* Days moved into Filters container above */}

        {/* Filters */}
        <div className="bg-white border rounded-lg p-4">
          <div className="flex items-center justify-between mb-3">
            <div className="text-base font-medium text-gray-900 flex items-center gap-2">
              <SlidersHorizontal className="w-4 h-4 text-gray-500" /> Filters
            </div>
            <div className="flex-1 flex items-center justify-center">
              <div className="flex flex-wrap gap-2">
                {allDays.map(d => (
                  <button key={d} className={`px-2 py-1 rounded-md text-xs border ${activeDay === d ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-gray-700 hover:bg-gray-50'}`} onClick={() => setActiveDay(d)}>
                    {d}
                  </button>
                ))}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button
                className="inline-flex items-center justify-center p-2 rounded-md bg-gray-100 hover:bg-gray-200"
                aria-label={showFilters ? 'Collapse filters' : 'Expand filters'}
                onClick={() => {
                  lastScrollYRef.current = window.scrollY
                  setShowFilters(v => !v)
                  setTimeout(() => window.scrollTo(0, lastScrollYRef.current), 0)
                }}
              >
                <ChevronDown className={`w-4 h-4 transition-transform ${showFilters ? 'rotate-180' : ''}`} />
              </button>
            </div>
          </div>
          <div
            ref={filterContentRef}
            aria-hidden={!showFilters}
            className="overflow-hidden transition-all duration-200"
            style={{ maxHeight: showFilters ? filterHeight : 0, opacity: showFilters ? 1 : 0 }}
          >
          <div className="grid gap-3 md:grid-cols-4">
            <div>
              <label className="block text-xs text-gray-700 mb-1">Day</label>
              <select className="w-full rounded border px-2 py-1.5 text-sm" value={filterDay} onChange={async e => { const d = e.target.value as DayName; setFilterDay(d); setActiveDay(d) }}>
                {allDays.map(d => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-700 mb-1">Course</label>
              <select className="w-full rounded border px-2 py-1.5 text-sm max-w-[260px] truncate" value={filterCourse} onChange={e => { setFilterCourse(e.target.value); setFilterSection('') }} title={(() => { const c=courses.find(x=>x.code===filterCourse); return c ? `${c.code}${c.name?` — ${c.name}`:''}` : '' })()}>
                <option value="">All</option>
                {courses.map(c => {
                  const full = `${c.code}${c.name ? ` — ${c.name}` : ''}`
                  const short = full.length > 36 ? `${full.slice(0,36)}…` : full
                  return (<option key={c.code} value={c.code} title={full}>{short}</option>)
                })}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-700 mb-1">Section</label>
              <select className="w-full rounded border px-2 py-1.5 text-sm" value={filterSection} onChange={e => setFilterSection(e.target.value)} disabled={!filterCourse}>
                <option value="">All</option>
                {filterSections.map(s => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs text-gray-700 mb-1">Room</label>
              <select className="w-full rounded border px-2 py-1.5 text-sm" value={filterRoomId} onChange={e => setFilterRoomId(e.target.value)}>
                <option value="">All</option>
                {rooms.map(r => (
                  <option key={r.id} value={String(r.id)}>{r.name}</option>
                ))}
              </select>
            </div>
          </div>
          </div>
        </div>

        {isFormOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
              <div className="flex items-start justify-between mb-3">
                <div>
                  <div className="text-lg font-semibold">{editing ? 'Edit Schedule' : 'Add Schedule'}</div>
                  <div className="text-sm text-gray-500 mt-0.5">Create a weekly recurring class schedule</div>
                </div>
                <button className={`p-1 ${isSaving ? 'text-gray-300 cursor-not-allowed' : 'text-gray-500 hover:text-gray-700'}`} disabled={isSaving} onClick={() => { if (isSaving) return; setIsFormOpen(false); resetForm() }}>
                  <XIcon className="w-5 h-5" />
                </button>
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Day</label>
                  <select className="w-full rounded border px-3 py-2" value={dayInForm} disabled={isSaving} onChange={e => setDayInForm(e.target.value as DayName)}>
                    {allDays.map(d => (
                      <option key={d} value={d}>{d}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Course</label>
                  <select className="w-full rounded border px-3 py-2 max-w-[260px] truncate" value={course} disabled={isSaving} onChange={async e => { const v = e.target.value; setCourse(v); setSection(''); await loadFormSections(v) }} title={(() => { const c=courses.find(x=>x.code===course); return c ? `${c.code}${c.name?` — ${c.name}`:''}` : '' })()}>
                    <option value="">Select course</option>
                    {courses.map(c => {
                      const full = `${c.code}${c.name ? ` — ${c.name}` : ''}`
                      const short = full.length > 36 ? `${full.slice(0,36)}…` : full
                      return (<option key={c.code} value={c.code} title={full}>{short}</option>)
                    })}
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Section</label>
                  <select className="w-full rounded border px-3 py-2" value={section} onChange={e => setSection(e.target.value)} disabled={!course || isSaving}>
                    <option value="">{formSectionsLoading ? 'Loading sections…' : 'Select section'}</option>
                    {!formSectionsLoading && formSections.map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                  {course && !formSectionsLoading && formSections.length === 0 && (
                    <div className="mt-1 text-xs text-gray-500">No sections found for selected course.</div>
                  )}
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Room</label>
                  <select className="w-full rounded border px-3 py-2" value={roomId} disabled={isSaving} onChange={e => setRoomId(e.target.value)}>
                    <option value="">Select room</option>
                    {rooms.map(r => (
                      <option key={r.id} value={String(r.id)}>{r.name}</option>
                    ))}
                  </select>
                </div>
                <div className="grid grid-cols-1 gap-4 items-start">
                  <div className="min-w-0 md:pb-0 pb-2">
                    <label className="block text-sm text-gray-700 mb-1">Start</label>
                    <div className="flex items-center gap-2 min-w-0">
                      <select className="rounded border px-2 py-2 w-24 text-center font-mono" value={startHour} disabled={isSaving} onChange={e => setStartHM(e.target.value, startMinute)}>
                        {hours.map(h => (<option key={h} value={h}>{h}</option>))}
                      </select>
                      <span className="text-gray-500">:</span>
                      <select className="rounded border px-2 py-2 w-24 text-center font-mono" value={startMinute} disabled={isSaving} onChange={e => setStartHM(startHour, e.target.value)}>
                        {minutes.map(m => (<option key={m} value={m}>{m}</option>))}
                      </select>
                    </div>
                  </div>
                  <div className="min-w-0 md:pb-0 pb-2">
                    <label className="block text-sm text-gray-700 mb-1">End</label>
                    <div className="flex items-center gap-2 min-w-0">
                      <select className="rounded border px-2 py-2 w-24 text-center font-mono" value={endHour} disabled={isSaving} onChange={e => setEndHM(e.target.value, endMinute)}>
                        {hours.map(h => (<option key={h} value={h}>{h}</option>))}
                      </select>
                      <span className="text-gray-500">:</span>
                      <select className="rounded border px-2 py-2 w-24 text-center font-mono" value={endMinute} disabled={isSaving} onChange={e => setEndHM(endHour, e.target.value)}>
                        {minutes.map(m => (<option key={m} value={m}>{m}</option>))}
                      </select>
                    </div>
                  </div>
                </div>
                <div className="mt-2 text-xs text-gray-500 flex items-center gap-1 md:col-span-2"><Clock className="w-3 h-3" /> Format: HH:MM (24h)</div>
              </div>
              {error && <div className="mt-2 text-sm text-red-600">{error}</div>}
              <div className="mt-4 flex justify-end gap-2">
                <button className={`px-3 py-1.5 rounded-md ${isSaving ? 'bg-gray-100 text-gray-400 cursor-not-allowed' : 'bg-gray-100 hover:bg-gray-200'}`} disabled={isSaving} onClick={() => { if (isSaving) return; setIsFormOpen(false); resetForm() }}>Cancel</button>
                <button className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md ${isFormValid && !isSaving ? 'bg-indigo-600 text-white hover:bg-indigo-700' : 'bg-gray-200 text-gray-500 cursor-not-allowed'}`} disabled={!isFormValid || isSaving} onClick={save}>
                  {isSaving && <span className="inline-block h-4 w-4 border-2 border-white/70 border-t-transparent rounded-full animate-spin" />}
                  {isSaving ? (editing ? 'Updating…' : 'Adding…') : (editing ? 'Update' : 'Add')}
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="bg-white rounded-lg shadow-sm border overflow-hidden" aria-busy={templatesLoading}>
          <div className="px-3 py-2 border-b bg-gray-50 flex items-center justify-between">
            <div className="text-sm text-gray-700">{filterDay} Schedules — Showing {sortedTemplates.length} item{sortedTemplates.length === 1 ? '' : 's'}</div>
            <div />
          </div>
           <div className="overflow-auto" style={{ maxHeight: 'calc(100vh - 330px)' }}>
            <table className="min-w-full divide-y divide-gray-200 text-sm">
              <thead className="bg-gray-50 sticky top-0 z-10">
                <tr className="bg-gray-50">
                  <th className="px-3 py-2 text-left font-medium text-gray-700">Time</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-700">Course</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-700">Section</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-700">Room</th>
                  <th className="px-3 py-2 text-left font-medium text-gray-700">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {templatesLoading && (
                  <tr>
                    <td colSpan={5} className="px-3 py-6 text-center text-sm text-gray-600">
                      <div className="inline-flex items-center gap-2">
                        <svg className="animate-spin h-4 w-4 text-gray-600" viewBox="0 0 24 24">
                          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                        </svg>
                        Loading schedules…
                      </div>
                    </td>
                  </tr>
                )}
                {sortedTemplates.map(t => (
                  <tr key={t.id}>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="inline-flex items-center gap-1 text-gray-800"><Clock className="w-4 h-4 text-gray-400" /> {t.start} – {t.end}</span>
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="inline-flex items-center gap-2">
                        <span className="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-100">{t.course}</span>
                      </span>
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="px-2 py-0.5 rounded-full bg-gray-100 text-gray-800 border border-gray-200">{t.section}</span>
                    </td>
                    <td className="px-3 py-2 whitespace-nowrap">
                      <span className="inline-flex items-center gap-1 text-gray-800"><Building2 className="w-4 h-4 text-gray-400" /> {rooms.find(r => r.id === t.room_id)?.name || `Room #${t.room_id}`}</span>
                    </td>
                    <td className="px-3 py-2 text-left">
                      <div className="inline-flex gap-2">
                        <button
                          className="inline-flex items-center justify-center p-2 rounded-md bg-gray-100 hover:bg-gray-200"
                          title="Edit"
                          aria-label="Edit"
                          onClick={() => onEdit(t)}
                        >
                          <Pencil className="w-4 h-4" />
                        </button>
                        <button
                          className="inline-flex items-center justify-center p-2 rounded-md bg-red-50 text-red-700 hover:bg-red-100"
                          title="Delete"
                          aria-label="Delete"
                          onClick={() => onDelete(t)}
                        >
                          <Trash2 className="w-4 h-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
                {!templatesLoading && sortedTemplates.length === 0 && (
                  <tr>
                    <td colSpan={5} className="px-3 py-6 text-center text-sm text-gray-500">
                      <div className="max-w-md mx-auto">
                        <div className="text-gray-600 mb-2">No schedules match your filters.</div>
                        <button className="inline-flex items-center gap-1 text-indigo-600 hover:underline" onClick={() => { resetForm(); setIsFormOpen(true) }}>
                          <Plus className="w-3 h-3" /> Add schedule
                        </button>
                      </div>
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </Layout>
  )
}


