// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Talking to OpenNVR, through the attendance app (attendance/webui.py):
// /auth/* signs in with an OpenNVR account, /api/* is core's /api/v1/*
// as that user. The tokens live in this origin's storage only; this page
// is a different origin from OpenNVR's own, so the two never share them.

const KEY = {
  access: 'attendance.access_token',
  refresh: 'attendance.refresh_token',
  device: 'attendance.device_token',
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key)
    else localStorage.setItem(key, value)
  } catch {
    /* private mode: the session lasts as long as the tab */
  }
}

export const SIGNED_OUT_EVENT = 'attendance:signed-out'

export function hasSession(): boolean {
  return !!read(KEY.access)
}

export function signOut() {
  write(KEY.access, null)
  write(KEY.refresh, null)
  // The device token is kept: it names this browser to OpenNVR's device
  // firewall, not this session, and losing it would mean a new approval.
  window.dispatchEvent(new Event(SIGNED_OUT_EVENT))
}

export class ApiError extends Error {
  status: number
  body: unknown
  constructor(status: number, message: string, body: unknown) {
    super(message)
    this.status = status
    this.body = body
  }
}

/** The human sentence in a core error body, whatever shape it came in. */
export function messageOf(body: unknown, fallback: string): string {
  if (!body) return fallback
  if (typeof body === 'string') return body
  const b = body as Record<string, unknown>
  const d = b.detail ?? b.error ?? b.message
  if (typeof d === 'string') return d
  if (Array.isArray(d) && d.length) {
    const first = d[0] as { msg?: string; loc?: unknown[] }
    const where = Array.isArray(first.loc) ? String(first.loc[first.loc.length - 1]) : ''
    return first.msg ? (where ? `${where}: ${first.msg}` : first.msg) : fallback
  }
  if (d && typeof d === 'object') {
    const m = (d as Record<string, unknown>).message
    if (typeof m === 'string') return m
  }
  return fallback
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.message
  if (e instanceof Error) return e.message
  return 'Something went wrong'
}

function headers(extra: Record<string, string> = {}): Record<string, string> {
  const h: Record<string, string> = { ...extra }
  const access = read(KEY.access)
  const device = read(KEY.device)
  if (access) h.authorization = `Bearer ${access}`
  if (device) h['x-device-token'] = device
  return h
}

async function parse(res: Response): Promise<unknown> {
  const text = await res.text()
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

function keepTokens(body: unknown) {
  const b = (body ?? {}) as Record<string, unknown>
  if (typeof b.access_token === 'string') write(KEY.access, b.access_token)
  if (typeof b.refresh_token === 'string') write(KEY.refresh, b.refresh_token)
  if (typeof b.device_token === 'string' && b.device_token) write(KEY.device, b.device_token)
}

let refreshing: Promise<boolean> | null = null

/** One refresh at a time, however many calls found the token expired. */
function refresh(): Promise<boolean> {
  const token = read(KEY.refresh)
  if (!token) return Promise.resolve(false)
  refreshing ??= (async () => {
    try {
      const res = await fetch('/auth/refresh', {
        method: 'POST',
        headers: headers({ 'content-type': 'application/json' }),
        body: JSON.stringify({ refresh_token: token }),
      })
      if (!res.ok) return false
      keepTokens(await parse(res))
      return true
    } catch {
      return false
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

export type Query = Record<string, string | number | boolean | null | undefined>

function qs(query?: Query): string {
  if (!query) return ''
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v))
  const s = p.toString()
  return s ? `?${s}` : ''
}

type Options = { query?: Query; json?: unknown; form?: FormData; blob?: boolean; raw?: Blob }

async function send(method: string, path: string, opts: Options, retried = false): Promise<Response> {
  const extra: Record<string, string> = {}
  let body: BodyInit | undefined
  if (opts.form) body = opts.form
  else if (opts.raw) {
    extra['content-type'] = opts.raw.type || 'application/octet-stream'
    body = opts.raw
  } else if (opts.json !== undefined) {
    extra['content-type'] = 'application/json'
    body = JSON.stringify(opts.json)
  }
  const res = await fetch(`/api/${path}${qs(opts.query)}`, { method, headers: headers(extra), body })
  if (res.status === 401 && !retried && (await refresh())) return send(method, path, opts, true)
  if (res.status === 401) signOut()
  return res
}

export async function call<T>(method: string, path: string, opts: Options = {}): Promise<T> {
  const res = await send(method, path, opts)
  if (!res.ok) {
    const body = await parse(res)
    throw new ApiError(res.status, messageOf(body, `${res.status} ${res.statusText}`), body)
  }
  return (await parse(res)) as T
}

/** A binary answer (a picture), or null when core has none (404). */
export async function fetchBlob(path: string): Promise<Blob | null> {
  const res = await send('GET', path, {})
  if (res.status === 404) return null
  if (!res.ok) {
    const body = await parse(res)
    throw new ApiError(res.status, messageOf(body, `${res.status} ${res.statusText}`), body)
  }
  return res.blob()
}

/** A file answer (the report as CSV/XLSX), with the name core gave it. */
export async function download(path: string, query: Query, fallbackName: string): Promise<void> {
  const res = await send('GET', path, { query })
  if (!res.ok) {
    const body = await parse(res)
    throw new ApiError(res.status, messageOf(body, `${res.status} ${res.statusText}`), body)
  }
  const cd = res.headers.get('content-disposition') ?? ''
  const name = /filename="?([^";]+)"?/i.exec(cd)?.[1] ?? fallbackName
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

export type LoginResult = { ok: true } | { ok: false; message: string; needsCode: boolean; setup: boolean }

export async function login(username: string, password: string, code?: string): Promise<LoginResult> {
  let res: Response
  try {
    res = await fetch('/auth/login', {
      method: 'POST',
      headers: headers({ 'content-type': 'application/json' }),
      body: JSON.stringify(code ? { username, password, code } : { username, password }),
    })
  } catch {
    return { ok: false, message: 'The attendance app is not answering.', needsCode: false, setup: false }
  }
  const body = await parse(res)
  if (res.ok) {
    keepTokens(body)
    return { ok: true }
  }
  const message = messageOf(body, 'Sign-in failed')
  return {
    ok: false,
    message,
    needsCode: /mfa|code/i.test(message),
    setup: res.headers.get('x-setup-required') === 'true',
  }
}
