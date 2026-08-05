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

// FastAPI errors come back as {detail: string | [{msg, ...}]} depending on the
// failure; normalize every shape to one human-readable string.

function toStringSafe(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (Array.isArray(v)) return v.map(toStringSafe).filter(Boolean).join(', ')
  // Pydantic v2 prefixes custom-validator messages with "Value error, " /
  // "Assertion error, " — strip it so users see just the human message.
  if (typeof v === 'object' && typeof (v as { msg?: unknown }).msg === 'string') {
    return (v as { msg: string }).msg.replace(/^(Value|Assertion) error, /, '')
  }
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

export function extractApiError(e: any, fallback: string): string {
  const detail = e?.data?.detail ?? e?.response?.data?.detail
  const msg = toStringSafe(detail) || (typeof e?.message === 'string' ? e.message : '')
  return msg || fallback
}
