#!/usr/bin/env node
// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Replace Tailwind palette classes with theme-token classes, by role.
 *
 *   node scripts/tokenize-classes.mjs [--dry] <file.tsx> [...]
 *
 * Palette classes (bg-red-900/30, border-neutral-700, text-amber-400, ...)
 * ignore the light theme; the tokens in src/index.css do not. This maps each
 * palette class to the token that plays the same ROLE -- a red text is
 * "danger", a neutral-700 border is "border" -- keeping every variant prefix
 * (hover:, focus:, md:, group-hover:, ...). It also squares corners
 * (`rounded`, `rounded-sm/md/lg/xl/2xl` go; `rounded-full` stays, for dots,
 * avatars and spinners), per docs/UI_GUIDELINES.md.
 *
 * Anything it does not know is left alone and listed at the end, so a person
 * decides those. Run it one wave of pages at a time and review the diff: it
 * is a presentation change, and the diff is the review.
 */
import fs from 'node:fs'

const STATUS = {
  red: 'danger', rose: 'danger',
  amber: 'warn', yellow: 'warn', orange: 'warn',
  green: 'ok', emerald: 'ok', lime: 'ok', teal: 'ok',
  blue: 'accent', sky: 'accent', indigo: 'accent', cyan: 'accent',
}
const NEUTRAL = new Set(['neutral', 'gray', 'slate', 'zinc', 'stone'])

// Neutral scale by role.
function neutral(kind, shade) {
  const n = Number(shade)
  if (kind === 'border' || kind === 'divide' || kind === 'ring') return 'var(--border)'
  if (kind === 'text' || kind === 'placeholder') return n <= 300 ? 'var(--text)' : 'var(--text-dim)'
  if (kind === 'bg') {
    if (n >= 950) return 'var(--bg)'
    if (n >= 900) return 'var(--bg-2)'
    if (n >= 700) return 'var(--panel-2)'
    return null // light greys on a dark UI: decide by hand
  }
  return null
}

function status(kind, color, shade, alpha) {
  const tok = STATUS[color]
  if (!tok) return null
  const v = `var(--${tok})`
  const n = Number(shade)
  if (kind === 'text' || kind === 'placeholder' || kind === 'fill' || kind === 'stroke' || kind === 'accent') return v
  if (kind === 'border' || kind === 'ring' || kind === 'divide') {
    return n >= 800 || alpha ? `color-mix(in_oklab,${v}_45%,var(--border))` : v
  }
  if (kind === 'bg') {
    // Deep shades and translucent tints are TINTS; mid shades are solid fills.
    if (n >= 800 || (alpha && Number(alpha) <= 60)) return `color-mix(in_oklab,${v}_14%,transparent)`
    return tok === 'danger' ? 'var(--critical)' : v
  }
  return null
}

const CLASS = /(^|[\s"'`{])((?:[a-z-]+:)*)(bg|text|border|ring|divide|placeholder|fill|stroke|accent)-([a-z]+)-(\d{2,3})(?:\/(\d{1,3}))?(?=$|[\s"'`}])/g
const ROUNDED = /(^|[\s"'`{])((?:[a-z-]+:)*)rounded(?:-(?:sm|md|lg|xl|2xl|3xl))?(?=$|[\s"'`}])/g

const dry = process.argv.includes('--dry')
const files = process.argv.slice(2).filter((a) => !a.startsWith('--'))
const unknown = new Map()
let total = 0

for (const file of files) {
  const src = fs.readFileSync(file, 'utf8')
  let changed = 0
  let out = src.replace(CLASS, (m, lead, prefix, kind, color, shade, alpha) => {
    const target = NEUTRAL.has(color) ? neutral(kind, shade) : status(kind, color, shade, alpha)
    if (!target) {
      const key = `${kind}-${color}-${shade}${alpha ? '/' + alpha : ''}`
      unknown.set(`${file}: ${key}`, (unknown.get(`${file}: ${key}`) ?? 0) + 1)
      return m
    }
    changed++
    return `${lead}${prefix}${kind}-[${target}]`
  })
  // Square corners -- but only inside string literals that look like class
  // lists, never in prose or comments that happen to say "rounded".
  out = out.replace(/(['"`])((?:(?!\1)[^\\\n]|\\.)*)\1/g, (lit, q, body) => {
    if (!/(?:^|\s)(?:p[xytrbl]?-|m[xytrbl]?-|flex|grid|border|bg-|text-|items-|gap-|w-|h-|inline-)/.test(body)) return lit
    const next = body.replace(ROUNDED, (m, lead) => {
      changed++
      return lead
    })
    return `${q}${next}${q}`
  })
  // Tidy the double spaces a removed class can leave inside a string.
  out = out.replace(/className="([^"]*)"/g, (m, cls) => `className="${cls.replace(/\s{2,}/g, ' ').trim()}"`)
  if (out !== src) {
    total += changed
    if (!dry) fs.writeFileSync(file, out)
    console.log(`${dry ? '[dry] ' : ''}${file}: ${changed} class(es)`)
  }
}
console.log(`${total} replacement(s)`)
if (unknown.size) {
  console.log('\nLeft for a person to decide:')
  for (const [k, n] of unknown) console.log(`  ${k}  x${n}`)
}
