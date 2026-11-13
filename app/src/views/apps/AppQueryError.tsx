// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { ErrorCard } from '../../components/ui'
import { extractApiError } from '../../lib/apiError'
import { useTranslation } from '../../i18n'

/**
 * The error an app page shows when one of its two queries fails.
 *
 * - `kind="apps"`: the installed-apps list failed, so the page cannot tell
 *   whether its app is installed. It used to say "install the app" — wrong
 *   advice for an operator whose app is installed and whose core is down.
 * - `kind="status"`: the app is installed but core could not fetch its live
 *   state. It used to render "No cameras selected", which reads as a setup
 *   mistake rather than an outage.
 */
export function AppQueryError({ kind, error, onRetry }: {
  kind: 'apps' | 'status'
  error: unknown
  onRetry: () => void
}) {
  const { t } = useTranslation()
  return kind === 'apps' ? (
    <ErrorCard
      title={t('apps.listFailedTitle')}
      message={extractApiError(error, t('apps.listFailedMessage'))}
      onRetry={onRetry}
    />
  ) : (
    <ErrorCard
      title={t('apps.statusFailedTitle')}
      message={extractApiError(error, t('apps.statusFailedMessage'))}
      onRetry={onRetry}
    />
  )
}
