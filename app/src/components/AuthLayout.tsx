// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The frame every signed-out screen shares — sign in, two-step
// verification, first-time setup: the background, a fixed-size logo and
// a card of the same width beneath it, centred on the page. One component
// so moving between those screens never jumps, and so they cannot drift
// apart the way three hand-copied layouts did.
//
// The card is always dark (it sits on dark artwork in either theme), so
// its colours are fixed rather than theme tokens.
import type { ReactNode } from 'react'
import { Logo } from './Logo'

export function AuthLayout({ children, wide = false }: { children: ReactNode; wide?: boolean }) {
  return (
    <div
      className="grid min-h-screen place-items-center bg-[var(--bg)] p-4 text-gray-100"
      style={{
        backgroundImage: 'linear-gradient(rgba(0,0,0,0.45), rgba(0,0,0,0.45)), url(/opennvr_bg.svg)',
        backgroundSize: 'cover',
        backgroundPosition: 'center',
      }}
    >
      <div className={`flex w-full flex-col items-center gap-6 ${wide ? 'max-w-md' : 'max-w-sm'}`}>
        <Logo className="h-20 w-auto text-gray-100" />
        <div className="w-full space-y-5 rounded-xl border border-[#2a3a4f] bg-[#1a2332]/95 p-7 shadow-2xl backdrop-blur">
          {children}
        </div>
      </div>
    </div>
  )
}

/** A field on the dark card. `pl-10` leaves room for a leading icon. */
export const authInput =
  'h-11 w-full rounded-lg border border-[#2a3a4f] bg-[#0f1720] pl-10 pr-3 text-sm text-gray-100 outline-none transition-colors placeholder:text-gray-500 focus:border-[#5eb3f6] focus:ring-2 focus:ring-[#5eb3f6]/20 disabled:cursor-not-allowed disabled:text-gray-400 disabled:opacity-70'

/** The leading icon inside an `authInput`. */
export const authFieldIcon = 'pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-gray-500'

export const authLabel = 'block text-sm font-medium text-gray-300'

export const authButton =
  'flex h-11 w-full items-center justify-center gap-2 rounded-lg bg-[var(--accent)] text-sm font-medium text-white shadow-md transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#5eb3f6] focus-visible:ring-offset-2 focus-visible:ring-offset-[#1a2332] disabled:cursor-not-allowed disabled:opacity-60'

export const authLink = 'text-[#5eb3f6] hover:text-[#8ccaf9]'

export function AuthAlert({ tone, icon, children }: { tone: 'warn' | 'error' | 'ok' | 'info'; icon: ReactNode; children: ReactNode }) {
  const styles = {
    warn: 'border-amber-500/30 bg-amber-900/30 text-amber-200',
    error: 'border-red-500/30 bg-red-900/30 text-red-300',
    ok: 'border-emerald-500/30 bg-emerald-900/30 text-emerald-300',
    info: 'border-sky-500/30 bg-sky-900/30 text-sky-200',
  }[tone]
  return (
    <div
      role={tone === 'error' || tone === 'warn' ? 'alert' : 'status'}
      className={`flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-sm ${styles}`}
    >
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}
