import { useEffect, useMemo, useState, useRef } from 'react'
import { authService } from '../services/auth'
import axios from 'axios'
import Layout from '../components/Layout'
import { config } from '../config/environment'
import { SlidersHorizontal, FileText, FileSpreadsheet, CheckCircle, ChevronDown, ChevronUp } from 'lucide-react'
import { useToast } from '../components/ToastProvider'
import { messages } from '../messages'
// @ts-ignore - runtime import available after install
import jsPDF from 'jspdf'
// @ts-ignore - runtime import available after install
import autoTable from 'jspdf-autotable'

const API = config.API_BASE_URL

type Student = { id: number; student_code: string; full_name: string; class_name?: string; course_code?: string; section?: string; email?: string | null }
type FinalizedAttendance = {
  id: number
  class_id: string
  student?: Student | null
  slots_present: number
  total_slots: number
  ratio: number
  status: 'PRESENT' | 'ABSENT' | 'UNWATCHED'
  finalized_at: string
}

export default function Attendance() {
  const user = authService.getUser()
  const { showToast } = useToast()
  // const isAdmin = !!(user && user.is_admin)
  const [classId, setClassId] = useState('')
  const [items, setItems] = useState<FinalizedAttendance[]>([])
  const [loading, setLoading] = useState(false)
  const [classes, setClasses] = useState<string[]>([])
  const [sectionId, setSectionId] = useState('')
  const [date, setDate] = useState('')
  const [classesLoading, setClassesLoading] = useState(false)
  // New filters
  const [course, setCourse] = useState('')
  const [section, setSection] = useState('')
  const [dateSlot, setDateSlot] = useState('')
  const [courses, setCourses] = useState<Array<{ code: string; name?: string }>>([])
  const [sections, setSections] = useState<string[]>([])
  const [sectionsLoading, setSectionsLoading] = useState<boolean>(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [isExportOpen, setIsExportOpen] = useState(false)
  const [showFilters, setShowFilters] = useState(false)
  const filterContentRef = useRef<HTMLDivElement>(null)
  const [filterHeight, setFilterHeight] = useState(0)
  const exportMenuRef = useRef<HTMLDivElement>(null)
  
  // Sorting state
  const [sortField, setSortField] = useState<string | null>(null)
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('asc')
  useEffect(() => {
    try { document.body.style.cursor = loading ? 'progress' : 'auto' } catch {}
    return () => { try { document.body.style.cursor = 'auto' } catch {} }
  }, [loading])

  // Measure collapsible filter content height
  useEffect(() => {
    const measure = () => setFilterHeight(filterContentRef.current ? filterContentRef.current.scrollHeight : 0)
    const id = window.setTimeout(measure, 0)
    window.addEventListener('resize', measure)
    return () => { window.clearTimeout(id); window.removeEventListener('resize', measure) }
  }, [courses, sections, course, section])

  // Close export menu on outside click or Escape
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (!exportMenuRef.current) return
      if (!exportMenuRef.current.contains(e.target as Node)) setIsExportOpen(false)
    }
    const handleKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') setIsExportOpen(false) }
    window.addEventListener('mousedown', handleClickOutside)
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.removeEventListener('mousedown', handleClickOutside)
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [])

  const loadByClassId = async (cid: string) => {
    if (!cid) return
    setLoading(true)
    try {
      const res = await axios.get<FinalizedAttendance[]>(`${API}/attendance/${encodeURIComponent(cid)}`)
      setItems(Array.isArray(res.data) ? res.data : [])
    } catch (e) {
      setItems([])
    } finally {
      setLoading(false)
    }
  }

  const loadBySection = async () => {
    if (!classId) return
    setLoading(true)
    try {
      const q = date ? `?date=${encodeURIComponent(date)}` : ''
      const res = await axios.get<FinalizedAttendance[]>(`${API}/attendance/by-name/${encodeURIComponent(classId)}${q}`)
      setItems(Array.isArray(res.data) ? res.data : [])
    } catch (e) {
      setItems([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    // Load available classes for quick access
    const loadClasses = async () => {
      setClassesLoading(true)
      try {
        const res = await axios.get<string[]>(`${API}/attendance/classes`)
        const list = Array.isArray(res.data) ? res.data : []
        // Keep only the most recent 9
        const recent = list.slice(0, 9)
        setClasses(recent)
        // Auto-select the most recent class by default
        if (recent.length > 0) {
          const mostRecent = recent[0]
          setClassId(mostRecent)
          try { await loadByClassId(mostRecent) } catch {}
        }
      } catch (e) {
        setClasses([])
      } finally {
        setClassesLoading(false)
      }
    }
    loadClasses()
    // Load courses for filter
    const loadCourses = async () => {
      try {
        const res = await axios.get(`${API}/planner/courses`)
        const list = Array.isArray(res.data) ? res.data : []
        setCourses(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined })))
      } catch {
        setCourses([])
      }
    }
    loadCourses()
  }, [])

  // When course changes, load sections
  useEffect(() => {
    const loadSections = async () => {
      if (!course) { setSections([]); setSection(''); return }
      try {
        setSectionsLoading(true)
        const res = await axios.get(`${API}/courses/${encodeURIComponent(course)}/sections`)
        const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((s: any) => String(s)) : []
        setSections(arr)
      } catch {
        setSections([])
      } finally {
        setSectionsLoading(false)
      }
    }
    loadSections()
  }, [course])

  const canFilter = useMemo(() => Boolean(course && section && dateSlot), [course, section, dateSlot])

  const loadByFilters = async () => {
    if (!canFilter) return
    // Clear recent-class selection when applying custom filters
    try { setClassId('') } catch {}
    setLoading(true)
    try {
      // Build expected class_id pattern using selected date slot as prefix filter is not guaranteed,
      // so fallback to backend endpoint by course
      const res = await axios.get<FinalizedAttendance[]>(`${API}/attendance/by-course/${encodeURIComponent(course)}?date=${encodeURIComponent(dateSlot)}`)
      const list = Array.isArray(res.data) ? res.data : []
      // Filter by section only when student section is present; keep rows without student too (e.g., ABSENT)
      setItems(list.filter((r) => (r.student?.section ? r.student.section === section : true)))
    } catch (e) {
      setItems([])
    } finally {
      setLoading(false)
    }
  }

  // Sorting function
  const handleSort = (field: string) => {
    if (sortField === field) {
      setSortDirection(sortDirection === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDirection('asc')
    }
  }

  const visibleItems = useMemo(() => {
    let filtered = items
    
    // Apply search filter
    const q = searchQuery.trim().toLowerCase()
    if (q) {
      filtered = items.filter((r) =>
        (r.student?.full_name || '').toLowerCase().includes(q) ||
        (r.student?.student_code || '').toLowerCase().includes(q) ||
        (r.student?.email || '').toLowerCase().includes(q)
      )
    }
    
    // Apply sorting
    if (sortField) {
      filtered = [...filtered].sort((a, b) => {
        let aValue: any
        let bValue: any
        
        switch (sortField) {
          case 'student_name':
            aValue = a.student?.full_name || ''
            bValue = b.student?.full_name || ''
            break
          case 'email':
            aValue = a.student?.email || ''
            bValue = b.student?.email || ''
            break
          case 'course':
            aValue = a.student?.course_code || ''
            bValue = b.student?.course_code || ''
            break
          case 'section':
            aValue = a.student?.section || ''
            bValue = b.student?.section || ''
            break
          case 'schedule':
            // Extract schedule from class_id
            const getSchedule = (cid: string) => {
              const tail = cid.slice(-9)
              let m = tail.match(/^(\d{2})(\d{2})[-_](\d{2})(\d{2})$/)
              if (m) {
                const [, h1, m1, h2, m2] = m
                return `${h1}:${m1} – ${h2}:${m2}`
              }
              m = cid.match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})/)
              if (m) {
                return `${m[1]} – ${m[2]}`
              }
              return '-'
            }
            aValue = getSchedule(a.class_id)
            bValue = getSchedule(b.class_id)
            break
          case 'status':
            aValue = a.status
            bValue = b.status
            break
          case 'presence':
            aValue = a.ratio
            bValue = b.ratio
            break
          default:
            return 0
        }
        
        // Handle string comparison
        if (typeof aValue === 'string' && typeof bValue === 'string') {
          const comparison = aValue.localeCompare(bValue)
          return sortDirection === 'asc' ? comparison : -comparison
        }
        
        // Handle numeric comparison
        if (typeof aValue === 'number' && typeof bValue === 'number') {
          return sortDirection === 'asc' ? aValue - bValue : bValue - aValue
        }
        
        return 0
      })
    }
    
    return filtered
  }, [items, searchQuery, sortField, sortDirection])

  const exportPdf = () => {
    try {
      const doc = new jsPDF({ orientation: 'landscape', unit: 'pt', format: 'A4' })
      const title = 'Attendance Report'
      const subtitle = [
        course ? `Course: ${course}` : null,
        section ? `Section: ${section}` : null,
        dateSlot ? `Date: ${dateSlot}` : null,
      ].filter(Boolean).join('  |  ')

      doc.setFont('helvetica', 'bold')
      doc.setFontSize(16)
      doc.text(title, 40, 40)
      doc.setFontSize(10)
      doc.setFont('helvetica', 'normal')
      if (subtitle) doc.text(subtitle, 40, 58)
      doc.setFontSize(9)
      doc.text(`Generated: ${new Date().toLocaleString()}`, 40, 74)

      const head = [[
        'Student Name', 'Email', 'Code', 'Course', 'Section', 'Schedule', 'Status', 'Presence %'
      ]]
      const body = visibleItems.map(r => {
        const cid = String(r.class_id || '')
        const tail = cid.slice(-9)
        let schedule = '-'
        let m = tail.match(/^(\d{2})(\d{2})[-_](\d{2})(\d{2})$/)
        if (m) {
          const [, h1, m1, h2, m2] = m
          schedule = `${h1}:${m1} – ${h2}:${m2}`
        } else {
          const mm = cid.match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})/)
          if (mm) schedule = `${mm[1]} – ${mm[2]}`
        }
        const pct = `${Math.round(r.ratio * 100)}%`
        return [
          r.student?.full_name ?? '-',
          r.student?.email ?? '-',
          r.student?.student_code ?? '-',
          r.student?.course_code ?? '-',
          r.student?.section ?? '-',
          schedule,
          r.status,
          pct,
        ]
      })

      autoTable(doc as any, {
        head,
        body,
        startY: 90,
        styles: { fontSize: 9, cellPadding: 6 },
        headStyles: { fillColor: [17, 24, 39] },
        alternateRowStyles: { fillColor: [245, 247, 250] },
        didDrawPage: () => {
          const pageSize = doc.internal.pageSize
          const pageWidth = pageSize.getWidth()
          doc.setFontSize(8)
          try {
            // @ts-ignore - available at runtime
            const total = (doc as any).getNumberOfPages ? (doc as any).getNumberOfPages() : 1
            doc.text(`Page ${total}`, pageWidth - 80, pageSize.getHeight() - 20)
          } catch {
            doc.text(`Page 1`, pageWidth - 80, pageSize.getHeight() - 20)
          }
        },
      })

      const fnameParts = ['attendance']
      if (course) fnameParts.push(course)
      if (section) fnameParts.push(section)
      if (dateSlot) fnameParts.push(dateSlot)
      const filename = `${fnameParts.join('_')}.pdf`
      doc.save(filename)
    } catch (e) {
      showToast('error', messages.attendance.exportPdfFailed)
    }
  }

  const exportExcel = () => {
    try {
      // Lazy import to reduce initial bundle size
      // @ts-ignore - dynamic import without types
      import('xlsx').then((XLSX: any) => {
        const rows = visibleItems.map((r) => {
          const cid = String(r.class_id || '')
          const tail = cid.slice(-9)
          let schedule = '-'
          let m = tail.match(/^(\d{2})(\d{2})[-_](\d{2})(\d{2})$/)
          if (m) {
            const [, h1, m1, h2, m2] = m
            schedule = `${h1}:${m1} – ${h2}:${m2}`
          } else {
            const mm = cid.match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})/)
            if (mm) schedule = `${mm[1]} – ${mm[2]}`
          }
          return {
            'Student Name': r.student?.full_name ?? '-',
            Email: r.student?.email ?? '-',
            Code: r.student?.student_code ?? '-',
            Course: r.student?.course_code ?? '-',
            Section: r.student?.section ?? '-',
            Schedule: schedule,
            Status: r.status,
            'Presence %': `${Math.round(r.ratio * 100)}%`,
          }
        })
        const ws = XLSX.utils.json_to_sheet(rows)
        const wb = XLSX.utils.book_new()
        XLSX.utils.book_append_sheet(wb, ws, 'Attendance')
        const fnameParts = ['attendance']
        if (course) fnameParts.push(course)
        if (section) fnameParts.push(section)
        if (dateSlot) fnameParts.push(dateSlot)
        const filename = `${fnameParts.join('_')}.xlsx`
        XLSX.writeFile(wb, filename)
      })
    } catch (e) {
      showToast('error', messages.attendance.exportExcelFailed)
    }
  }

  return (
    <Layout title="Attendance Records">
      <div className="bg-white border rounded-lg p-4 mb-2 flex items-center justify-between">
        <div className="flex items-center gap-2 text-base font-medium text-gray-900">
          <CheckCircle className="w-5 h-5 text-gray-500" />
          <span>Attendance</span>
          <span className="text-sm font-normal text-gray-500">— View and export attendance report</span>
        </div>
        <div />
      </div>
      { <>
        <div className="bg-white border rounded-lg p-4">
          {/* <div className="p-4 border-b flex items-center gap-3">
            <input
              className="rounded border px-3 py-2 font-mono text-sm"
              placeholder="Enter class name (e.g., Class_20250909_0800)"
              value={classId}
              onChange={(e) => setClassId(e.target.value)}
            />
            <button
              className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
              onClick={() => loadByClassId(classId)}
            >
              {loading ? 'Loading…' : 'Load'}
            </button>
          </div> */}
          {/* Recent classes section removed as per request */}
          <div className="flex items-center justify-between mb-3">
            <div className="text-base font-medium text-gray-900 flex items-center gap-2">
              <SlidersHorizontal className="w-4 h-4 text-gray-500" /> Filters
            </div>
            <div className="flex-1 flex items-center justify-center">
              {classesLoading ? (
                <div className="flex items-center gap-2 text-sm text-gray-500">
                  <svg className="animate-spin h-3 w-3 text-gray-500" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                  </svg>
                  Loading recent classes...
                </div>
              ) : (
                <div className="flex flex-wrap gap-2">
                  {classes.slice(0, 5).map(cid => (
                    <button
                      key={cid}
                      className={`px-2 py-1 rounded border text-xs max-w-[160px] ${classId === cid ? 'bg-gray-800 text-white border-gray-800' : 'bg-white text-gray-700 hover:bg-gray-50'}`}
                      title={cid}
                      onClick={() => {
                        // Clear custom filters when selecting a recent class
                        try { setCourse(''); setSection(''); setDateSlot('') } catch {}
                        setClassId(cid); loadByClassId(cid)
                      }}
                    >
                      <span className="truncate inline-block align-bottom max-w-[140px]">{cid}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
            <button
              className="inline-flex items-center justify-center p-2 rounded-md bg-gray-100 hover:bg-gray-200"
              aria-label={showFilters ? 'Collapse filters' : 'Expand filters'}
              onClick={() => setShowFilters(v => !v)}
            >
              <ChevronDown className={`w-4 h-4 transition-transform ${showFilters ? 'rotate-180' : ''}`} />
            </button>
          </div>
          <div
            ref={filterContentRef}
            aria-hidden={!showFilters}
            className="overflow-hidden transition-all duration-200"
            style={{ maxHeight: showFilters ? filterHeight : 0, opacity: showFilters ? 1 : 0 }}
          >
            <div>
              <div className="grid gap-3 md:grid-cols-4 items-end">
                <div>
                  <label className="block text-xs text-gray-700 mb-1">Course</label>
                  <select className="w-full rounded border px-2 py-1.5 text-sm max-w-[260px] truncate" value={course} onChange={e => { setCourse(e.target.value); setSection('') }} title={(() => { const c=courses.find(x=>x.code===course); return c ? `${c.code}${c.name?` — ${c.name}`:''}` : '' })()}>
                    <option value="">Select course</option>
                    {courses.map(c => {
                      const full = `${c.code}${c.name ? ` — ${c.name}` : ''}`
                      const short = full.length > 36 ? `${full.slice(0,36)}…` : full
                      return (<option key={c.code} value={c.code} title={full}>{short}</option>)
                    })}
                  </select>
                </div>
                <div>
                  <label className="block text-xs text-gray-700 mb-1">Section</label>
                  <select className="w-full rounded border px-2 py-1.5 text-sm" value={section} onChange={e => setSection(e.target.value)} onFocus={() => { if (course && sections.length === 0 && !sectionsLoading) { (async () => { try { setSectionsLoading(true); const res = await axios.get(`${API}/courses/${encodeURIComponent(course)}/sections`); const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((s: any) => String(s)) : []; setSections(arr); } catch { setSections([]) } finally { setSectionsLoading(false) } })() } }} disabled={!course}>
                    <option value="">{sectionsLoading ? 'Loading sections…' : 'Select section'}</option>
                    {!sectionsLoading && sections.map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                  {course && !sectionsLoading && sections.length === 0 && (
                    <div className="mt-1 text-[11px] text-gray-500">No sections found for selected course.</div>
                  )}
                </div>
                <div>
                  <label className="block text-xs text-gray-700 mb-1">Date</label>
                  <div className="flex items-center gap-2">
                    <input type="date" className="w-full rounded border px-2 py-1.5 text-sm" value={dateSlot} onChange={e => setDateSlot(e.target.value)} />
                  </div>
                </div>
                <div className="flex items-end">
                  <button
                    className={`px-3 py-1.5 rounded inline-flex items-center gap-2 text-sm ${canFilter && !loading ? 'bg-indigo-600 text-white hover:bg-indigo-700' : 'bg-gray-200 text-gray-500'} ${loading ? 'opacity-70 cursor-wait' : ''}`}
                    disabled={!canFilter || loading}
                    onClick={loadByFilters}
                  >
                    {loading && (
                      <svg className="animate-spin h-4 w-4 text-current" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                      </svg>
                    )}
                    <span>{loading ? 'Loading…' : 'Apply'}</span>
                  </button>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className={`bg-white rounded-lg shadow-sm border overflow-hidden mt-2 ${loading ? 'cursor-wait' : ''}`}>
          <div className="px-3 py-2 border-b bg-gray-50 flex items-center justify-between">
            <div className="text-sm text-gray-700">Attendance — Showing {visibleItems.length} record{visibleItems.length === 1 ? '' : 's'}</div>
            <div className="flex items-center gap-2">
              <div className="relative">
                <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400">🔎</span>
                <input className="pl-8 pr-2 py-1.5 rounded border w-56 text-sm" placeholder="Search name or email" value={searchQuery} onChange={e => setSearchQuery(e.target.value)} />
              </div>
              <div className="relative z-30" ref={exportMenuRef}>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 px-2.5 py-1.5 rounded-md border border-gray-300 bg-white text-gray-700 hover:bg-gray-50 shadow-sm disabled:opacity-50"
                  disabled={visibleItems.length === 0}
                  title="Export attendance report"
                  onClick={() => setIsExportOpen(v => !v)}
                >
                  <FileText className="w-4 h-4" />
                  <span className="text-sm">Export</span>
                </button>
                {isExportOpen && (
                  <div className="absolute right-0 mt-1 w-36 bg-white border rounded shadow-lg z-50">
                    <button className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-2" onClick={() => { setIsExportOpen(false); exportPdf() }} disabled={visibleItems.length === 0}>
                      <FileText className="w-4 h-4" /> PDF
                    </button>
                    <button className="w-full text-left px-3 py-2 text-sm hover:bg-gray-50 flex items-center gap-2" onClick={() => { setIsExportOpen(false); exportExcel() }} disabled={visibleItems.length === 0}>
                      <FileSpreadsheet className="w-4 h-4" /> Excel
                    </button>
                  </div>
                )}
              </div>
            </div>
          </div>
          {!loading && items.length === 0 ? (
            <div className="p-12 text-center">
              <div className="text-gray-400 mb-2">
                <svg className="w-12 h-12 mx-auto" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v10a2 2 0 002 2h8a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
                </svg>
              </div>
              <h3 className="text-lg font-medium text-gray-900 mb-1">No Attendance Records</h3>
              <p className="text-gray-500">Enter a class name and click Load to view results.</p>
            </div>
          ) : !loading ? (
            <div className="p-0">
              <div className="overflow-x-auto">
                <div className="overflow-y-auto border rounded" style={{ maxHeight: 'calc(100vh - 330px)' }}>
                  <table className="min-w-full divide-y divide-gray-200">
                    <thead className="bg-gray-50 sticky top-0 z-10">
                      <tr>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('student_name')}
                        >
                          <div className="flex items-center gap-1">
                            Student Name
                            {sortField === 'student_name' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('email')}
                        >
                          <div className="flex items-center gap-1">
                            Email
                            {sortField === 'email' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('course')}
                        >
                          <div className="flex items-center gap-1">
                            Course
                            {sortField === 'course' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('section')}
                        >
                          <div className="flex items-center gap-1">
                            Section
                            {sortField === 'section' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('schedule')}
                        >
                          <div className="flex items-center gap-1">
                            Schedule
                            {sortField === 'schedule' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('status')}
                        >
                          <div className="flex items-center gap-1">
                            Status
                            {sortField === 'status' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                        <th 
                          className="px-6 py-3 text-left text-xs font-medium text-gray-500 uppercase tracking-wider cursor-pointer hover:bg-gray-100 select-none"
                          onClick={() => handleSort('presence')}
                        >
                          <div className="flex items-center gap-1">
                            Presence %
                            {sortField === 'presence' && (
                              sortDirection === 'asc' ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />
                            )}
                          </div>
                        </th>
                      </tr>
                    </thead>
                    <tbody className="bg-white divide-y divide-gray-200">
                      {loading && (
                        <tr>
                          <td colSpan={7} className="px-6 py-6 text-center text-sm text-gray-600">
                            <div className="inline-flex items-center gap-2">
                              <svg className="animate-spin h-4 w-4 text-gray-600" viewBox="0 0 24 24">
                                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                              </svg>
                              Loading attendance records...
                            </div>
                          </td>
                        </tr>
                      )}
                      {!loading && visibleItems.map((r) => {
                        const schedule = (() => {
                          const cid = String(r.class_id || '')
                          // Preferred: last 9 chars like HHMM-HHMM or HHMM_HHMM
                          const tail = cid.slice(-9)
                          let m = tail.match(/^(\d{2})(\d{2})[-_](\d{2})(\d{2})$/)
                          if (m) {
                            const [, h1, m1, h2, m2] = m
                            return `${h1}:${m1} – ${h2}:${m2}`
                          }
                          // Fallback: find first two HH:MM occurrences anywhere
                          m = cid.match(/(\d{2}:\d{2}).*?(\d{2}:\d{2})/)
                          if (m) {
                            return `${m[1]} – ${m[2]}`
                          }
                          return '-'
                        })()
                        return (
                        <tr key={r.id} className="hover:bg-gray-50">
                          <td className="px-6 py-4 whitespace-nowrap">
                        <div className="text-sm font-medium text-gray-900">{r.student?.full_name ?? '-'}</div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-700 break-all">
                        {r.student?.email ?? '-'}
                      </td>
                      {/* Code column removed per request */}
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-700">{r.student?.course_code ?? '-'}</td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-700">{r.student?.section ?? '-'}</td>
                          <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-700 font-mono">{schedule}</td>
                      <td className="px-6 py-4 whitespace-nowrap">
                        <div className="flex items-center gap-2 text-sm">
                          {r.status === 'PRESENT' ? (
                            <svg className="w-5 h-5 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                            </svg>
                          ) : r.status === 'UNWATCHED' ? (
                            <svg className="w-5 h-5 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                            </svg>
                          ) : (
                            <svg className="w-5 h-5 text-red-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                          )}
                          <span className={r.status === 'PRESENT' ? 'text-green-700' : r.status === 'UNWATCHED' ? 'text-gray-500' : 'text-red-700'} title={r.status === 'UNWATCHED' ? 'The camera could not be watched during this class (no frames or recognition down); nobody was marked absent.' : undefined}>
                            {r.status === 'PRESENT' ? 'Present' : r.status === 'UNWATCHED' ? 'Not watched' : 'Absent'}
                          </span>
                        </div>
                      </td>
                      <td className="px-6 py-4 whitespace-nowrap text-sm text-gray-700">
                        <div className="flex items-center gap-3">
                          <span className="font-medium">{Math.round(r.ratio * 100)}%</span>
                          <div className="w-28 h-2 bg-gray-200 rounded">
                            <div
                              className={`h-2 rounded ${r.status === 'PRESENT' ? 'bg-gray-800' : 'bg-gray-500'}`}
                              style={{ width: `${Math.max(0, Math.min(100, Math.round(r.ratio * 100)))}%` }}
                            />
                          </div>
                        </div>
                      </td>
                        </tr>
                      )})}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </> }
    </Layout>
  )
}
