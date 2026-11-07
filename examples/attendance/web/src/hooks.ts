// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

import { useCallback, useEffect, useRef, useState } from 'react'
import { errorText } from './api'
import { api, type Camera, type Group } from './attendance'

export type Loaded<T> = {
  data: T | undefined
  error: string | null
  loading: boolean
  reload: () => void
}

/** Load once, again whenever ``deps`` change, and on ``reload()``. A
 *  late answer to an older request never overwrites a newer one. */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[] = [], every?: number): Loaded<T> {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [tick, setTick] = useState(0)
  const seq = useRef(0)
  const reload = useCallback(() => setTick((t) => t + 1), [])

  useEffect(() => {
    const mine = ++seq.current
    setLoading(true)
    load()
      .then((d) => {
        if (mine !== seq.current) return
        setData(d)
        setError(null)
      })
      .catch((e) => {
        if (mine === seq.current) setError(errorText(e))
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick])

  useEffect(() => {
    if (!every) return
    const id = window.setInterval(reload, every)
    return () => window.clearInterval(id)
  }, [every, reload])

  return { data, error, loading, reload }
}

export function useGroups() {
  return useLoad<Group[]>(api.listGroups)
}

export function useCameras() {
  return useLoad<Camera[]>(api.cameras)
}
