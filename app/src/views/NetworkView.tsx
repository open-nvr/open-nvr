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

import { NavLink, useLocation } from 'react-router-dom'
import { useEffect, useState } from 'react'
import { apiService } from '../lib/apiService'
import { useSnackbar } from '../components/Snackbar'
import { useTranslation } from '../i18n'

const SUBTABS = [
  { key: 'camera-lan', label: 'Camera LAN' },
  { key: 'uplink', label: 'Uplink' },
]

export function NetworkView() {
  const { t } = useTranslation()
  const location = useLocation()
  const active = (location.pathname.split('/network/')[1] || '').replace(/\/$/, '') || 'camera-lan'
  const [lan, setLan] = useState<any | null>(null)
  const [uplink, setUplink] = useState<any | null>(null)
  const [whitelist, setWhitelist] = useState<string[]>([])
  const [error, setError] = useState<string | null>(null)
  const [savingLan, setSavingLan] = useState(false)
  const [savingUplink, setSavingUplink] = useState(false)
  const { showSuccess, showError } = useSnackbar()

  const saveLan = async () => {
    setSavingLan(true)
    try {
      await apiService.updateCameraLAN(lan)
      const extra = (lan?.scan_subnets || []).length
      const cidr = lan?.subnet_cidr
      showSuccess(
        cidr
          ? `Camera LAN saved — subnet ${cidr}${extra ? ` (+${extra} scan subnet${extra > 1 ? 's' : ''})` : ''}`
          : 'Camera LAN settings saved'
      )
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to save Camera LAN settings')
    } finally {
      setSavingLan(false)
    }
  }

  const saveUplink = async () => {
    setSavingUplink(true)
    try {
      await apiService.updateUplink(uplink)
      showSuccess('Uplink settings saved')
    } catch (e: any) {
      showError(e?.data?.detail || e?.message || 'Failed to save Uplink settings')
    } finally {
      setSavingUplink(false)
    }
  }

  useEffect(() => {
    ;(async () => {
      try {
        setError(null)
        const [lanRes, uplinkRes] = await Promise.allSettled([
          apiService.getCameraLAN(),
          apiService.getUplink(),
        ])
        if (lanRes.status === 'fulfilled') {
          setLan(lanRes.value.data?.settings || null)
          setWhitelist(lanRes.value.data?.whitelisted_ips || [])
        }
        if (uplinkRes.status === 'fulfilled') setUplink(uplinkRes.value.data?.settings || null)
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load network settings')
      }
    })()
  }, [])
  return (
    <section className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">{t('network.title')}</h1>
      </div>

      {/* Top-style navigation like Configuration tabs */}
      <div className="bg-[var(--accent)] text-white px-3 py-2 text-sm flex items-center gap-4">
        {SUBTABS.map((s) => (
          <NavLink
            key={s.key}
            to={`/network/${s.key}`}
            className={({ isActive }) => `px-2 py-1 rounded ${isActive ? 'bg-white/15' : 'opacity-90 hover:opacity-100'}`}
            end
          >
            {s.key === 'camera-lan' ? t('network.cameraLan') : t('network.uplink')}
          </NavLink>
        ))}
      </div>

      <div className="p-4 bg-[var(--panel)] space-y-4">
        {error && <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-red-300 text-sm">{error}</div>}
        {active === 'camera-lan' ? (
          <div className="space-y-3 text-sm">
            <div className="text-[var(--text-dim)]">{t('network.isolatedDescription')}</div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.interface')}</span>
                <input className="input" defaultValue={lan?.interface_name||''} onBlur={(e)=> setLan((s:any)=>({...s, interface_name:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.dhcp')}</span>
                <select className="select" defaultValue={String(lan?.dhcp_enabled ?? true)} onChange={(e)=> setLan((s:any)=>({...s, dhcp_enabled: e.target.value === 'true'}))}>
                  <option value="true">{t('network.enabled')}</option>
                  <option value="false">{t('network.disabled')}</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.mtu')}</span>
                <input className="input" type="number" defaultValue={lan?.mtu||1500} onBlur={(e)=> setLan((s:any)=>({...s, mtu: Number(e.target.value||1500)}))} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.ipv4')}</span>
                <input className="input" defaultValue={lan?.ipv4_address||''} onBlur={(e)=> setLan((s:any)=>({...s, ipv4_address:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.subnetMask')}</span>
                <input className="input" defaultValue={lan?.ipv4_subnet_mask||''} onBlur={(e)=> setLan((s:any)=>({...s, ipv4_subnet_mask:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.gateway')}</span>
                <input className="input" defaultValue={lan?.ipv4_gateway||''} onBlur={(e)=> setLan((s:any)=>({...s, ipv4_gateway:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[var(--text-dim)]">{t('network.subnetCidr')}</span>
                <input className="input" placeholder="e.g., 192.168.1.0/24" defaultValue={lan?.subnet_cidr||''} onBlur={(e)=> setLan((s:any)=>({...s, subnet_cidr:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1 md:col-span-2">
                <span className="text-[var(--text-dim)]">{t('network.additionalSubnets')} <span className="text-xs">({t('network.commaSeparated')}, e.g. 10.0.0.0/24,172.16.0.0/24)</span></span>
                <input className="input" placeholder="e.g., 10.0.0.0/24,172.16.1.0/24" defaultValue={(lan?.scan_subnets||[]).join(',')} onBlur={(e)=> setLan((s:any)=>({...s, scan_subnets: e.target.value.split(',').map((v:string)=>v.trim()).filter(Boolean)}))} />
              </label>
              <label className="flex flex-col gap-1 md:col-span-3">
                <span className="text-[var(--text-dim)]">{t('network.description')}</span>
                <input className="input" defaultValue={lan?.description||''} onBlur={(e)=> setLan((s:any)=>({...s, description:e.target.value}))} />
              </label>
            </div>
            <div>
              <div className="font-medium mb-1">{t('network.whitelisted')}</div>
              <div className="border border-neutral-700 bg-[var(--panel-2)] p-2 rounded">
                {whitelist.length === 0 ? (
                  <div className="text-[var(--text-dim)]">{t('network.noProvisioned')}</div>
                ) : (
                  <ul className="list-disc ml-5">
                    {whitelist.map((ip)=> <li key={ip}>{ip}</li>)}
                  </ul>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2">
              <button className="btn btn-primary" disabled={savingLan} onClick={saveLan}>{savingLan ? t('common.loading') : t('network.save')}</button>
              {/* <button className="btn" onClick={async ()=>{ await apiService.isolateCameraLAN(); }}>Isolate from internet</button> */}
            </div>
          </div>
        ) : active === 'uplink' ? (
          <div className="space-y-3 text-sm">
            <div className="text-[var(--text-dim)]">{t('network.uplinkDescription')}</div>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
              <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)]">{t('network.interface')}</span>
                <input className="input" defaultValue={uplink?.interface_name||''} onBlur={(e)=> setUplink((s:any)=>({...s, interface_name:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)]">{t('network.dhcp')}</span>
                <select className="select" defaultValue={String(uplink?.dhcp_enabled ?? true)} onChange={(e)=> setUplink((s:any)=>({...s, dhcp_enabled: e.target.value === 'true'}))}>
                  <option value="true">{t('network.enabled')}</option>
                  <option value="false">{t('network.disabled')}</option>
                </select>
              </label>
              <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)]">{t('network.mtu')}</span>
                <input className="input" type="number" defaultValue={uplink?.mtu||1500} onBlur={(e)=> setUplink((s:any)=>({...s, mtu: Number(e.target.value||1500)}))} />
              </label>
              <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)]">{t('network.ipv4')}</span>
                <input className="input" defaultValue={uplink?.ipv4_address||''} onBlur={(e)=> setUplink((s:any)=>({...s, ipv4_address:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)]">{t('network.subnetMask')}</span>
                <input className="input" defaultValue={uplink?.ipv4_subnet_mask||''} onBlur={(e)=> setUplink((s:any)=>({...s, ipv4_subnet_mask:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1">
                  <span className="text-[var(--text-dim)]">{t('network.gateway')}</span>
                <input className="input" defaultValue={uplink?.ipv4_gateway||''} onBlur={(e)=> setUplink((s:any)=>({...s, ipv4_gateway:e.target.value}))} />
              </label>
              <label className="flex flex-col gap-1 md:col-span-3">
                <span className="text-[var(--text-dim)]">{t('network.blacklisted')}</span>
                <input className="input" defaultValue={(uplink?.blacklisted_ips||[]).join(', ')} onBlur={(e)=> setUplink((s:any)=>({...s, blacklisted_ips: e.target.value.split(',').map(v=>v.trim()).filter(Boolean)}))} />
              </label>
              <label className="flex flex-col gap-1 md:col-span-3">
                <span className="text-[var(--text-dim)]">{t('network.description')}</span>
                <input className="input" defaultValue={uplink?.description||''} onBlur={(e)=> setUplink((s:any)=>({...s, description:e.target.value}))} />
              </label>
            </div>
            <div className="flex items-center gap-2">
              <button className="btn btn-primary" disabled={savingUplink} onClick={saveUplink}>{savingUplink ? t('common.loading') : t('network.save')}</button>
            </div>
          </div>
        ) : (
          <div className="text-sm text-[var(--text-dim)]">Select a section.</div>
        )}
      </div>
    </section>
  )
}


