// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import type { ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from '../../i18n'
import { apiService } from '../../lib/apiService'
import { rebaseToCurrentOrigin } from '../../lib/streamUrl'
import { Modal } from '../Modal'
import { VideoPlayer } from '../VideoPlayer'
import { detectionsPreferred } from '../../lib/detectionFeed'

type PeekCamera = {
  name: string
  location?: string | null
  ip_address?: string
  display_aspect_ratio?: string | null
}

/**
 * One camera live in a dialog, over whatever page opened it: closing it puts
 * the viewer back exactly where they were. Used where a list shows stills
 * (the tour editor, the Cameras grid) and live is one click away.
 */
export function LivePeek({ cameraId, cam, onClose, footer }: {
  cameraId: number
  cam: PeekCamera | undefined
  onClose: () => void
  /** Extra controls beside the camera's details, e.g. "Open in Live View". */
  footer?: ReactNode
}) {
  const { t } = useTranslation()
  const info = useQuery({
    queryKey: ['tour-stream', cameraId],
    queryFn: async () => (await apiService.getTourStreamInfo(cameraId)).data as { urls?: { webrtc?: string; hls?: string }; token?: string },
    staleTime: 45 * 60_000,
  })
  const name = cam?.name ?? `#${cameraId}`
  const details = [cam?.location, cam?.ip_address].filter(Boolean).join(' · ')
  return (
    <Modal open title={t('tours.liveTitle', { name })} onClose={onClose} widthClassName="w-[calc(100vw-2rem)] max-w-4xl" bodyClassName="p-0">
      <div className="aspect-video bg-black" data-testid="tour-live-peek">
        {info.data?.urls ? (
          <VideoPlayer
            mode="live"
            chrome="controls"
            whepUrl={rebaseToCurrentOrigin(info.data.urls.webrtc)}
            hlsUrl={rebaseToCurrentOrigin(info.data.urls.hls)}
            mediamtxToken={info.data.token}
            onAuthExpired={() => info.refetch()}
            preferredStreamType="webrtc"
            autoPlay
            muted
            displayAspectOverride={cam?.display_aspect_ratio}
            cameraId={cameraId}
            className="w-full h-full"
            showDetections={detectionsPreferred()}
          />
        ) : (
          <div className="w-full h-full flex items-center justify-center text-sm text-white/60">
            {info.isError ? t('tours.liveFailed') : t('tours.loading')}
          </div>
        )}
      </div>
      {(details || footer) && (
        <div className="flex items-center justify-between gap-3 px-3 py-2 text-xs text-[var(--text-dim)]">
          <span className="truncate">{details}</span>
          {footer}
        </div>
      )}
    </Modal>
  )
}
