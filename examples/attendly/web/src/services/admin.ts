import axios, { type AxiosRequestConfig } from 'axios'
import { config } from '../config/environment'
import { attachAuth } from './http'

const API_BASE_URL = config.API_BASE_URL

const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: config.API_TIMEOUT,
})

attachAuth(api)

export const roomsService = {
  list: () => api.get('/classrooms/'),
  create: (name: string, camera_id: number) => api.post('/classrooms/', { name, camera_id }),
  update: (id: number, data: { name?: string; camera_id?: number }) => api.put(`/classrooms/${id}`, data),
  remove: (id: number) => api.delete(`/classrooms/${id}`),
}

export type CameraOption = { id: number; handle: string; name: string; role?: string }

export const camerasService = {
  list: () => api.get<CameraOption[]>('/cameras/'),
}

export const coursesService = {
  list: () => api.get('/courses'),
  create: (code: string, name?: string, sections_list?: string[]) => api.post('/courses', { code, name, sections_list }),
  update: (code: string, data: { name?: string; sections_list?: string[] }) => api.put(`/courses/${encodeURIComponent(code)}`, data),
  remove: (code: string) => api.delete(`/courses/${encodeURIComponent(code)}`),
  sections: (code: string) => api.get(`/courses/${encodeURIComponent(code)}/sections`),
}

export type DayName = 'Monday' | 'Tuesday' | 'Wednesday' | 'Thursday' | 'Friday' | 'Saturday' | 'Sunday'

export const schedulesService = {
  listByDay: (day: DayName, options?: AxiosRequestConfig) => api.get('/planner/schedules', { params: { day }, ...(options || {}) }),
  createTemplate: (payload: { day_of_week: DayName; course: string; section: string; room_id: number; start: string; end: string; active?: boolean }) =>
    api.post('/planner/schedules', payload),
  updateTemplate: (id: string, payload: Partial<{ day_of_week: DayName; course: string; section: string; room_id: number; start: string; end: string; active: boolean }>) =>
    api.put(`/planner/schedules/${encodeURIComponent(id)}`, payload),
  deleteTemplate: (id: string) => api.delete(`/planner/schedules/${encodeURIComponent(id)}`),
}

// ── Phase 2: staff, departments, gates, meals ──────────────────────
export type Department = { id: number; name: string; camera_id: number | null; camera_name: string | null; work_start: string; work_end: string }
export type Staff = { id: number; staff_code: string; full_name: string; email: string | null; department_id: number | null; department_name: string | null; designation: string | null; created_at: string }
export type Gate = { id: number; name: string; kind: 'hostel' | 'mess'; camera_id: number | null; camera_name: string | null; hostel_block: string | null; allow_staff: boolean; enabled: boolean; open_url: string | null; meal_windows: string | null }
export type AccessEvent = { id: number; gate_id: number; gate_name: string | null; kind: string; person_code: string | null; person_name: string | null; person_kind: string | null; decision: string; reason: string; unit: string | null; similarity: number | null; actuated: boolean; source: string; ts: string }
export type MealBooking = { id: number; student_code: string; date: string; meal: string; source: string; created_at: string; consumed_at: string | null; student?: { full_name: string; course_code?: string | null; section?: string | null; email?: string | null } | null }

export const departmentsService = {
  list: () => api.get<Department[]>('/departments/'),
  create: (data: { name: string; camera_id: number; work_start?: string; work_end?: string }) => api.post<Department>('/departments/', data),
  update: (id: number, data: Partial<{ name: string; camera_id: number; work_start: string; work_end: string }>) => api.put<Department>(`/departments/${id}`, data),
  remove: (id: number) => api.delete(`/departments/${id}`),
}

export const staffService = {
  list: (department_id?: number) => api.get<Staff[]>('/staff/', { params: department_id ? { department_id } : {} }),
  create: (data: { full_name: string; email?: string | null; department_id?: number | null; designation?: string | null }) => api.post<Staff>('/staff/', data),
  update: (code: string, data: Partial<{ full_name: string; email: string | null; department_id: number | null; designation: string | null }>) => api.put<Staff>(`/staff/${encodeURIComponent(code)}`, data),
  remove: (code: string) => api.delete(`/staff/${encodeURIComponent(code)}`),
  faces: (code: string) => api.get(`/staff/${encodeURIComponent(code)}/faces`),
  registerNew: (form: FormData) => api.post('/staff/register_new', form, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 120000 }),
  registerFaces: (code: string, form: FormData) => api.post(`/staff/${encodeURIComponent(code)}/register`, form, { headers: { 'Content-Type': 'multipart/form-data' }, timeout: 120000 }),
  attendance: (date: string, department_id?: number) => api.get('/staff-attendance/', { params: { date, ...(department_id ? { department_id } : {}) } }),
  attendanceDates: () => api.get<string[]>('/staff-attendance/dates'),
  engineStatus: () => api.get('/staff-attendance/engine_status'),
}

export const gatesService = {
  list: () => api.get<Gate[]>('/gates/'),
  create: (data: Partial<Gate> & { name: string; kind: 'hostel' | 'mess'; camera_id: number }) => api.post<Gate>('/gates/', data),
  update: (id: number, data: Partial<Gate>) => api.put<Gate>(`/gates/${id}`, data),
  remove: (id: number) => api.delete(`/gates/${id}`),
  status: () => api.get('/gates/status'),
  events: (params: { gate_id?: number; limit?: number; date?: string; decision?: string }) => api.get<AccessEvent[]>('/gates/events', { params }),
}

export const mealsService = {
  bookings: (date: string, meal?: string) => api.get<{ date: string; bookings: MealBooking[] }>('/meals/bookings', { params: { date, ...(meal ? { meal } : {}) } }),
  book: (data: { student_code?: string; email?: string; date: string; meals: string[] }) => api.post('/meals/bookings', data),
  remove: (id: number) => api.delete(`/meals/bookings/${id}`),
  importCsv: (file: File, date?: string) => {
    const form = new FormData()
    form.append('file', file)
    return api.post('/meals/bookings/import', form, { params: date ? { date } : {}, headers: { 'Content-Type': 'multipart/form-data' } })
  },
  summary: (date: string) => api.get('/meals/summary', { params: { date } }),
}

export default api


