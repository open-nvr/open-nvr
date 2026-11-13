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

// FastAPI errors come back as {detail: string | [{msg, loc, ...}]} depending on
// the failure; normalize every shape to one human-readable string.

function toStringSafe(v: unknown): string {
  if (v == null) return ''
  if (typeof v === 'string') return v
  if (Array.isArray(v)) return v.map(formatValidationIssue).filter(Boolean).join(', ')
  // Pydantic v2 prefixes custom-validator messages with "Value error, " /
  // "Assertion error, " — strip it so users see just the human message.
  if (typeof v === 'object' && typeof (v as { msg?: unknown }).msg === 'string') {
    return (v as { msg: string }).msg.replace(/^(Value|Assertion) error, /, '')
  }
  // Structured errors ({error, message, ...}: account locked, bad credentials).
  if (typeof v === 'object' && typeof (v as { message?: unknown }).message === 'string') {
    return (v as { message: string }).message
  }
  try {
    return JSON.stringify(v)
  } catch {
    return String(v)
  }
}

function formatValidationIssue(v: unknown): string {
  if (typeof v !== 'object' || v == null || typeof (v as { msg?: unknown }).msg !== 'string') {
    return toStringSafe(v)
  }

  const issue = v as { loc?: unknown[]; msg: string }
  const message = issue.msg.replace(/^(Value|Assertion) error, /, '')
  const location = Array.isArray(issue.loc)
    ? issue.loc
        .filter((part) => !['body', 'query', 'path', 'header', 'cookie'].includes(String(part)))
        .map(String)
        .join('.')
    : ''
  if (!location) return message

  const pathParts = location.split('.')
  const field = pathParts[pathParts.length - 1].replace(/_/g, ' ')
  const label = field.replace(/\b\w/g, (character) => character.toUpperCase())
  return `${label}: ${message}`
}

export function extractApiError(e: any, fallback: string): string {
  const detail = e?.data?.detail ?? e?.response?.data?.detail
  const msg = toStringSafe(detail) || (typeof e?.message === 'string' ? e.message : '')
  if (e?.code === 'API_NETWORK_ERROR' || /^(failed to fetch|api_network_error)$/i.test(msg.trim())) return fallback
  return msg || fallback
}
