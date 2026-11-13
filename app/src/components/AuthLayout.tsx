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
      className="grid min-h-screen place-items-center bg-[var(--bg)] p-4 text-[var(--text)]"
      style={{
        backgroundImage: 'linear-gradient(rgba(0,0,0,0.45), rgba(0,0,0,0.45)), url(/opennvr_bg.svg)',
        backgroundSize: 'cover',
        backgroundPosition: 'center',
      }}
    >
      <div className={`flex w-full flex-col items-center gap-6 ${wide ? 'max-w-md' : 'max-w-sm'}`}>
        <Logo className="h-20 w-auto text-[var(--text)]" />
        <div className="w-full space-y-5 border border-[#2a3a4f] bg-[#1a2332]/95 p-7 shadow-2xl backdrop-blur">
          {children}
        </div>
      </div>
    </div>
  )
}

/** A field on the dark card. `pl-10` leaves room for a leading icon. */
export const authInput =
  'h-11 w-full  border border-[#2a3a4f] bg-[#0f1720] pl-10 pr-3 text-sm text-[var(--text)] outline-none transition-colors placeholder:text-[var(--text-dim)] focus:border-[#5eb3f6] focus:ring-2 focus:ring-[#5eb3f6]/20 disabled:cursor-not-allowed disabled:text-[var(--text-dim)] disabled:opacity-70'

/** The leading icon inside an `authInput`. */
export const authFieldIcon = 'pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-[var(--text-dim)]'

export const authLabel = 'block text-sm font-medium text-[var(--text)]'

export const authButton =
  'flex h-11 w-full items-center justify-center gap-2  bg-[var(--accent)] text-sm font-medium text-white shadow-md transition hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[#5eb3f6] focus-visible:ring-offset-2 focus-visible:ring-offset-[#1a2332] disabled:cursor-not-allowed disabled:opacity-60'

export const authLink = 'text-[#5eb3f6] hover:text-[#8ccaf9]'

export function AuthAlert({ tone, icon, children }: { tone: 'warn' | 'error' | 'ok' | 'info'; icon: ReactNode; children: ReactNode }) {
  const styles = {
    warn: 'border-[color-mix(in_oklab,var(--warn)_45%,var(--border))] bg-[color-mix(in_oklab,var(--warn)_14%,transparent)] text-[var(--warn)]',
    error: 'border-[color-mix(in_oklab,var(--danger)_45%,var(--border))] bg-[color-mix(in_oklab,var(--danger)_14%,transparent)] text-[var(--danger)]',
    ok: 'border-[color-mix(in_oklab,var(--ok)_45%,var(--border))] bg-[color-mix(in_oklab,var(--ok)_14%,transparent)] text-[var(--ok)]',
    info: 'border-[color-mix(in_oklab,var(--accent)_45%,var(--border))] bg-[color-mix(in_oklab,var(--accent)_14%,transparent)] text-[var(--accent)]',
  }[tone]
  return (
    <div
      role={tone === 'error' || tone === 'warn' ? 'alert' : 'status'}
      className={`flex items-start gap-2.5  border px-3 py-2.5 text-sm ${styles}`}
    >
      <span className="mt-0.5 shrink-0">{icon}</span>
      <div className="min-w-0">{children}</div>
    </div>
  )
}
