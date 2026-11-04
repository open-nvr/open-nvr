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

export default api


