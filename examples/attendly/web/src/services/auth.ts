// Login is OpenNVR's: the app proxies /api/auth/login-json, /refresh and
// /me to core and keeps no users of its own. The token pair core issues is
// stored here and attached to every request by services/http.ts.
import axios from 'axios'
import { config } from '../config/environment'
import { attachAuth } from './http'

const api = axios.create({ baseURL: config.API_BASE_URL, timeout: config.API_TIMEOUT })
attachAuth(api)

export interface User {
  id: string
  username: string
  email: string
  mfa_enabled: boolean
  is_admin?: boolean
  role_name?: string | null
  tier?: 'viewer' | 'operator' | 'admin'
  created_at: string
}

export interface LoginResponse {
  access_token: string
  refresh_token?: string
  token_type: string
  device_token?: string
}

export const authService = {
  // Username + password; the server answers 202 when a TOTP code is needed.
  login: (username: string, password: string, code?: string) => {
    const headers: Record<string, string> = {}
    const device = localStorage.getItem('device_token')
    if (device) headers['x-device-token'] = device
    return api.post<LoginResponse>('/api/auth/login-json', code ? { username, password, code } : { username, password }, { headers })
  },
  loginJson: (username: string, password: string, code: string) => authService.login(username, password, code),

  refresh: (refresh_token: string) => api.post<LoginResponse>('/api/auth/refresh', { refresh_token }),

  me: () => api.get<User>('/api/auth/me'),

  setToken: (token: string) => localStorage.setItem('access_token', token),
  setSession: (data: LoginResponse) => {
    localStorage.setItem('access_token', data.access_token)
    if (data.refresh_token) localStorage.setItem('refresh_token', data.refresh_token)
    if (data.device_token) localStorage.setItem('device_token', data.device_token)
  },
  getToken: () => localStorage.getItem('access_token'),
  removeToken: () => {
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    localStorage.removeItem('user')
  },
  setUser: (user: User) => localStorage.setItem('user', JSON.stringify(user)),
  getUser: (): User | null => {
    const user = localStorage.getItem('user')
    return user ? JSON.parse(user) : null
  },
  isAuthenticated: () => !!localStorage.getItem('access_token'),
  logout: () => {
    const token = localStorage.getItem('access_token')
    if (token) api.post('/api/auth/logout').catch(() => {})
    authService.removeToken()
    window.location.href = '/login'
  },
}

export default authService
