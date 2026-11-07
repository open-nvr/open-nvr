import { useEffect, useMemo, useState } from 'react'
import axios from 'axios'
import { config } from '../config/environment'
import Layout from '../components/Layout'
import { Plus, Pencil, Trash2, Clock, Camera, Building2, ListPlus, X } from 'lucide-react'
import { useToast } from '../components/ToastProvider'
import { useConfirm } from '../components/ConfirmProvider'
import { messages } from '../messages'

type DayName = 'Monday' | 'Tuesday' | 'Wednesday' | 'Thursday' | 'Friday' | 'Saturday'

type Course = {
  code: string
  students: number
  sections: number
}

type ScheduleEntry = {
  id?: string
  course: string
  section: string
  roomId: number
  start: string
  end: string
}

export default function Streams() {
  const API = config.API_BASE_URL
  const allDays: DayName[] = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']

  const getToday = (): DayName => {
    const jsDay = new Date().getDay() // 0-6; 0=Sunday
    const map: Record<number, DayName> = { 1: 'Monday', 2: 'Tuesday', 3: 'Wednesday', 4: 'Thursday', 5: 'Friday', 6: 'Saturday' }
    return map[jsDay] || 'Monday'
  }

  const [activeDay, setActiveDay] = useState<DayName>(getToday())

  // Courses start empty (admin can add)
  const [courses, setCourses] = useState<Course[]>([])

  // Rooms are OpenNVR cameras
  type Room = { id: number; name: string; cameraId: number | null; cameraName: string }
  type CameraOption = { id: number; name: string; handle: string }
  const [cameras, setCameras] = useState<CameraOption[]>([])
  const [rooms, setRooms] = useState<Room[]>([])

  // Schedules per day start empty
  const [schedules, setSchedules] = useState<Record<DayName, ScheduleEntry[]>>({
    Monday: [],
    Tuesday: [],
    Wednesday: [],
    Thursday: [],
    Friday: [],
    Saturday: []
  })

  // Add Course dialog state (global create-by-code only, rarely used now)
  const [isAddCourseOpen, setIsAddCourseOpen] = useState(false)
  const [newCourseCode, setNewCourseCode] = useState('')
  const [newCourseStudents, setNewCourseStudents] = useState('')
  const [newCourseSections, setNewCourseSections] = useState('')

  // Add/Edit Schedule dialog state
  const [isScheduleOpen, setIsScheduleOpen] = useState(false)
  const [editingIndex, setEditingIndex] = useState<number | null>(null)
  const [selectedCourse, setSelectedCourse] = useState('')
  const [selectedSection, setSelectedSection] = useState('')
  const [selectedRoomId, setSelectedRoomId] = useState<string>('')
  const [startTime, setStartTime] = useState('')
  const [endTime, setEndTime] = useState('')
  const [formError, setFormError] = useState<string | null>(null)

  // Rooms dialog state
  const [isAddRoomOpen, setIsAddRoomOpen] = useState(false)
  const [newRoomName, setNewRoomName] = useState('')
  const [newRoomCameraId, setNewRoomCameraId] = useState('')

  // Manage room courses dialog
  const [isManageRoomCoursesOpen, setIsManageRoomCoursesOpen] = useState(false)
  const [activeRoom, setActiveRoom] = useState<Room | null>(null)
  const [roomCourses, setRoomCourses] = useState<string[]>([])
  const [newRoomCourseCode, setNewRoomCourseCode] = useState('')

  // Quick Schedule dialog (top-level)
  const [isQuickScheduleOpen, setIsQuickScheduleOpen] = useState(false)
  const [qsRoomId, setQsRoomId] = useState<string>('')
  const [qsCourse, setQsCourse] = useState<string>('')
  const [qsSection, setQsSection] = useState<string>('')
  const [qsStart, setQsStart] = useState<string>('')
  const [qsEnd, setQsEnd] = useState<string>('')
  const [qsError, setQsError] = useState<string | null>(null)
  const [qsRoomCourses, setQsRoomCourses] = useState<string[]>([])
  const { showToast } = useToast()
  const { confirm } = useConfirm()

  // Dynamic sections fetched from backend per course
  const [courseSections, setCourseSections] = useState<Record<string, string[]>>({})

  const fetchSectionsForCourse = async (courseCode: string) => {
    if (!courseCode) return
    try {
      const res = await axios.get(`${API}/courses/${encodeURIComponent(courseCode)}/sections`)
      const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((s: any) => String(s)) : []
      setCourseSections(prev => ({ ...prev, [courseCode]: arr }))
    } catch {
      // leave as is on error
    }
  }

  const publishToday = async () => {
    const d = new Date()
    const yyyy = d.getFullYear()
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const iso = `${yyyy}-${mm}-${dd}`
    try {
      await axios.post(`${API}/planner/publish`, { from_date: iso, to_date: iso })
      showToast('success', messages.streams.publishTodaySuccess)
    } catch (e: any) {
      showToast('error', messages.streams.publishTodayFailed(String(e?.response?.data?.detail || e?.message)))
    }
  }

  const loadRooms = async () => {
    try {
      const res = await axios.get(`${API}/classrooms/`)
      const list = Array.isArray(res.data) ? res.data : []
      setRooms(list.map((s: any) => ({ id: Number(s.id), name: String(s.name), cameraId: s.camera_id == null ? null : Number(s.camera_id), cameraName: String(s.camera_name || '') })))
    } catch (e) {
      setRooms([])
    }
  }

  const loadCameras = async () => {
    try {
      const res = await axios.get(`${API}/cameras/`)
      const list = Array.isArray(res.data) ? res.data : []
      setCameras(list.map((c: any) => ({ id: Number(c.id), name: String(c.name), handle: String(c.handle || '') })))
    } catch (e) {
      setCameras([])
    }
  }

  const loadRoomCourses = async (roomId: number) => {
    try {
      const res = await axios.get(`${API}/streams/${roomId}/courses`)
      const arr = Array.isArray(res.data?.courses) ? res.data.courses : []
      setRoomCourses(arr.map((c: any) => String(c)))
    } catch (e) {
      setRoomCourses([])
    }
  }

  const loadRoomCoursesForScheduler = async (roomId: number) => {
    try {
      const res = await axios.get(`${API}/streams/${roomId}/courses`)
      const arr = Array.isArray(res.data?.courses) ? res.data.courses : []
      setQsRoomCourses(arr.map((c: any) => String(c)))
    } catch (e) {
      setQsRoomCourses([])
    }
  }

  const loadCourses = async () => {
    try {
      const res = await axios.get(`${API}/courses`)
      const list = Array.isArray(res.data) ? res.data : []
      setCourses(list.map((c: any) => ({ code: String(c.code), students: Number(c.students || 0), sections: Number(c.sections || 0) })))
    } catch (e) {
      setCourses([])
    }
  }

  const loadSchedulesForDay = async (day: DayName) => {
    try {
      const res = await axios.get(`${API}/planner/schedules`, { params: { day } })
      const list = Array.isArray(res.data) ? res.data : []
      const mapped: ScheduleEntry[] = list.map((it: any) => ({
        id: String(it.id),
        course: String(it.course),
        section: String(it.section),
        roomId: Number(it.room_id),
        start: String(it.start),
        end: String(it.end),
      }))
      setSchedules(prev => ({ ...prev, [day]: mapped }))
    } catch (e) {
      setSchedules(prev => ({ ...prev, [day]: [] }))
    }
  }

  useEffect(() => {
    loadRooms()
    loadCameras()
    loadCourses()
    loadSchedulesForDay(activeDay)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    loadSchedulesForDay(activeDay)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeDay])

  const saveNewRoom = async () => {
    const name = newRoomName.trim()
    const cameraId = Number(newRoomCameraId || '0')
    if (!name) { showToast('error', messages.streams.roomNameRequired); return }
    if (!cameraId) { showToast('error', messages.streams.cameraRequired); return }
    try {
      const res = await axios.post(`${API}/classrooms/`, { name, camera_id: cameraId })
      const s = res.data
      setRooms(prev => [...prev, { id: Number(s.id), name: String(s.name), cameraId: Number(s.camera_id), cameraName: String(s.camera_name || '') }])
    } catch (e: any) {
      showToast('error', messages.streams.failedAddRoom(String(e?.response?.data?.detail || e?.message)))
      return
    }
    setIsAddRoomOpen(false)
    setNewRoomName('')
    setNewRoomCameraId('')
  }

  const openManageCourses = async (room: Room) => {
    setActiveRoom(room)
    setNewRoomCourseCode('')
    await loadRoomCourses(room.id)
    setIsManageRoomCoursesOpen(true)
  }

  const addCourseToRoom = async () => {
    if (!activeRoom) return
    const code = newRoomCourseCode.trim()
    if (!code) { showToast('error', messages.streams.courseCodeRequired); return }
    try {
      // Ensure course exists globally (optional, best-effort)
      try { await axios.post(`${API}/courses`, { code }) } catch {}
      await axios.post(`${API}/streams/${activeRoom.id}/courses`, null, { params: { course_code: code } })
      await loadRoomCourses(activeRoom.id)
      setNewRoomCourseCode('')
    } catch (e: any) {
      showToast('error', messages.streams.failedAddCourse(String(e?.response?.data?.detail || e?.message)))
    }
  }

  const removeCourseFromRoom = async (code: string) => {
    if (!activeRoom) return
    try {
      await axios.delete(`${API}/streams/${activeRoom.id}/courses/${encodeURIComponent(code)}`)
      await loadRoomCourses(activeRoom.id)
    } catch (e: any) {
      showToast('error', messages.streams.failedRemoveCourse(String(e?.response?.data?.detail || e?.message)))
    }
  }

  const openQuickSchedule = () => {
    setQsRoomId('')
    setQsCourse('')
    setQsSection('')
    setQsStart('')
    setQsEnd('')
    setQsError(null)
    setQsRoomCourses([])
    setIsQuickScheduleOpen(true)
  }

  const sectionsForCourse = (courseCode: string): string[] => {
    const dyn = courseSections[courseCode]
    if (dyn && dyn.length > 0) return dyn
    const course = courses.find(c => c.code === courseCode)
    const count = course?.sections ?? 0
    const arr: string[] = []
    for (let i = 0; i < count; i++) arr.push(String.fromCharCode(65 + i))
    return arr
  }

  const saveQuickSchedule = async () => {
    const rid = Number(qsRoomId || '0')
    const entry: ScheduleEntry = { course: qsCourse, section: qsSection, roomId: rid, start: qsStart, end: qsEnd }
    const err = validateSchedule(entry)
    if (err) { setQsError(err); return }
    try {
      await axios.post(`${API}/planner/schedules`, {
        day_of_week: activeDay,
        course: entry.course,
        section: entry.section,
        room_id: entry.roomId,
        start: entry.start,
        end: entry.end,
        active: true,
      })
      await loadSchedulesForDay(activeDay)
      setIsQuickScheduleOpen(false)
    } catch (e: any) {
      setQsError(String(e?.response?.data?.detail || e?.message || 'Failed to schedule class'))
    }
  }

  const sectionsForSelectedCourse = useMemo(() => {
    const dyn = courseSections[selectedCourse]
    if (dyn && dyn.length > 0) return dyn
    const course = courses.find(c => c.code === selectedCourse)
    const count = course?.sections ?? 0
    const arr: string[] = []
    for (let i = 0; i < count; i++) arr.push(String.fromCharCode(65 + i))
    return arr
  }, [courses, selectedCourse, courseSections])

  // Helpers
  const isValidTimeWindow = (_t: string) => true // temporarily disabled for testing
  const isStartBeforeEnd = (a: string, b: string) => a < b
  const timesOverlap = (aStart: string, aEnd: string, bStart: string, bEnd: string) => !(aEnd <= bStart || aStart >= bEnd)

  const validateSchedule = (entry: ScheduleEntry, ignoreIndex?: number): string | null => {
    if (!rooms.some(r => r.id === entry.roomId)) return 'Room is required.'
    if (!/\d{2}:\d{2}/.test(entry.start) || !/\d{2}:\d{2}/.test(entry.end)) return 'Start and End times are required.'
    if (!isValidTimeWindow(entry.start) || !isValidTimeWindow(entry.end)) return 'Time must be between 09:00 and 16:00.'
    if (!isStartBeforeEnd(entry.start, entry.end)) return 'Start time must be earlier than end time.'
    // Overlap check for same course+section on the same day
    const list = schedules[activeDay]
    for (let i = 0; i < list.length; i++) {
      if (ignoreIndex != null && i === ignoreIndex) continue
      const s = list[i]
      if (s.course === entry.course && s.section === entry.section) {
        if (timesOverlap(entry.start, entry.end, s.start, s.end)) return 'Overlapping schedule for this section.'
      }
    }
    return null
  }

  // Course CRUD (add only for now)
  const openAddCourse = () => {
    setNewCourseCode('')
    setNewCourseStudents('')
    setNewCourseSections('')
    setIsAddCourseOpen(true)
  }

  const saveNewCourse = async () => {
    const studentsNum = Number(newCourseStudents || '0')
    const sectionsNum = Number(newCourseSections || '0')
    if (!newCourseCode.trim()) { alert('Course code is required.'); return }
    // Students/sections are optional and will be derived from registrations
    try {
      await axios.post(`${API}/planner/courses`, { code: newCourseCode.trim(), students: studentsNum || 0, sections: sectionsNum || 0 })
      await loadCourses()
    } catch (e: any) {
      const msg = e?.response?.data?.detail || e?.message || 'Failed to create course'
      alert(String(msg))
      return
    }
    setIsAddCourseOpen(false)
  }

  // Schedule CRUD
  const openAddSchedule = (courseCode: string) => {
    setSelectedCourse(courseCode)
    setSelectedSection('')
    setSelectedRoomId('')
    setStartTime('')
    setEndTime('')
    setEditingIndex(null)
    setFormError(null)
    setIsScheduleOpen(true)
  }

  const openEditSchedule = (index: number) => {
    const item = schedules[activeDay][index]
    setSelectedCourse(item.course)
    setSelectedSection(item.section)
    setSelectedRoomId(String(item.roomId))
    setStartTime(item.start)
    setEndTime(item.end)
    setEditingIndex(index)
    setFormError(null)
    setIsScheduleOpen(true)
  }

  const saveSchedule = async () => {
    const entry: ScheduleEntry = {
      course: selectedCourse,
      section: selectedSection,
      roomId: Number(selectedRoomId || '0'),
      start: startTime,
      end: endTime
    }
    const err = validateSchedule(entry, editingIndex ?? undefined)
    if (err) { setFormError(err); return }
    try {
      const isEdit = editingIndex != null
      const existing = isEdit ? schedules[activeDay][editingIndex!] : undefined
      if (isEdit && existing?.id) {
        // Keep using planner update for template edits
        await axios.put(`${API}/planner/schedules/${encodeURIComponent(existing.id)}`, {
          day_of_week: activeDay,
          course: entry.course,
          section: entry.section,
          room_id: entry.roomId,
          start: entry.start,
          end: entry.end,
          active: true,
        })
      } else {
        // Use new schedules alias to add a concrete schedule on a date-by-day_of_week
        await axios.post(`${API}/schedules`, {
          day_of_week: activeDay,
          course_code: entry.course,
          section: entry.section,
          room_id: entry.roomId,
          start_time: entry.start,
          end_time: entry.end,
        })
      }
      await loadSchedulesForDay(activeDay)
    } catch (e: any) {
      setFormError(String(e?.response?.data?.detail || e?.message || 'Failed to save schedule'))
      return
    }
    setIsScheduleOpen(false)
  }

  const deleteSchedule = async (index: number) => {
    const item = schedules[activeDay][index]
    if (item?.id) {
      try {
        await axios.delete(`${API}/planner/schedules/${encodeURIComponent(item.id)}`)
        await loadSchedulesForDay(activeDay)
        return
      } catch (e: any) {
        showToast('error', messages.streams.failedDeleteSchedule(String(e?.response?.data?.detail || e?.message)))
      }
    }
    // Fallback to local removal if no id
    setSchedules(prev => ({ ...prev, [activeDay]: prev[activeDay].filter((_, i) => i !== index) }))
  }

  return (
    <Layout title="Classrooms">
      <div className="space-y-6">
        {/* Tabs Mon-Sat */}
        <div className="bg-white border rounded-lg p-4">
          <div className="flex flex-wrap gap-2">
            {allDays.map(d => (
              <button
                key={d}
                className={`px-3 py-1.5 rounded-md text-sm border ${activeDay === d ? 'bg-indigo-600 text-white border-indigo-600' : 'bg-white text-gray-700 hover:bg-gray-50'}`}
                onClick={() => setActiveDay(d)}
              >
                {d}
              </button>
            ))}
            <div className="ml-auto">
              <div className="flex gap-2">
                <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm bg-gray-100 hover:bg-gray-200 text-gray-800" onClick={openQuickSchedule}>
                  <Clock className="w-4 h-4" /> Schedule Class
                </button>
                <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm bg-gray-100 hover:bg-gray-200 text-gray-800" onClick={publishToday}>
                  Publish Today
                </button>
                <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md text-sm bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => setIsAddRoomOpen(true)}>
                  <Building2 className="w-4 h-4" /> Add Room
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Rooms and per-room courses */}
        <div className="bg-white border rounded-lg p-6">
          <div className="flex items-center justify-between mb-4">
            <h3 className="text-lg font-semibold text-gray-900">Rooms</h3>
            <div />
          </div>
          {rooms.length === 0 ? (
            <div className="text-sm text-gray-500">No rooms yet. Click "Add Room" to create one.</div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {rooms.map(r => (
                <div key={r.id} className="border rounded-lg p-4 hover:shadow-sm">
                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-base font-semibold text-gray-900">{r.name}</div>
                      <div className="text-xs text-gray-500 break-all">{r.cameraName || (r.cameraId != null ? `Camera #${r.cameraId}` : 'No camera')}</div>
                    </div>
                    <Camera className="w-5 h-5 text-gray-400" />
                  </div>
                  <div className="mt-3 flex gap-2">
                    <button className="inline-flex items-center gap-2 px-2.5 py-1.5 rounded-md text-sm bg-gray-100 hover:bg-gray-200" onClick={() => openManageCourses(r)}>
                      <ListPlus className="w-4 h-4" /> Manage Courses
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Day Schedule Table */}
        <div className="bg-white border rounded-lg p-6">
          <h3 className="text-lg font-semibold text-gray-900 mb-4">{activeDay} Schedule</h3>
          {schedules[activeDay].length === 0 ? (
            <div className="text-sm text-gray-500">No schedules for {activeDay}. Use "Add Schedule" on a course card.</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="min-w-full divide-y divide-gray-200 text-sm">
                <thead>
                  <tr className="bg-gray-50">
                    <th className="px-3 py-2 text-left font-medium text-gray-700">Time Slot</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-700">Course</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-700">Section</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-700">Room</th>
                    <th className="px-3 py-2 text-left font-medium text-gray-700">Status</th>
                    <th className="px-3 py-2 text-right font-medium text-gray-700">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {schedules[activeDay].map((s, idx) => (
                    <tr key={idx} className="hover:bg-gray-50">
                      <td className="px-3 py-2 whitespace-nowrap"><span className="inline-flex items-center gap-1"><Clock className="w-4 h-4 text-gray-400" /> {s.start} – {s.end}</span></td>
                      <td className="px-3 py-2 whitespace-nowrap">{s.course}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{s.section}</td>
                      <td className="px-3 py-2 whitespace-nowrap">{rooms.find(r => r.id === s.roomId)?.name || `Room #${s.roomId}`}</td>
                      <td className="px-3 py-2 whitespace-nowrap">
                        <span className="inline-flex px-2 py-0.5 text-xs rounded-full bg-blue-50 text-blue-700 border border-blue-100">Scheduled</span>
                      </td>
                      <td className="px-3 py-2 whitespace-nowrap text-right">
                        <div className="inline-flex items-center gap-2">
                          <button className="px-2 py-1 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => openEditSchedule(idx)}>
                            <Pencil className="w-4 h-4" />
                          </button>
                          <button className="px-2 py-1 rounded-md bg-red-50 text-red-700 hover:bg-red-100" onClick={() => deleteSchedule(idx)}>
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Add Course Dialog (global; optional) */}
        {isAddCourseOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-md rounded-lg bg-white border shadow-lg p-5">
              <div className="text-lg font-semibold text-gray-900 mb-3">Add Course</div>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Course Code</label>
                  <input className="w-full rounded border px-3 py-2" placeholder="e.g., MBA-005" value={newCourseCode} onChange={e => setNewCourseCode(e.target.value)} />
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">Total Students</label>
                    <input type="number" min={0} className="w-full rounded border px-3 py-2" value={newCourseStudents} onChange={e => setNewCourseStudents(e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">Sections Count</label>
                    <input type="number" min={1} className="w-full rounded border px-3 py-2" value={newCourseSections} onChange={e => setNewCourseSections(e.target.value)} />
                  </div>
                </div>
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => setIsAddCourseOpen(false)}>Cancel</button>
                <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={saveNewCourse}>Save</button>
              </div>
            </div>
          </div>
        )}

        {/* Add/Edit Schedule Dialog */}
        {isScheduleOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
              <div className="text-lg font-semibold text-gray-900 mb-3">{editingIndex == null ? 'Add' : 'Edit'} Schedule — {activeDay}</div>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Course</label>
                  <select className="w-full rounded border px-3 py-2" value={selectedCourse} onChange={async e => { const v = e.target.value; setSelectedCourse(v); setSelectedSection(''); await fetchSectionsForCourse(v); }}>
                    <option value="">Select course</option>
                    {courses.map(c => (
                      <option key={c.code} value={c.code}>{c.code}</option>
                    ))}
                  </select>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">Section</label>
                    <select className="w-full rounded border px-3 py-2" value={selectedSection} onChange={e => setSelectedSection(e.target.value)}>
                      <option value="">Select section</option>
                      {sectionsForSelectedCourse.map(s => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">Room</label>
                    <select className="w-full rounded border px-3 py-2" value={selectedRoomId} onChange={e => setSelectedRoomId(e.target.value)}>
                      <option value="">Select room</option>
                      {rooms.map(r => (
                        <option key={r.id} value={String(r.id)}>{r.name}</option>
                      ))}
                    </select>
                    {rooms.length === 0 && (
                      <div className="mt-1 text-xs text-gray-500">No rooms yet. Add rooms from Course Setup section.</div>
                    )}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">Start Time</label>
                    <input type="time" className="w-full rounded border px-3 py-2" value={startTime} onChange={e => setStartTime(e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">End Time</label>
                    <input type="time" className="w-full rounded border px-3 py-2" value={endTime} onChange={e => setEndTime(e.target.value)} />
                  </div>
                </div>
                {formError && <div className="text-sm text-red-600">{formError}</div>}
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => setIsScheduleOpen(false)}>Cancel</button>
                <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={saveSchedule}>Save</button>
              </div>
            </div>
          </div>
        )}
        {/* Add Room Dialog */}
        {isAddRoomOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-md rounded-lg bg-white border shadow-lg p-5">
              <div className="text-lg font-semibold text-gray-900 mb-3">Add Room</div>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Room Name</label>
                  <input className="w-full rounded border px-3 py-2" placeholder="e.g., Room 101" value={newRoomName} onChange={e => setNewRoomName(e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">OpenNVR camera</label>
                  <select className="w-full rounded border px-3 py-2" value={newRoomCameraId} onChange={e => setNewRoomCameraId(e.target.value)}>
                    <option value="">Select camera</option>
                    {cameras.map(c => (
                      <option key={c.id} value={String(c.id)}>{c.name}{c.handle ? ` (${c.handle})` : ''}</option>
                    ))}
                  </select>
                  {cameras.length === 0 && <div className="mt-1 text-xs text-gray-500">No cameras found in OpenNVR yet.</div>}
                </div>
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => setIsAddRoomOpen(false)}>Cancel</button>
                <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={saveNewRoom}>Save</button>
              </div>
            </div>
          </div>
        )}

        {/* Manage Room Courses Dialog */}
        {isManageRoomCoursesOpen && activeRoom && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
              <div className="flex items-center justify-between mb-3">
                <div className="text-lg font-semibold text-gray-900">Manage Courses — {activeRoom.name}</div>
                <button className="p-1 text-gray-500 hover:text-gray-700" onClick={() => setIsManageRoomCoursesOpen(false)}><X className="w-5 h-5" /></button>
              </div>
              <div className="space-y-3">
                <div className="flex gap-2">
                  <input className="flex-1 rounded border px-3 py-2" placeholder="Course code e.g., MBA-005" value={newRoomCourseCode} onChange={e => setNewRoomCourseCode(e.target.value)} />
                  <button className="px-3 py-2 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={addCourseToRoom}><Plus className="w-4 h-4" /></button>
                </div>
                <div className="text-xs text-gray-500">Add multiple course codes to this room.</div>
                <div className="border rounded">
                  {roomCourses.length === 0 ? (
                    <div className="p-3 text-sm text-gray-500">No courses added to this room.</div>
                  ) : (
                    <ul className="divide-y">
                      {roomCourses.map(code => (
                        <li key={code} className="flex items-center justify-between px-3 py-2 text-sm">
                          <span className="font-mono">{code}</span>
                          <button className="px-2 py-1 text-red-600 hover:bg-red-50 rounded" onClick={() => removeCourseFromRoom(code)}>Remove</button>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
              <div className="mt-5 flex justify-end">
                <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => setIsManageRoomCoursesOpen(false)}>Close</button>
              </div>
            </div>
          </div>
        )}

        {/* Quick Schedule Dialog */}
        {isQuickScheduleOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
              <div className="flex items-center justify-between mb-3">
                <div className="text-lg font-semibold text-gray-900">Schedule Class — {activeDay}</div>
                <button className="p-1 text-gray-500 hover:text-gray-700" onClick={() => setIsQuickScheduleOpen(false)}><X className="w-5 h-5" /></button>
              </div>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Room</label>
                  <select className="w-full rounded border px-3 py-2" value={qsRoomId} onChange={async e => { setQsRoomId(e.target.value); setQsCourse(''); await loadRoomCoursesForScheduler(Number(e.target.value || '0')) }}>
                    <option value="">Select room</option>
                    {rooms.map(r => (
                      <option key={r.id} value={String(r.id)}>{r.name}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Course (from selected room)</label>
                  <select className="w-full rounded border px-3 py-2" value={qsCourse} onChange={async e => { const v = e.target.value; setQsCourse(v); setQsSection(''); await fetchSectionsForCourse(v); }} disabled={!qsRoomId}>
                    <option value="">Select course</option>
                    {qsRoomCourses.map(code => (
                      <option key={code} value={code}>{code}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Section</label>
                  <select className="w-full rounded border px-3 py-2" value={qsSection} onChange={e => setQsSection(e.target.value)} disabled={!qsCourse}>
                    <option value="">Select section</option>
                    {sectionsForCourse(qsCourse).map(s => (
                      <option key={s} value={s}>{s}</option>
                    ))}
                  </select>
                </div>
                <div className="grid grid-cols-2 gap-3">
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">Start Time</label>
                    <input type="time" className="w-full rounded border px-3 py-2" value={qsStart} onChange={e => setQsStart(e.target.value)} />
                  </div>
                  <div>
                    <label className="block text-sm text-gray-700 mb-1">End Time</label>
                    <input type="time" className="w-full rounded border px-3 py-2" value={qsEnd} onChange={e => setQsEnd(e.target.value)} />
                  </div>
                </div>
                {qsError && <div className="text-sm text-red-600">{qsError}</div>}
              </div>
              <div className="mt-5 flex justify-end gap-2">
                <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" onClick={() => setIsQuickScheduleOpen(false)}>Cancel</button>
                <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={saveQuickSchedule}>Save</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </Layout>
  )
}
