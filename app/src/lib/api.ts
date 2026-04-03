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

let authToken: string | null = null

export function setAuthToken(token: string | null) {
  authToken = token
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

  if (!resp.ok) {
    const err: any = new Error((payload && (payload.detail || payload.message)) || `HTTP ${resp.status}`)
    err.status = resp.status
    err.data = payload
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
