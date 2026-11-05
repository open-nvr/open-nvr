// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// A page that throws while drawing shows what went wrong, inside the app's
// frame, instead of React unmounting everything into a blank screen. The
// boundary is keyed by the route, so going to another page starts afresh.

import { Component, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { AlertTriangle } from 'lucide-react'

type State = { error: Error | null }

class Boundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error) {
    console.error('attendance page failed:', error)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="mx-auto mt-10 max-w-xl rounded-lg border border-red-200 bg-red-50 p-5 text-sm text-red-800">
        <div className="mb-2 flex items-center gap-2 font-semibold">
          <AlertTriangle className="h-4 w-4" /> This page could not be shown
        </div>
        <p className="mb-3">{this.state.error.message}</p>
        <button
          type="button"
          className="rounded-md border border-red-300 bg-white px-3 py-1.5 font-medium hover:bg-red-100"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    )
  }
}

export function PageErrorBoundary({ children }: { children: ReactNode }) {
  const { pathname } = useLocation()
  return <Boundary key={pathname}>{children}</Boundary>
}
