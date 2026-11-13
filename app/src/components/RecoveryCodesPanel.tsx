/*
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

// A fresh set of recovery codes, shown once: copy, download, and confirm
// they are saved before moving on. The server keeps only their hashes.

import { useState } from 'react'
import { Button } from './ui'
import { useTranslation } from '../i18n'

type Props = {
  codes: string[]
  username?: string
  onDone: () => void
  doneLabel?: string
}

export function RecoveryCodesPanel({ codes, username, onDone, doneLabel }: Props) {
  const { t } = useTranslation()
  const [saved, setSaved] = useState(false)
  const [copied, setCopied] = useState(false)
  const text = codes.join('\n')

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const download = () => {
    const header = `OpenNVR recovery codes${username ? ` for ${username}` : ''}\n` +
      `Generated ${new Date().toISOString().slice(0, 10)}. Each code signs you in once.\n\n`
    const url = URL.createObjectURL(new Blob([header + text + '\n'], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'opennvr-recovery-codes.txt'
    a.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="border border-[color-mix(in_oklab,var(--ok)_45%,var(--border))] bg-[color-mix(in_oklab,var(--ok)_10%,transparent)] p-4 space-y-3" data-testid="recovery-codes">
      <div className="text-sm font-medium">{t('recoveryCodes.title')}</div>
      <p className="text-xs text-[var(--text-dim)]">{t('recoveryCodes.help')}</p>
      <ol className="grid grid-cols-2 gap-x-6 gap-y-1 font-mono text-sm bg-[var(--panel)] border border-[var(--border)] p-3 select-all">
        {codes.map((c) => <li key={c}>{c}</li>)}
      </ol>
      <div className="flex flex-wrap gap-2">
        <Button size="sm" onClick={copy}>{copied ? t('recoveryCodes.copied') : t('recoveryCodes.copy')}</Button>
        <Button size="sm" onClick={download}>{t('recoveryCodes.download')}</Button>
      </div>
      <label className="flex items-center gap-2 text-sm">
        <input type="checkbox" className="accent-[var(--accent)]" checked={saved} onChange={(e) => setSaved(e.target.checked)} />
        {t('recoveryCodes.saved')}
      </label>
      <div className="flex justify-end">
        <Button variant="primary" disabled={!saved} onClick={onDone}>{doneLabel ?? t('recoveryCodes.done')}</Button>
      </div>
    </div>
  )
}
