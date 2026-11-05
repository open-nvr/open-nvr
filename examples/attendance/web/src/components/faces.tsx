// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The person's picture: shown from core, and cut here from an enrolled
// photo (core has no image library; the browser has a canvas).

import { useEffect, useState } from 'react'
import { api, type Person } from '../attendance'

/** A picture costs a request with the user's token (an <img src> cannot
 * send one), so each is fetched once per page and kept. */
const cache = new Map<string, Promise<string | null>>()

function pictureUrl(person: Person, version: number): Promise<string | null> {
  const key = `${person.id}:${version}`
  let hit = cache.get(key)
  if (!hit) {
    hit = api
      .photo(person.id)
      .then((b) => (b ? URL.createObjectURL(b) : null))
      .catch(() => null)
    cache.set(key, hit)
  }
  return hit
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase() || '?'
}

export function Avatar({ person, size = 40, version = 0 }: { person: Person; size?: number; version?: number }) {
  const [url, setUrl] = useState<string | null>(null)
  useEffect(() => {
    let live = true
    setUrl(null)
    if (person.has_photo) pictureUrl(person, version).then((u) => live && setUrl(u))
    return () => {
      live = false
    }
  }, [person.id, person.has_photo, version]) // eslint-disable-line react-hooks/exhaustive-deps

  const style = { width: size, height: size }
  return url ? (
    <img src={url} alt="" style={style} className="shrink-0 rounded-full object-cover ring-1 ring-gray-200" />
  ) : (
    <span
      style={{ ...style, fontSize: size * 0.38 }}
      className="flex shrink-0 items-center justify-center rounded-full bg-indigo-100 font-semibold text-indigo-700"
      aria-hidden
    >
      {initials(person.display_name)}
    </span>
  )
}

/** Forget a person's cached picture after it changed. */
export function pictureChanged(personId: number) {
  for (const key of cache.keys()) if (key.startsWith(`${personId}:`)) cache.delete(key)
}

/** A square head-and-shoulders crop around the face box (pixels of the
 * photo as the face service decoded it), as a small JPEG. */
export async function cutPicture(photo: Blob, bbox: number[], size = 192): Promise<Blob | null> {
  const [x1, y1, x2, y2] = bbox
  if (![x1, y1, x2, y2].every(Number.isFinite) || x2 <= x1 || y2 <= y1) return null
  const img = await createImageBitmap(photo)
  const side = Math.min(Math.max(x2 - x1, y2 - y1) * 1.8, img.width, img.height)
  const cx = (x1 + x2) / 2
  const cy = (y1 + y2) / 2
  const sx = Math.min(Math.max(cx - side / 2, 0), img.width - side)
  const sy = Math.min(Math.max(cy - side / 2, 0), img.height - side)
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size
  canvas.getContext('2d')?.drawImage(img, sx, sy, side, side, 0, 0, size, size)
  img.close()
  return new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.85))
}

/** A photo as the face service should get it: a JPEG at most ``max``
 * pixels on its long side, turned upright (a phone photo's EXIF
 * orientation is applied here, so what is enrolled is what was seen).
 * Small enough for core to keep, so the person's photos can be shown. */
export async function shrinkPhoto(photo: Blob, max = 1600): Promise<Blob> {
  try {
    const img = await createImageBitmap(photo)
    const scale = Math.min(1, max / Math.max(img.width, img.height))
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(img.width * scale)
    canvas.height = Math.round(img.height * scale)
    canvas.getContext('2d')?.drawImage(img, 0, 0, canvas.width, canvas.height)
    img.close()
    const out = await new Promise<Blob | null>((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.9))
    return out ?? photo
  } catch {
    return photo
  }
}
