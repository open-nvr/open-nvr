/**
 * Copyright (c) 2026 OpenNVR
 * SPDX-License-Identifier: AGPL-3.0-or-later
 *
 * Add the cameras of a remote site in one go (plan S5): the devices the
 * site's agent found (ONVIF discovery, and a camera scan on request), one
 * user and password for all, a check that finds each one's streams and
 * shows a picture, then "Add N cameras". The password goes only to the
 * devices ticked, through the site's agent (server: services/site_probe.py).
 */
import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, Loader2, Radar, Search } from 'lucide-react'
import { useSnackbar } from '../components/Snackbar'
import { extractApiError } from '../lib/apiError'
import { queryClient } from '../lib/queries'
import { Badge, Button } from '../components/ui'
import { Field, Input } from '../components/ui/form'
import { Modal } from '../components/Modal'
import { estimateKbps, siteService, siteUplinkKbps, type BulkCamera, type ProbeResult, type Site } from '../services/siteService'

const SITES_KEY = ['sites']

const STATUS: Record<ProbeResult['status'], { label: string; variant: 'success' | 'warning' | 'destructive' | 'neutral' }> = {
  ok: { label: 'found', variant: 'success' },
  wrong_password: { label: 'wrong password', variant: 'destructive' },
  no_stream_found: { label: 'no stream found', variant: 'warning' },
  unreachable: { label: 'not reachable', variant: 'neutral' },
}

function lastOctet(ip: string): string {
  return ip.split('.').pop() || ip
}

type Pick = {
  key: string; ip: string; name: string; main_url: string; sub_url: string | null; add: boolean
  width: number | null; height: number | null
}

