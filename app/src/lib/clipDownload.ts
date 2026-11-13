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

// Clip download with real progress.
//
// The export route streams fragmented MP4 (chunked, no Content-Length), and
// MediaMTX takes several seconds before the first byte. Progress therefore
// comes from the media itself: every fragment's moof/traf/tfdt carries its
// decode time from the clip start, so time / clip length is the share done.
//
// Where the bytes go: a file the user picked (File System Access API,
// Chromium) is written as it arrives, so even a 4 h clip never sits in
// memory. Elsewhere, short clips are collected and saved as a Blob; long
// ones go to the browser's own download, without in-app progress.

/** Clips up to this long may be buffered in memory where no file can be picked. */
export const MAX_BUFFERED_CLIP_SECONDS = 10 * 60

export interface ClipProgress {
  /** No byte yet: the server is still cutting the clip. */
  phase: 'preparing' | 'downloading'
  bytes: number
  /** Media time reached, seconds from the clip start. */
  mediaSeconds: number
}

/** Something chunks can be written to and closed (FileSystemWritableFileStream fits). */
export interface ClipSink {
  write(chunk: Uint8Array): Promise<void>
  close(): Promise<void>
  abort?(reason?: unknown): Promise<void>
}

const ascii = (b: Uint8Array, at: number) => String.fromCharCode(b[at], b[at + 1], b[at + 2], b[at + 3])

/** Child box payload by type path, inside a box payload; null if absent. */
function findBox(buf: Uint8Array, path: string[]): Uint8Array | null {
  const view = new DataView(buf.buffer, buf.byteOffset, buf.byteLength)
  let i = 0
  while (i + 8 <= buf.length) {
    const size = view.getUint32(i)
    const type = ascii(buf, i + 4)
    if (size < 8 || i + size > buf.length) return null
    if (type === path[0]) {
      const body = buf.subarray(i + 8, i + size)
      return path.length === 1 ? body : findBox(body, path.slice(1))
    }
    i += size
  }
  return null
}

/**
 * Follows top-level MP4 boxes across arbitrary chunk boundaries. Small
 * boxes (moov, moof) are buffered and read; mdat is only counted past.
 */
export class Fmp4Clock {
  private timescale = 0
  private pending = new Uint8Array(0)
  private skip = 0
  /** Latest fragment decode time, seconds from the clip start. */
  seconds = 0

  push(chunk: Uint8Array): void {
    let data = chunk
    while (data.length) {
      if (this.skip > 0) {
        const n = Math.min(this.skip, data.length)
        this.skip -= n
        data = data.subarray(n)
        continue
      }
      const merged = new Uint8Array(this.pending.length + data.length)
      merged.set(this.pending)
      merged.set(data, this.pending.length)
      this.pending = merged
      data = new Uint8Array(0)
      while (this.pending.length >= 8) {
        const view = new DataView(this.pending.buffer, this.pending.byteOffset, this.pending.byteLength)
        let size = view.getUint32(0)
        const type = ascii(this.pending, 4)
        let header = 8
        if (size === 1) {
          if (this.pending.length < 16) return
          size = Number(view.getBigUint64(8))
          header = 16
        }
        if (size < header) {
          // Not MP4 as expected: stop reading, keep counting bytes.
          this.skip = Number.MAX_SAFE_INTEGER
          this.pending = new Uint8Array(0)
          return
        }
        if (type === 'moov' || type === 'moof') {
          if (this.pending.length < size) break // wait for the whole box
          this.read(type, this.pending.subarray(header, size))
          this.pending = this.pending.subarray(size)
        } else {
          // mdat and the rest: skip the payload without buffering it.
          const have = Math.min(size, this.pending.length)
          this.skip = size - have
          this.pending = this.pending.subarray(have)
        }
      }
    }
  }

