/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 * 
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

// Minimal fetch-based API client with token handling and query params support
export type ApiConfig = {
  baseURL?: string
  headers?: Record<string, string>
}

export type RequestOptions = {
  headers?: Record<string, string>
  params?: Record<string, any>
  responseType?: 'json' | 'text' | 'blob'
  signal?: AbortSignal
}

const TOKEN_KEY = 'opennvr.token'
const REFRESH_KEY = 'opennvr.refresh_token'

let authToken: string | null = null
let isRefreshing = false
let pendingRequests: Array<(token: string | null) => void> = []

export function setAuthToken(token: string | null) {
  authToken = token
}

async function tryRefreshToken(): Promise<string | null> {
  const refreshToken = localStorage.getItem(REFRESH_KEY)
  if (!refreshToken) return null

  const baseURL = ((import.meta as any)?.env?.VITE_API_BASE_URL && String((import.meta as any).env.VITE_API_BASE_URL).trim()) ||
    ((import.meta as any)?.env?.PROD
      ? (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000')
      : 'http://localhost:8000')

  const url = buildUrl(baseURL, '/api/v1/auth/refresh')
  const resp = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
    body: JSON.stringify({ refresh_token: refreshToken }),
    credentials: 'omit',
  })

  if (!resp.ok) {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(REFRESH_KEY)
    authToken = null
    return null
  }

  const data = await resp.json()
  authToken = data.access_token
  localStorage.setItem(TOKEN_KEY, data.access_token)
  localStorage.setItem(REFRESH_KEY, data.refresh_token)
  return data.access_token
}

// API Base URL configuration
// - Development: Uses relative URLs (Vite proxy handles routing to backend)
// - Production: Uses window.location.origin (frontend served by backend)
// - Override: Set VITE_API_BASE_URL in app/.env for custom backend URL
const defaultBaseURL =
  ((import.meta as any)?.env?.VITE_API_BASE_URL && String((import.meta as any).env.VITE_API_BASE_URL).trim()) ||
  ((import.meta as any)?.env?.PROD
    ? (typeof window !== 'undefined' ? window.location.origin : 'http://localhost:8000')
    : 'http://localhost:8000')  // Fallback for dev mode when Vite proxy is active

function buildUrl(base: string, path: string, params?: Record<string, any>) {
  // Ensure path starts with /
  const cleanPath = path.startsWith('/') ? path : '/' + path
  
  // If base is empty or not a valid URL, use relative path construction
  if (!base || base === '') {
    const url = new URL(cleanPath, window.location.origin)
    if (params) {
      Object.entries(params).forEach(([k, v]) => {
        if (v === undefined || v === null) return
        if (Array.isArray(v)) v.forEach((vv) => url.searchParams.append(k, String(vv)))
        else url.searchParams.set(k, String(v))
      })
    }
    return url.pathname + url.search
  }
  
  // Build absolute URL with base
  const url = new URL(cleanPath.replace(/^\//, ''), base.endsWith('/') ? base : base + '/')
  if (params) {
    Object.entries(params).forEach(([k, v]) => {
      if (v === undefined || v === null) return
      if (Array.isArray(v)) v.forEach((vv) => url.searchParams.append(k, String(vv)))
      else url.searchParams.set(k, String(v))
    })
  }
  return url.toString()
}

async function doFetch(method: string, fullUrl: string, headers: Record<string, string>, body: BodyInit | undefined, options: RequestOptions) {
  const resp = await fetch(fullUrl, {
    method,
    headers,
    body: method === 'GET' ? undefined : body,
    signal: options.signal,
    credentials: 'omit',
  })

  const rtype = options.responseType || 'json'
  let payload: any = null
  try {
    if (rtype === 'text') payload = await resp.text()
    else if (rtype === 'blob') payload = await resp.blob()
    else payload = await resp.json()
  } catch {
    payload = null
  }

  return { resp, payload }
}

async function request(method: string, url: string, data?: any, options: RequestOptions = {}, config: ApiConfig = {}) {
  const baseURL = config.baseURL ?? defaultBaseURL
  const fullUrl = buildUrl(baseURL, url, options.params)

  const headers: Record<string, string> = {
    'Accept': 'application/json',
    ...(config.headers || {}),
    ...(options.headers || {}),
  }

  let body: BodyInit | undefined
  if (data instanceof URLSearchParams || data instanceof FormData) {
    body = data as any
    if (data instanceof URLSearchParams) {
      headers['Content-Type'] = headers['Content-Type'] || 'application/x-www-form-urlencoded'
    }
  } else if (data !== undefined && data !== null && method !== 'GET') {
    headers['Content-Type'] = headers['Content-Type'] || 'application/json'
    body = JSON.stringify(data)
  }

  if (authToken) headers['Authorization'] = `Bearer ${authToken}`

  let { resp, payload } = await doFetch(method, fullUrl, headers, body, options)

  // On 401, attempt a silent token refresh once (skip for the refresh endpoint itself)
  if (resp.status === 401 && !url.includes('/auth/refresh') && !url.includes('/auth/login')) {
    let newToken: string | null = null

    if (isRefreshing) {
      // Queue this request until the in-flight refresh resolves
      newToken = await new Promise<string | null>((resolve) => {
        pendingRequests.push(resolve)
      })
    } else {
      isRefreshing = true
      newToken = await tryRefreshToken()
      isRefreshing = false
      pendingRequests.forEach((resolve) => resolve(newToken))
      pendingRequests = []
    }

    if (newToken) {
      headers['Authorization'] = `Bearer ${newToken}`;
      ({ resp, payload } = await doFetch(method, fullUrl, headers, body, options))
    } else {
      // Refresh failed — redirect to login
      if (typeof window !== 'undefined') window.location.href = '/login'
      const err: any = new Error('Session expired')
      err.status = 401
      err.data = payload
      throw err
    }
  }

  if (!resp.ok) {
    const err: any = new Error((payload && (payload.detail || payload.message)) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.data = payload
    // Expose response headers on auth errors so callers can inspect x-setup-required etc.
    const h: Record<string, string> = {}
    resp.headers.forEach((v, k) => { h[k.toLowerCase()] = v })
    err.headers = h
    throw err
  }

  // Expose response headers as a simple object lower-cased
  const headersObj: Record<string, string> = {}
  resp.headers.forEach((v, k) => { headersObj[k.toLowerCase()] = v })
  return { data: payload, status: resp.status, headers: headersObj }
}

export const api = {
  get: (url: string, options?: RequestOptions) => request('GET', url, undefined, options),
  post: (url: string, data?: any, options?: RequestOptions) => request('POST', url, data, options),
  put: (url: string, data?: any, options?: RequestOptions) => request('PUT', url, data, options),
  patch: (url: string, data?: any, options?: RequestOptions) => request('PATCH', url, data, options),
  delete: (url: string, options?: RequestOptions) => request('DELETE', url, undefined, options),
  setToken: setAuthToken,
}

export type ApiInstance = typeof api
