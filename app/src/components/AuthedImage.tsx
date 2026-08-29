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

// Renders an auth-gated image (e.g. a timeline evidence photo): the
// endpoint requires the JWT header, which a bare <img src> can't send,
// so we fetch the blob through the api client and objectURL it.
// The objectURL is revoked on unmount / source change (no leak).

import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

export function AuthedImage({
  fetchBlob,
  queryKey,
  alt,
  className,
  onClick,
}: {
  fetchBlob: () => Promise<{ data: any }>
  queryKey: (string | number)[]
  alt: string
  className?: string
  onClick?: () => void
}) {
  const blobQuery = useQuery({
    queryKey,
    queryFn: async () => (await fetchBlob()).data as Blob,
    retry: 0,
    staleTime: 5 * 60 * 1000, // evidence never changes once written
  })
  const [url, setUrl] = useState<string | null>(null)

  useEffect(() => {
    if (!(blobQuery.data instanceof Blob)) return
    const u = URL.createObjectURL(blobQuery.data)
    setUrl(u)
    return () => URL.revokeObjectURL(u)
  }, [blobQuery.data])

  if (blobQuery.isError || (!blobQuery.isPending && !url)) {
    return (
      <div className={`grid place-items-center text-[10px] text-[var(--text-dim)] bg-[var(--bg-2)] ${className ?? ''}`}>
        no photo
      </div>
    )
  }
  if (!url) {
    return <div className={`animate-pulse bg-[var(--bg-2)] ${className ?? ''}`} />
  }
  return <img src={url} alt={alt} className={className} onClick={onClick} />
}
