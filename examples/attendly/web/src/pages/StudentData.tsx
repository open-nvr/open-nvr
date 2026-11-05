import { useEffect, useState, useRef } from 'react'
import axios from 'axios'
import Layout from '../components/Layout'
import { Pencil, Trash2 } from 'lucide-react'
import { Users } from 'lucide-react'
import { config } from '../config/environment'
import { authService } from '../services/auth'
import ManageCourses from './ManageCourses'
import { useToast } from '../components/ToastProvider'
import { useConfirm } from '../components/ConfirmProvider'
import { messages } from '../messages'
import ResidenceFields, { type Residence } from '../components/ResidenceFields'

const API = config.API_BASE_URL

interface CollectionInfo {
  collection_id: string
  face_count: number
  face_model_version: string
  creation_timestamp: string
  collection_arn: string
}

interface FaceInfo {
  face_id: string
  external_image_id: string
  student_code: string
  student_name: string
  student_class: string
  confidence: number
  image_id: string
  bounding_box: any
}

interface StudentFaceStats {
  student_code: string
  face_count: number
  student_name?: string
  student_class?: string
  course_code?: string
  section?: string
  max_confidence?: number
  student_email?: string | null
  residence?: string | null
  hostel_block?: string | null
  enrolments?: Array<{ course_code: string; section: string }>
}