  private read(type: string, body: Uint8Array): void {
    if (type === 'moov') {
      const mdhd = findBox(body, ['trak', 'mdia', 'mdhd'])
      if (mdhd && mdhd.length >= 24) {
        const v = new DataView(mdhd.buffer, mdhd.byteOffset, mdhd.byteLength)
        this.timescale = mdhd[0] === 1 ? v.getUint32(20) : v.getUint32(12)
      }
      return
    }
    const tfdt = findBox(body, ['traf', 'tfdt'])
    if (!tfdt || !this.timescale || tfdt.length < 8) return
    const v = new DataView(tfdt.buffer, tfdt.byteOffset, tfdt.byteLength)
    const t = tfdt[0] === 1 ? Number(v.getBigUint64(4)) : v.getUint32(4)
    this.seconds = Math.max(this.seconds, t / this.timescale)
  }
}

/**
 * Stream ``url`` into ``sink`` (or into memory when none), reporting
 * progress. Resolves with the bytes written and, without a sink, the Blob.
 */
export async function fetchClip(
  url: string,
  opts: { sink?: ClipSink; signal?: AbortSignal; onProgress?: (p: ClipProgress) => void },
): Promise<{ bytes: number; blob?: Blob }> {
  const { sink, signal, onProgress } = opts
  onProgress?.({ phase: 'preparing', bytes: 0, mediaSeconds: 0 })
  const res = await fetch(url, { signal, credentials: 'same-origin' })
  if (!res.ok || !res.body) {
    let detail = `HTTP ${res.status}`
    try {
      const j = await res.json()
      if (j?.detail) detail = String(j.detail)
    } catch {
      /* not JSON */
    }
    throw new Error(detail)
  }
  const reader = res.body.getReader()
  const clock = new Fmp4Clock()
  const parts: Uint8Array[] = []
  let bytes = 0
  let lastReport = 0
  try {
    for (;;) {
      const { done, value } = await reader.read()
      if (done) break
      bytes += value.length
      clock.push(value)
      if (sink) await sink.write(value)
      else parts.push(value)
      const now = performance.now()
      if (now - lastReport > 200) {
        lastReport = now
        onProgress?.({ phase: 'downloading', bytes, mediaSeconds: clock.seconds })
      }
    }
  } catch (e) {
    await sink?.abort?.(e).catch(() => undefined)
    throw e
  }
  onProgress?.({ phase: 'downloading', bytes, mediaSeconds: clock.seconds })
  if (sink) {
    await sink.close()
    return { bytes }
  }
  return { bytes, blob: new Blob(parts as BlobPart[], { type: 'video/mp4' }) }
}

/** Hand a Blob to the browser as a download. */
export function saveBlob(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = href
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(href), 60_000)
}

/** Let the browser download ``url`` itself (no in-app progress). */
export function nativeDownload(url: string): void {
  const a = document.createElement('a')
  a.href = url
  document.body.appendChild(a)
  a.click()
  a.remove()
}

type PickerWindow = Window & {
  showSaveFilePicker?: (o: unknown) => Promise<{ createWritable(): Promise<ClipSink>; name: string }>
  showDirectoryPicker?: (o?: unknown) => Promise<{
    getFileHandle(name: string, o: { create: boolean }): Promise<{ createWritable(): Promise<ClipSink> }>
  }>
}

export const canPickFiles = () => typeof (window as PickerWindow).showSaveFilePicker === 'function'

/**
 * Ask where to save, synchronously inside the click (pickers need the user
 * gesture): one file for one clip, a folder for several. Returns a factory
 * that opens a sink per file name, or null when the user cancelled or the
 * browser has no pickers.
 */
export async function pickDestination(
  filenames: string[],
): Promise<((name: string) => Promise<ClipSink>) | null | 'unsupported'> {
  const w = window as PickerWindow
  if (!w.showSaveFilePicker || !w.showDirectoryPicker) return 'unsupported'
  try {
    if (filenames.length === 1) {
      const handle = await w.showSaveFilePicker({
        suggestedName: filenames[0],
        types: [{ description: 'MP4 video', accept: { 'video/mp4': ['.mp4'] } }],
      })
      return () => handle.createWritable()
    }
    const dir = await w.showDirectoryPicker({ mode: 'readwrite' })
    return async (name: string) => (await dir.getFileHandle(name, { create: true })).createWritable()
  } catch (e: any) {
    if (e?.name === 'AbortError') return null // the user closed the dialog
    return 'unsupported'
  }
}