export function AddCamerasDialog({ siteId, onClose, onAddByUrl }: { siteId: number; onClose: () => void; onAddByUrl: () => void }) {
  const { showSuccess, showError } = useSnackbar()
  const { data: sites = [] } = useQuery({ queryKey: SITES_KEY, queryFn: siteService.list, refetchInterval: 2000 })
  const site: Site | undefined = sites.find((s) => s.id === siteId)
  const found = useMemo(() => site?.found ?? [], [site?.found])
  const [ticked, setTicked] = useState<Record<string, boolean>>({})
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [checking, setChecking] = useState(false)
  const [results, setResults] = useState<Record<string, ProbeResult>>({})
  const [picks, setPicks] = useState<Pick[]>([])
  const [adding, setAdding] = useState(false)

  // New devices (a scan just finished) are ticked by default.
  useEffect(() => {
    setTicked((t) => {
      const next = { ...t }
      for (const d of found) if (!(d.ip in next)) next[d.ip] = true
      return next
    })
  }, [found])

  if (!site) return null
  const chosen = found.filter((d) => ticked[d.ip])
  const scanning = !!site.scan?.pending

  const scan = async () => {
    try {
      await siteService.scan(site.id)
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
    } catch (e) {
      showError(extractApiError(e, 'Could not start a scan'))
    }
  }

  const check = async () => {
    setChecking(true)
    try {
      const { results: rs } = await siteService.probe(site.id, chosen.map((d) => d.ip), username.trim(), password)
      const byIp: Record<string, ProbeResult> = { ...results }
      const nextPicks = picks.filter((p) => !rs.some((r) => r.ip === p.ip))
      for (const r of rs) {
        byIp[r.ip] = r
        const dev = found.find((d) => d.ip === r.ip)
        const chans = r.channels ?? []
        chans.forEach((c, i) => {
          const base = dev?.name || r.model || 'Camera'
          nextPicks.push({
            key: `${r.ip}#${c.index ?? i}`,
            ip: r.ip,
            name: chans.length > 1 ? `${site.name} · ${base} · ${c.name || `Ch ${i + 1}`}` : `${site.name} · ${base} · .${lastOctet(r.ip)}`,
            main_url: c.main_url,
            sub_url: c.sub_url,
            add: true,
            width: c.width,
            height: c.height,
          })
        })
      }
      setResults(byIp)
      setPicks(nextPicks)
    } catch (e) {
      showError(extractApiError(e, 'Could not check the cameras'))
    } finally {
      setChecking(false)
    }
  }

  const toAdd = picks.filter((p) => p.add && p.name.trim())
  // What the ticked cameras would send, against the upload left free beside
  // what the site sends already (the last uplink test).
  const subOnly = site.live_policy === 'sub_only'
  const needKbps = toAdd.reduce((sum, p) => sum + (subOnly && p.sub_url
    ? estimateKbps(null, null, true)
    : estimateKbps(p.width, p.height) + (p.sub_url ? estimateKbps(null, null, true) : 0)), 0)
  const freeKbps = site.uplink_test?.kbps ?? null
  const tight = freeKbps != null && needKbps > freeKbps * 0.8
  const testUplink = async () => {
    try {
      await siteService.uplinkTest(site.id)
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
    } catch (e) {
      showError(extractApiError(e, 'Could not start the uplink test'))
    }
  }
  const useSubOnly = async () => {
    try {
      await siteService.update(site.id, { live_policy: 'sub_only' })
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
    } catch (e) {
      showError(extractApiError(e, 'Could not switch to sub streams'))
    }
  }
  const add = async () => {
    setAdding(true)
    try {
      const cams: BulkCamera[] = toAdd.map((p) => ({ name: p.name.trim(), main_url: p.main_url, sub_url: p.sub_url, control_host: p.ip }))
      const res = await siteService.bulkAdd(site.id, cams, username.trim(), password)
      showSuccess(`${res.added.length} camera${res.added.length === 1 ? '' : 's'} added; the site's agent starts relaying them within seconds`)
      void queryClient.invalidateQueries({ queryKey: SITES_KEY })
      void queryClient.invalidateQueries({ queryKey: ['cameras'] })
      onClose()
    } catch (e) {
      showError(extractApiError(e, 'Could not add the cameras'))
    } finally {
      setAdding(false)
    }
  }

  return (
    <Modal open title={`Add cameras at "${site.name}"`} onClose={onClose}
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button variant="ghost" onClick={onAddByUrl}>Add one by its stream URL</Button>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onClose}>Cancel</Button>
            <Button variant="primary" disabled={adding || toAdd.length === 0} onClick={add}>
              {adding ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}
              Add {toAdd.length} camera{toAdd.length === 1 ? '' : 's'}
            </Button>
          </div>
        </div>
      }>
      <div className="space-y-3 text-sm">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[var(--text-dim)]">
            {found.length === 0 ? 'The site\'s agent has not found a camera yet.' : `Found on the site's network: ${found.length}.`}
            {site.scan?.subnets?.length ? ` Scanned ${site.scan.subnets.join(', ')}.` : ''}
          </span>
          <span className="flex-1" />
          <Button size="sm" variant="outline" disabled={scanning || !site.online} onClick={scan}
            title="The site's agent looks for cameras on its networks (camera ports only)">
            {scanning ? <Loader2 size={14} className="animate-spin" /> : <Radar size={14} />}
            {scanning ? 'Scanning…' : 'Scan the network'}
          </Button>
        </div>
        {site.scan?.note && <p className="text-xs text-[var(--text-dim)]">{site.scan.note}</p>}

        {found.length > 0 && (
          <ul className="border border-[var(--border)] divide-y divide-[var(--border)]">
            {found.map((d) => {
              const r = results[d.ip]
              const st = r ? STATUS[r.status] : null
              return (
                <li key={d.ip} className="flex items-center gap-2 px-2 py-1.5">
                  <input type="checkbox" checked={!!ticked[d.ip]} aria-label={`Check ${d.ip}`}
                    onChange={(e) => setTicked({ ...ticked, [d.ip]: e.target.checked })} />
                  <span className="font-mono text-xs">{d.ip}</span>
                  <span>{d.name || r?.model || ''}</span>
                  <span className="text-xs text-[var(--text-dim)]">{[d.hardware, d.onvif ? 'ONVIF' : 'RTSP only'].filter(Boolean).join(' · ')}</span>
                  <span className="flex-1" />
                  {st && <Badge variant={st.variant} title={r?.detail}>{st.label}</Badge>}
                </li>
              )
            })}
          </ul>
        )}

        <div className="grid grid-cols-2 gap-2">
          <Field label="Camera user (all of them)">
            <Input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="off" />
          </Field>
          <Field label="Camera password">
            <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" />
          </Field>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" disabled={checking || chosen.length === 0 || !site.online} onClick={check}>
            {checking ? <Loader2 size={14} className="animate-spin" /> : <Search size={14} />}
            {checking ? 'Checking…' : `Check ${chosen.length} device${chosen.length === 1 ? '' : 's'}`}
          </Button>
          <span className="text-xs text-[var(--text-dim)]">
            The password goes only to the devices ticked, through the site's agent.
          </span>
        </div>

        {toAdd.length > 0 && (
          <div className={`flex flex-wrap items-center gap-2 border p-2 ${tight ? 'border-[var(--warn)]' : 'border-[var(--border)]'}`}>
            <span>
              These {toAdd.length} camera{toAdd.length === 1 ? '' : 's'} need about <b>{(needKbps / 1000).toFixed(1)} Mbit/s</b> of upload
              {subOnly ? ' (sub streams only)' : ''}
              {freeKbps != null
                ? <>; the site has <b>{(freeKbps / 1000).toFixed(1)} Mbit/s</b> free beside the {Math.round(siteUplinkKbps(site) / 100) / 10} Mbit/s it sends now.</>
                : '.'}
              {tight && !subOnly ? ' That is too much for full quality: sub streams only would fit far more.' : ''}
              {tight && subOnly ? ' Even sub streams may not fit: add fewer, or raise the site\'s upload.' : ''}
            </span>
            <span className="flex-1" />
            {freeKbps == null && (
              <Button size="sm" variant="outline" disabled={!!site.uplink_test?.pending || !site.online} onClick={testUplink}>
                {site.uplink_test?.pending ? 'Testing…' : 'Test the uplink'}
              </Button>
            )}
            {tight && !subOnly && <Button size="sm" variant="primary" onClick={useSubOnly}>Use sub streams only</Button>}
          </div>
        )}
        {picks.length > 0 && (
          <ul className="space-y-2">
            {picks.map((p) => {
              const r = results[p.ip]
              const ch = (r?.channels ?? []).find((c, i) => `${p.ip}#${c.index ?? i}` === p.key)
              return (
                <li key={p.key} className="flex items-start gap-2 border border-[var(--border)] p-2">
                  <input type="checkbox" className="mt-2" checked={p.add} aria-label={`Add ${p.name}`}
                    onChange={(e) => setPicks(picks.map((x) => (x.key === p.key ? { ...x, add: e.target.checked } : x)))} />
                  {ch?.snapshot_b64
                    ? <img src={`data:image/jpeg;base64,${ch.snapshot_b64}`} alt="" width={96} height={54} className="object-cover border border-[var(--border)]" />
                    : <div className="w-24 h-[54px] grid place-items-center border border-[var(--border)] text-[10px] text-[var(--text-dim)]">no preview</div>}
                  <div className="flex-1 space-y-1">
                    <Input value={p.name} aria-label="Camera name"
                      onChange={(e) => setPicks(picks.map((x) => (x.key === p.key ? { ...x, name: e.target.value } : x)))} />
                    <div className="text-xs text-[var(--text-dim)] break-all">
                      {ch?.width ? `${ch.width}×${ch.height} · ` : ''}{p.sub_url ? 'with a sub stream · ' : 'no sub stream · '}{p.main_url}
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </Modal>
  )
}