export default function StudentData() {
  const [collectionInfo, setCollectionInfo] = useState<CollectionInfo | null>(null)
  const [stats, setStats] = useState<any>(null)
  const [loading, setLoading] = useState(true)
  const [statsLoading, setStatsLoading] = useState(false)
  const [error, setError] = useState<string>('')
  const [studentPage, setStudentPage] = useState(1)
  const pageSize = 50
  const [processingTime, setProcessingTime] = useState<string>('')
  const [engineWorkers, setEngineWorkers] = useState<any[]>([])
  const [studentSearch, setStudentSearch] = useState('')
  const [courseNameMap, setCourseNameMap] = useState<Record<string, string>>({})
  const { showToast } = useToast()
  const { confirm } = useConfirm()
  
  // Request deduplication to prevent duplicate calls
  const requestCache = useRef<Record<string, Promise<any>>>({})
  
  const isRequestInProgress = (cacheKey: string) => {
    return requestCache.current[cacheKey] !== undefined
  }

  // Fast initial load - only collection info
  const loadDashboardData = async () => {
    const cacheKey = 'dashboard'
    if (isRequestInProgress(cacheKey)) {
      return requestCache.current[cacheKey]
    }
    
    const request = (async () => {
      try {
        const startTime = Date.now()
        const res = await axios.get(`${API}/registry/dashboard`)
        setCollectionInfo(res.data.collection)
        const endTime = Date.now()
        setProcessingTime(`${(endTime - startTime)}ms`)
      } catch (error) {
        console.error('Error loading dashboard data:', error)
        setError('Failed to load dashboard data')
      } finally {
        delete requestCache.current[cacheKey]
      }
    })()
    
    requestCache.current[cacheKey] = request
    return request
  }

  const loadEngineStatus = async () => {
    const cacheKey = 'engine_status'
    if (isRequestInProgress(cacheKey)) {
      return requestCache.current[cacheKey]
    }
    
    const request = (async () => {
      try {
        const res = await axios.get(`${API}/registry/engine_status`)
        setEngineWorkers(Array.isArray(res.data?.workers) ? res.data.workers : [])
      } catch (e) {
        // ignore
      } finally {
        delete requestCache.current[cacheKey]
      }
    })()
    
    requestCache.current[cacheKey] = request
    return request
  }

  // Load detailed stats on demand
  const loadStats = async () => {
    if (statsLoading) return
    
    const cacheKey = 'stats'
    if (isRequestInProgress(cacheKey)) {
      return requestCache.current[cacheKey]
    }
    
    setStatsLoading(true)
    const request = (async () => {
      try {
        const res = await axios.get(`${API}/registry/stats?include_face_ids=false&include_student_info=true&include_students=true`)
        setStats(res.data)
        setProcessingTime(res.data.statistics?.processing_time_seconds ? `${res.data.statistics.processing_time_seconds}s` : '')
      } catch (error) {
        console.error('Error loading stats:', error)
        setError('Failed to load stats')
      } finally {
        setStatsLoading(false)
        delete requestCache.current[cacheKey]
      }
    })()
    
    requestCache.current[cacheKey] = request
    return request
  }

  const loadPlannerCourses = async () => {
    const cacheKey = 'courses'
    if (isRequestInProgress(cacheKey)) {
      return requestCache.current[cacheKey]
    }
    
    const request = (async () => {
      try {
        const res = await axios.get(`${API}/planner/courses`)
        const list = Array.isArray(res.data) ? res.data : []
        const map: Record<string, string> = {}
        for (const c of list) {
          const code = String(c.code)
          const name = c.name ? String(c.name) : ''
          if (code) map[code] = name
        }
        setCourseNameMap(map)
      } catch {
        setCourseNameMap({})
      } finally {
        delete requestCache.current[cacheKey]
      }
    })()
    
    requestCache.current[cacheKey] = request
    return request
  }

  // Removed faces list pagination; using merged student table instead

  const loadStudentFaces = async (studentCode: string) => {
    try {
      const res = await axios.get(`${API}/registry/student/${studentCode}/faces`)
      const s = res.data
      const faceIds = Array.isArray(s.faces) ? s.faces.map((f: any) => String(f.face_id)) : []
      showToast('info', messages.registry.studentFacesInfo(String(s.student_name || studentCode), Number(s.total_faces || faceIds.length), faceIds))
    } catch (error) {
      console.error('Error loading student faces:', error)
      showToast('error', messages.registry.failedLoadStudentFaces)
    }
  }

  const [editOpen, setEditOpen] = useState(false)
  const [editing, setEditing] = useState<StudentFaceStats | null>(null)
  const [editName, setEditName] = useState('')
  const [editEmail, setEditEmail] = useState('')
  const [editCourse, setEditCourse] = useState('')
  const [editSection, setEditSection] = useState('')
  const [editResidence, setEditResidence] = useState<Residence>('day_scholar')
  const [editHostelBlock, setEditHostelBlock] = useState('')
  const [editEnrolments, setEditEnrolments] = useState<Array<{ course_code: string; section: string }>>([])
  const [newEnrolCourse, setNewEnrolCourse] = useState('')
  const [newEnrolSection, setNewEnrolSection] = useState('')
  const [courseOptions, setCourseOptions] = useState<Array<{ code: string; name?: string }>>([])
  const [sectionOptions, setSectionOptions] = useState<string[]>([])
  const [sectionsLoading, setSectionsLoading] = useState(false)
  const [savingEdit, setSavingEdit] = useState(false)
  const [editError, setEditError] = useState('')
  const selectedCourseLabel = (() => {
    const found = courseOptions.find(c => c.code === editCourse)
    return found ? `${found.code}${found.name ? ` — ${found.name}` : ''}` : ''
  })()
  const formatCourseLabel = (code: string, name?: string) => {
    const full = `${code}${name ? ` — ${name}` : ''}`
    const limit = 36
    return full.length > limit ? `${full.slice(0, limit)}…` : full
  }

  const openEdit = (s: StudentFaceStats) => {
    setEditing(s)
    setEditName(s.student_name || '')
    setEditEmail(s.student_email || '')
    setEditCourse(s.course_code || '')
    setEditSection(s.section || '')
    setEditResidence(s.residence === 'hostler' ? 'hostler' : 'day_scholar')
    setEditHostelBlock(s.hostel_block || '')
    setEditEnrolments(Array.isArray(s.enrolments) ? s.enrolments.map(e => ({ course_code: String(e.course_code), section: String(e.section) })) : [])
    setNewEnrolCourse(''); setNewEnrolSection('')
    setEditError('')
    setEditOpen(true)
    // Load courses if not loaded yet
    if (courseOptions.length === 0) {
      ;(async () => {
        try {
          const res = await axios.get(`${API}/planner/courses`)
          const list = Array.isArray(res.data) ? res.data : []
          setCourseOptions(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined })))
        } catch {
          setCourseOptions([])
        }
      })()
    }
    // Load sections for existing course
    if (s.course_code) {
      ;(async (code: string) => {
        try {
          setSectionsLoading(true)
          const res = await axios.get(`${API}/courses/${encodeURIComponent(code)}/sections`)
          const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((x: any) => String(x)) : []
          setSectionOptions(arr)
        } catch {
          setSectionOptions([])
        } finally {
          setSectionsLoading(false)
        }
      })(s.course_code)
    } else {
      setSectionOptions([])
    }
  }

  const saveEdit = async () => {
    if (!editing) return
    try {
      setSavingEdit(true)
      setEditError('')
      await axios.put(`${API}/students/${encodeURIComponent(editing.student_code)}`, {
        full_name: editName,
        email: editEmail || null,
        course_code: editCourse || null,
        section: editSection || null,
        residence: editResidence,
        hostel_block: editResidence === 'hostler' ? editHostelBlock.trim() : '',
        enrolments: editEnrolments,
      })
      // Refresh stats to reflect changes
      await loadStats()
      setEditOpen(false)
    } catch (err: any) {
      const msg = err?.response?.data?.message || err?.response?.data?.detail || err?.message
      setEditError(typeof msg === 'string' ? msg : 'Failed to save changes')
    } finally {
      setSavingEdit(false)
    }
  }

  const deleteStudentFaces = async (studentCode: string) => {
    const user = authService.getUser()
      if (user && user.is_admin === false) {
        setError("This feature is disabled during trial use.")
        return
      }
    const ok = await confirm({ description: messages.registry.confirmDeleteStudentFaces(studentCode), confirmText: messages.common.delete })
    if (!ok) return

    try {
      const res = await axios.post(`${API}/registry/student/${studentCode}/soft_delete`)
      const deletedFaces = Number(res.data?.deleted_faces || 0)
      showToast('success', messages.registry.deletedStudentFaces(deletedFaces, studentCode))
      // Refresh data
      await loadDashboardData()
      if (stats) await loadStats()
    } catch (error) {
      console.error('Error soft deleting student:', error)
      showToast('error', messages.registry.failedDeleteStudentFaces)
    }
  }

  // Fast initial load - load all data at once
  const loadInitialData = async () => {
    setLoading(true)
    setError('')
    try {
      // Load all data in parallel for better performance
      await Promise.all([
        loadDashboardData(),
        loadStats(),
        loadEngineStatus(),
        loadPlannerCourses()
      ])
    } catch (error) {
      console.error('Error loading initial data:', error)
      setError('Failed to load data')
    } finally {
      setLoading(false)
    }
  }

  // Load all data on mount
  useEffect(() => {
    loadInitialData()
  }, [])

  // Auto-refresh removed - student data doesn't change frequently enough to warrant automatic updates

  // Derived pagination for students
  const students: StudentFaceStats[] = Array.isArray(stats?.students_with_faces) ? stats.students_with_faces : []
  const sortedStudents = [...students].sort((a: StudentFaceStats, b: StudentFaceStats) => {
    const aNum = parseInt((a.student_code?.match(/\d+/)?.[0]) || '0', 10)
    const bNum = parseInt((b.student_code?.match(/\d+/)?.[0]) || '0', 10)
    return aNum - bNum
  })
  const normalizedQuery = studentSearch.trim().toLowerCase()
  const filteredStudents = normalizedQuery
    ? sortedStudents.filter((s) => {
        const courseName = s.course_code ? courseNameMap[s.course_code] || '' : ''
        const fields = [
          s.student_code,
          s.student_name,
          s.student_class,
          s.course_code,
          courseName, // Include full course name
          s.section,
          s.student_email,
        ]
          .filter(Boolean)
          .map((v) => String(v).toLowerCase())
        
        // Debug logging to see what's being searched
        if (normalizedQuery.includes('section') || normalizedQuery.includes('a') || normalizedQuery.includes('b')) {
          console.log('Search query:', normalizedQuery)
          console.log('Student:', s.student_name, 'Section:', s.section)
          console.log('Searchable fields:', fields)
        }
        
        return fields.some((f) => f.includes(normalizedQuery))
      })
    : sortedStudents

  const totalStudentPages = Math.max(1, Math.ceil(filteredStudents.length / pageSize))
  const paginatedStudents = filteredStudents.slice((studentPage - 1) * pageSize, (studentPage - 1) * pageSize + pageSize)

  useEffect(() => {
    // Reset to first page when search changes
    setStudentPage(1)
  }, [studentSearch])

  // Do not early-return while loading; we'll show skeletons in the table area instead

  if (error) {
    return (
      <Layout title="Registered Students">
        <div className="bg-red-50 border border-red-200 rounded-lg p-4">
          <div className="text-red-800">Error: {error}</div>
          <button 
            onClick={() => loadInitialData()}
            className="mt-2 px-3 py-1 bg-red-100 text-red-800 rounded hover:bg-red-200"
          >
            Retry
          </button>
        </div>
      </Layout>
    )
  }

  return (
    <Layout title="Registered Students">
      <div className="space-y-2">
        <div className="bg-white border rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900">
            <Users className="w-5 h-5 text-gray-500" />
            <span>Students</span>
            <span className="text-sm font-normal text-gray-500">— Manage registered students</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative w-72 sm:w-80 md:w-96 lg:w-[480px]">
              <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400">🔎</span>
              <input
                className="pl-8 pr-3 py-2 rounded border w-full"
                placeholder="Search name, email, course"
                value={studentSearch}
                onChange={(e) => setStudentSearch(e.target.value)}
              />
            </div>
          </div>
        </div>

        {/* Collection Info */}
        {/* {collectionInfo && (
          <div className="bg-white rounded-lg shadow-sm border p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">Collection Information</h3>
            <div className="grid gap-4 md:grid-cols-2">
              <div>
                <label className="block text-sm font-medium text-gray-700">Collection ID</label>
                <div className="mt-1 text-sm text-gray-900">{collectionInfo.collection_id}</div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Total Faces</label>
                <div className="mt-1 text-2xl font-bold text-blue-600">{collectionInfo.face_count}</div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Face Model Version</label>
                <div className="mt-1 text-sm text-gray-900">{collectionInfo.face_model_version}</div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700">Created</label>
                <div className="mt-1 text-sm text-gray-900">
                  {new Date(collectionInfo.creation_timestamp).toLocaleString('en-IN', { timeZone: 'Asia/Kolkata' })}
                </div>
              </div>
            </div>
          </div>
        )} */}

        {/* Statistics
        {statsLoading ? (
          <div className="bg-white rounded-lg shadow-sm border p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">Statistics</h3>
            <div className="flex items-center justify-center py-8">
              <div className="text-gray-500">⏳ Loading detailed statistics...</div>
            </div>
          </div>
        ) : stats ? (
          <div className="bg-white rounded-lg shadow-sm border p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-4">
              Statistics 
              {stats.statistics?.processing_time_seconds && (
                <span className="text-sm text-gray-500 ml-2">
                  (loaded in {stats.statistics.processing_time_seconds}s)
                </span>
              )}
            </h3>
            <div className="grid gap-4 md:grid-cols-3">
              <div className="bg-blue-50 p-4 rounded-lg">
                <div className="text-2xl font-bold text-blue-600">{stats.statistics.total_students_with_faces}</div>
                <div className="text-sm text-gray-600">Students with Faces</div>
              </div>
              <div className="bg-green-50 p-4 rounded-lg">
                <div className="text-2xl font-bold text-green-600">{stats.statistics.total_faces}</div>
                <div className="text-sm text-gray-600">Total Faces</div>
              </div>
              <div className="bg-purple-50 p-4 rounded-lg">
                <div className="text-2xl font-bold text-purple-600">
                  {stats.statistics.average_faces_per_student}
                </div>
                <div className="text-sm text-gray-600">Avg Faces/Student</div>
              </div>
            </div>
          </div>
        ) : null} */}

        {/* Merged table: Students with Registered Faces */}
        <div className="bg-white rounded-lg shadow-sm border overflow-hidden" aria-busy={statsLoading || loading}>
            {/* Sub-toolbar: count only (search moved to header) */}
            <div className="px-3 py-2 border-b bg-gray-50 flex items-center justify-between">
              <div className="text-xs text-gray-600">Showing {paginatedStudents.length} of {filteredStudents.length} students</div>
              <div />
            </div>
            <div className="overflow-auto" style={{ maxHeight: 'calc(100vh - 290px)' }}>
              <table className="min-w-full divide-y divide-gray-200 text-xs leading-8">
                <thead className="bg-gray-50 sticky top-0 z-10">
                  <tr>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Name</th>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Email</th>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Course</th>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Section</th>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Residence</th>
                    <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Image captured</th>
                                        <th className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">Action</th>
                  </tr>
                </thead>
                <tbody className="bg-white divide-y divide-gray-200">
                  {(!stats || statsLoading) && (
                    <tr>
                      <td colSpan={7} className="px-3 py-6 text-center text-sm text-gray-600">
                        <div className="inline-flex items-center gap-2">
                          <svg className="animate-spin h-4 w-4 text-gray-600" viewBox="0 0 24 24">
                            <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                            <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                          </svg>
                          Loading students...
                        </div>
                      </td>
                    </tr>
                  )}
                  {stats && !statsLoading && paginatedStudents.length > 0 && paginatedStudents.map((student) => (
                    <tr key={student.student_code} className="hover:bg-gray-50">
                      <td className="px-3 py-1 whitespace-nowrap text-gray-900">{student.student_name || 'Unknown'}</td>
                      <td className="px-3 py-1 whitespace-nowrap text-gray-500 break-all">{student.student_email || '-'}</td>
                      <td className="px-3 py-1 whitespace-nowrap text-gray-500">
                        {(() => {
                          const code = student.course_code || '-'
                          const name = code && courseNameMap[code] ? ` — ${courseNameMap[code]}` : ''
                          const full = `${code}${name}`
                          const short = full.length > 36 ? `${full.slice(0,36)}…` : full
                          return <span title={full} className="inline-block max-w-[240px] truncate align-bottom">{short}</span>
                        })()}
                      </td>
                      <td className="px-3 py-1 whitespace-nowrap text-gray-500">{student.section || '-'}{Array.isArray(student.enrolments) && student.enrolments.length > 0 && <span className="ml-1 text-[10px] text-indigo-700" title={student.enrolments.map(e => `${e.course_code}/${e.section}`).join(', ')}>+{student.enrolments.length}</span>}</td>
                      <td className="px-3 py-1 whitespace-nowrap text-gray-500">{student.residence === 'hostler' ? `Hostler${student.hostel_block ? ` · ${student.hostel_block}` : ''}` : 'Day scholar'}</td>
                      <td className="px-3 py-1 whitespace-nowrap text-gray-900">{student.face_count}</td>
                                            <td className="px-3 py-1 whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          <button
                            onClick={() => openEdit(student)}
                            className="inline-flex items-center justify-center p-2 rounded-md bg-gray-100 hover:bg-gray-200"
                            title="Edit"
                            aria-label="Edit"
                          >
                            <Pencil className="w-4 h-4" />
                          </button>
                          <button
                            onClick={() => deleteStudentFaces(student.student_code)}
                            className="inline-flex items-center justify-center p-2 rounded-md bg-red-50 text-red-700 hover:bg-red-100"
                            title="Delete"
                            aria-label="Delete"
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {stats && !statsLoading && paginatedStudents.length === 0 && (
                    <tr>
                      <td colSpan={7} className="px-6 py-4 text-center text-gray-500">
                        <div className="flex items-center justify-center gap-2">
                          <svg
                            xmlns="http://www.w3.org/2000/svg"
                            viewBox="0 0 24 24"
                            fill="none"
                            stroke="currentColor"
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            className="w-4 h-4 text-gray-400"
                            aria-hidden="true"
                          >
                            <circle cx="12" cy="8" r="4" />
                            <path d="M6 20a6 6 0 1 1 12 0" />
                          </svg>
                          <span>{studentSearch ? 'No matching students' : 'No students registered yet'}</span>
                        </div>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
            {/* Pagination */}
            <div className="flex items-center justify-between p-2 text-xs">
              <div className="text-gray-700">Page {studentPage} of {totalStudentPages}</div>
              <div className="flex gap-2">
                <button
                  onClick={() => setStudentPage((p) => Math.max(1, p - 1))}
                  disabled={studentPage <= 1}
                  className="px-2 py-1 bg-gray-100 text-gray-700 rounded hover:bg-gray-200 disabled:opacity-50"
                >
                  Previous
                </button>
                <button
                  onClick={() => setStudentPage((p) => Math.min(totalStudentPages, p + 1))}
                  disabled={studentPage >= totalStudentPages}
                  className="px-2 py-1 bg-gray-100 text-gray-700 rounded hover:bg-gray-200 disabled:opacity-50"
                >
                  Next
                </button>
              </div>
            </div>
          </div>

        {/* Edit Student Modal */}
        {editOpen && (
          <div className="fixed inset-0 z-[100] flex items-end sm:items-center justify-center bg-black/40 p-0 sm:p-4">
            <div className="w-full sm:max-w-md bg-white rounded-t-xl sm:rounded-xl shadow-lg border">
              <div className="px-4 py-3 border-b bg-gray-50 rounded-t-xl flex items-center justify-between">
                <h3 className="font-semibold text-base">Edit Student</h3>
                <button onClick={() => setEditOpen(false)} className="text-gray-500 hover:text-gray-700">✖</button>
              </div>
              <div className="p-4 space-y-3">
                {editError && (
                  <div className="text-sm text-red-600 bg-red-50 border border-red-200 rounded px-2 py-1">{editError}</div>
                )}
                {/* Student Code removed from edit form per request */}
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Full Name</label>
                  <input value={editName} onChange={(e) => setEditName(e.target.value)} className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Email</label>
                  <input type="email" value={editEmail} onChange={(e) => setEditEmail(e.target.value)} className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500" />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Course</label>
                    <select
                      value={editCourse}
                      onChange={async (e) => {
                        const val = e.target.value
                        setEditCourse(val)
                        setEditSection('')
                        if (!val) { setSectionOptions([]); return }
                        try {
                          setSectionsLoading(true)
                          const res = await axios.get(`${API}/courses/${encodeURIComponent(val)}/sections`)
                          const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((x: any) => String(x)) : []
                          setSectionOptions(arr)
                        } catch {
                          setSectionOptions([])
                        } finally {
                          setSectionsLoading(false)
                        }
                      }}
                      onClick={async (e) => {
                        // Defensive: ensure courses loaded on first open
                        if (courseOptions.length === 0) {
                          try {
                            const res = await axios.get(`${API}/planner/courses`)
                            const list = Array.isArray(res.data) ? res.data : []
                            setCourseOptions(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined })))
                          } catch {
                            setCourseOptions([])
                          }
                        }
                      }}
                      className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500 max-w-[260px] truncate"
                      title={selectedCourseLabel}
                    >
                      <option value="">Select course</option>
                      {courseOptions.map(opt => {
                        const full = `${opt.code}${opt.name ? ` — ${opt.name}` : ''}`
                        const short = formatCourseLabel(opt.code, opt.name)
                        return (
                          <option key={opt.code} value={opt.code} title={full}>{short}</option>
                        )
                      })}
                    </select>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-gray-700 mb-1">Section</label>
                    <select
                      value={editSection}
                      onChange={(e) => setEditSection(e.target.value)}
                      onFocus={async () => {
                        if (editCourse && sectionOptions.length === 0 && !sectionsLoading) {
                          try {
                            setSectionsLoading(true)
                            const res = await axios.get(`${API}/courses/${encodeURIComponent(editCourse)}/sections`)
                            const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((x: any) => String(x)) : []
                            setSectionOptions(arr)
                          } catch {
                            setSectionOptions([])
                          } finally {
                            setSectionsLoading(false)
                          }
                        }
                      }}
                      disabled={!editCourse}
                      className="w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                    >
                      <option value="">{sectionsLoading ? 'Loading sections…' : 'Select section'}</option>
                      {!sectionsLoading && sectionOptions.map(sec => (
                        <option key={sec} value={sec}>{sec}</option>
                      ))}
                    </select>
                    {editCourse && !sectionsLoading && sectionOptions.length === 0 && (
                      <div className="mt-1 text-[11px] text-gray-500">No sections found for selected course.</div>
                    )}
                  </div>
                </div>
              </div>
              <div className="px-4 pb-4 grid grid-cols-2 gap-3">
                <ResidenceFields compact residence={editResidence} hostelBlock={editHostelBlock} onResidence={setEditResidence} onHostelBlock={setEditHostelBlock} />
              </div>
              <div className="px-4 pb-4">
                <label className="block text-sm font-medium text-gray-700 mb-1">Also enrolled in <span className="font-normal text-gray-500">(a class of any of these marks the student)</span></label>
                {editEnrolments.length > 0 && (
                  <div className="flex flex-wrap gap-1 mb-2">
                    {editEnrolments.map((e, i) => (
                      <span key={i} className="inline-flex items-center gap-1 text-xs bg-indigo-50 text-indigo-800 px-2 py-0.5 rounded">
                        {e.course_code} / {e.section}
                        <button type="button" className="text-indigo-500 hover:text-indigo-800" onClick={() => setEditEnrolments(editEnrolments.filter((_, j) => j !== i))} aria-label="Remove">×</button>
                      </span>
                    ))}
                  </div>
                )}
                <div className="flex gap-2">
                  <select className="flex-1 rounded-md border border-gray-300 px-2 py-1.5 text-sm" value={newEnrolCourse} onChange={e => setNewEnrolCourse(e.target.value)}>
                    <option value="">Course</option>
                    {courseOptions.map(c => <option key={c.code} value={c.code}>{c.code}{c.name ? ` — ${c.name}` : ''}</option>)}
                  </select>
                  <input className="w-24 rounded-md border border-gray-300 px-2 py-1.5 text-sm" placeholder="Section" value={newEnrolSection} onChange={e => setNewEnrolSection(e.target.value)} />
                  <button type="button" className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200 text-sm disabled:opacity-50" disabled={!newEnrolCourse || !newEnrolSection.trim()}
                    onClick={() => {
                      const pair = { course_code: newEnrolCourse, section: newEnrolSection.trim() }
                      if (!editEnrolments.some(e => e.course_code === pair.course_code && e.section === pair.section)) setEditEnrolments([...editEnrolments, pair])
                      setNewEnrolCourse(''); setNewEnrolSection('')
                    }}>Add</button>
                </div>
              </div>
              <div className="px-4 py-3 border-t bg-gray-50 flex justify-end gap-2">
                <button onClick={() => setEditOpen(false)} className="px-4 py-2 rounded-md bg-gray-100 text-gray-700 hover:bg-gray-200">Cancel</button>
                <button onClick={saveEdit} disabled={savingEdit} className="px-4 py-2 rounded-md bg-blue-600 text-white hover:bg-blue-700 disabled:opacity-50">{savingEdit ? 'Saving…' : 'Save'}</button>
              </div>
            </div>
          </div>
        )}

        {/* Engine status */}
        {/* <div className="bg-white rounded-lg shadow-sm border overflow-hidden">
          <div className="px-4 py-3 border-b">
            <div className="text-sm font-medium text-gray-700">Engine Workers</div>
          </div>
          <div className="p-4">
            {engineWorkers.length === 0 ? (
              <div className="text-sm text-gray-500">No active workers</div>
            ) : (
              <div className="space-y-2">
                {engineWorkers.map((w, idx) => (
                  <div key={idx} className="text-sm grid grid-cols-5 gap-2">
                    <div className="font-mono">{w.stream_id}</div>
                    <div>{w.name}</div>
                    <div className="font-mono">{w.class_id || '-'}</div>
                    <div className="font-mono">{w.start_time || '-'}</div>
                    <div className="font-mono">{w.end_time || '-'}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div> */}
      </div>
    </Layout>
  )
}
