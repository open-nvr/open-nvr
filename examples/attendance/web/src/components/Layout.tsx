// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// attendly's frame: a sticky white header with the nav, a user menu, and
// the page below on gray-50.

import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { ExternalLink, LogOut, Menu, X } from 'lucide-react'
import { useAuth } from '../auth'

const NAV: { to: string; label: string; need?: 'enroll' | 'manage' }[] = [
  { to: '/register', label: 'Register', need: 'enroll' },
  { to: '/people', label: 'People' },
  { to: '/groups', label: 'Groups' },
  { to: '/schedule', label: 'Schedule' },
  { to: '/attendance', label: 'Attendance' },
  { to: '/reports', label: 'Reports' },
  { to: '/settings', label: 'Settings', need: 'manage' },
]

/** OpenNVR's own UI: the same host, its default HTTPS port. */
function openNvrUrl(): string {
  return `${window.location.protocol}//${window.location.hostname}/`
}

export function Logo({ size = 'h-8 w-8' }: { size?: string }) {
  return <img src="/favicon.svg" alt="" className={size} />
}

export default function Layout({ children }: { children: ReactNode }) {
  const { me, can, signOut } = useAuth()
  const [menu, setMenu] = useState(false)
  const [nav, setNav] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const location = useLocation()

  useEffect(() => {
    setNav(false)
    setMenu(false)
  }, [location.pathname])

  useEffect(() => {
    const away = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) setMenu(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [])

  const links = NAV.filter((n) => !n.need || can[n.need])
  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `rounded-md px-2 py-1 ${isActive ? 'text-indigo-700 bg-indigo-50' : 'text-gray-700 hover:text-indigo-600'}`

  return (
    <div className="flex min-h-dvh flex-col bg-gray-50">
      <header className="sticky top-0 z-40 border-b bg-white/80 backdrop-blur print:hidden">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
          <Link to="/" className="flex items-center gap-2">
            <Logo />
            <span className="text-lg font-bold text-gray-900">Attendance</span>
          </Link>

          <nav className="hidden items-center gap-1 text-sm lg:flex" aria-label="Main">
            {links.map((n) => (
              <NavLink key={n.to} to={n.to} className={linkClass}>
                {n.label}
              </NavLink>
            ))}
          </nav>

          <div className="flex items-center gap-2">
            {me && (
              <div className="relative" ref={menuRef}>
                <button
                  onClick={() => setMenu((m) => !m)}
                  className="flex items-center gap-2 text-sm text-gray-700 hover:text-indigo-600"
                  aria-haspopup="menu"
                  aria-expanded={menu}
                >
                  <span className="flex h-7 w-7 items-center justify-center rounded-full bg-indigo-600 text-xs font-medium text-white">
                    {me.username.charAt(0).toUpperCase()}
                  </span>
                  <span className="hidden sm:inline">{me.username}</span>
                </button>
                {menu && (
                  <div role="menu" className="absolute right-0 mt-2 w-56 overflow-hidden rounded-md border bg-white shadow-lg">
                    {me.email && <div className="break-all border-b px-4 py-2 text-xs text-gray-500">{me.email}</div>}
                    <a
                      role="menuitem"
                      href={openNvrUrl()}
                      className="flex items-center gap-2 px-4 py-2 text-sm text-gray-700 hover:bg-gray-100"
                    >
                      <ExternalLink className="h-4 w-4" /> Open OpenNVR
                    </a>
                    <button
                      role="menuitem"
                      onClick={signOut}
                      className="flex w-full items-center gap-2 px-4 py-2 text-left text-sm text-red-600 hover:bg-gray-100"
                    >
                      <LogOut className="h-4 w-4" /> Sign out
                    </button>
                  </div>
                )}
              </div>
            )}
            <button
              className="rounded-md p-1.5 text-gray-600 hover:bg-gray-100 lg:hidden"
              onClick={() => setNav((n) => !n)}
              aria-label={nav ? 'Close menu' : 'Open menu'}
            >
              {nav ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
            </button>
          </div>
        </div>
        {nav && (
          <nav className="flex flex-col gap-1 border-t px-4 py-2 text-sm lg:hidden" aria-label="Main">
            {links.map((n) => (
              <NavLink key={n.to} to={n.to} className={linkClass}>
                {n.label}
              </NavLink>
            ))}
          </nav>
        )}
      </header>
      <main className="mx-auto w-full max-w-6xl flex-1 px-4 py-5">{children}</main>
    </div>
  )
}
