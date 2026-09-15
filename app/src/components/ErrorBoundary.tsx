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

// A crashing view must never blank the whole app — the shell (nav, header)
// stays alive and the user can retry or navigate away. Mount with
// key={location.pathname} so navigating resets the boundary automatically.

import { Component, type ReactNode } from 'react'
import { CircleAlert, RefreshCw } from 'lucide-react'
import { useTranslation } from '../i18n'

type Props = { children: ReactNode; title?: string }
type State = { error: Error | null }

function TryAgainLabel() {
  const { t } = useTranslation()
  return <>{t('shared.tryAgain')}</>
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('ErrorBoundary caught:', error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div className="p-6">
        <div className="max-w-lg mx-auto rounded border border-red-700/40 bg-[var(--panel-2)]">
          <div className="px-4 py-3 border-b border-[var(--border)] flex items-center gap-2">
            <CircleAlert size={16} className="text-red-300" />
            <h3 className="text-sm font-semibold text-[var(--text)]">{this.props.title ?? 'Something went wrong'}</h3>
          </div>
          <div className="p-4 space-y-3">
            <p className="text-sm text-[var(--text-dim)]">
              This view crashed, but the rest of the app is still running. You can retry, or use the sidebar to go somewhere else.
            </p>
            <pre className="text-xs text-red-300/90 bg-[var(--bg-2)] border border-[var(--border)] rounded p-2 overflow-auto max-h-32">
              {this.state.error.message}
            </pre>
            <button
              className="inline-flex items-center gap-2 rounded px-3 py-1.5 text-sm border border-[var(--border)] bg-[var(--panel)] hover:bg-[var(--panel-2)] text-[var(--text)]"
              onClick={() => this.setState({ error: null })}
            >
              <RefreshCw size={14} /> <TryAgainLabel />
            </button>
          </div>
        </div>
      </div>
    )
  }
}
