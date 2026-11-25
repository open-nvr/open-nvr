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

import { useState } from 'react'
import { Bug, MessageCircle, MessagesSquare, Star, X } from 'lucide-react'
import { useTranslation } from '../i18n'

/*
 * Where to find other OpenNVR users. Plain links only: nothing here fetches
 * anything, so a deployment still never contacts the internet on its own.
 */
const REPO = 'https://github.com/open-nvr/open-nvr'

export const COMMUNITY_LINKS = [
  { key: 'community.discord', href: 'https://opennvr.org/discord', Icon: MessageCircle },
  { key: 'community.discussions', href: `${REPO}/discussions`, Icon: MessagesSquare },
  { key: 'community.reportBug', href: `${REPO}/issues/new/choose`, Icon: Bug },
  { key: 'community.star', href: REPO, Icon: Star },
] as const

const DISMISS_KEY = 'opennvr.community-strip.dismissed.v1'

function readDismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === '1'
  } catch {
    return false
  }
}

function linkProps(href: string) {
  return { href, target: '_blank', rel: 'noopener noreferrer' }
}

/** The full set of community links, as a card for the Support page. */
export function CommunityCard() {
  const { t } = useTranslation()
  return (
    <div className="card">
      <h2 className="font-medium mb-1">{t('community.title')}</h2>
      <p className="text-sm text-[var(--text-dim)] mb-3">{t('community.body')}</p>
      <div className="flex flex-wrap gap-2">
        {COMMUNITY_LINKS.map(({ key, href, Icon }) => (
          <a key={key} className="btn inline-flex items-center gap-1.5" {...linkProps(href)}>
            <Icon size={14} /> {t(key)}
          </a>
        ))}
      </div>
    </div>
  )
}

/**
 * One slim line above the dashboard grid. The grid fills the height left
 * below its top edge, so this must stay a single short row; it is in the DOM
 * before the grid first measures, and dismissing it triggers a re-measure. Dismissal is
 * per browser and survives reloads; if storage is unavailable it just shows.
 */
export function CommunityStrip() {
  const { t } = useTranslation()
  const [dismissed, setDismissed] = useState(readDismissed)
  if (dismissed) return null

  const dismiss = () => {
    try {
      localStorage.setItem(DISMISS_KEY, '1')
    } catch {
      // Storage blocked: hide for this page view only.
    }
    setDismissed(true)
    // Removing the strip moves the grid's top edge without resizing the grid
    // or necessarily the body, so nudge the grid's window-resize listener to
    // re-measure the height it may fill once the strip is gone.
    requestAnimationFrame(() => window.dispatchEvent(new Event('resize')))
  }

  return (
    <div className="flex items-center gap-3 px-3 py-1 text-[12px] border border-[var(--border)] bg-[var(--panel)] text-[var(--text-dim)] min-w-0">
      <MessageCircle size={14} className="shrink-0 text-[var(--accent)]" />
      <span className="truncate min-w-0">{t('community.strip')}</span>
      <span className="flex items-center gap-3 shrink-0 ml-auto">
        {COMMUNITY_LINKS.slice(0, 2).map(({ key, href }) => (
          <a key={key} className="underline text-[var(--accent)] whitespace-nowrap" {...linkProps(href)}>
            {t(key)}
          </a>
        ))}
        <button
          type="button"
          className="inline-flex items-center gap-1 whitespace-nowrap hover:text-[var(--text)]"
          onClick={dismiss}
        >
          <X size={13} /> {t('community.dismiss')}
        </button>
      </span>
    </div>
  )
}
