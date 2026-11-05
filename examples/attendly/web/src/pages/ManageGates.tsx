import { useEffect, useMemo, useState } from 'react'
import Layout from '../components/Layout'
import { camerasService, gatesService, type AccessEvent, type CameraOption, type Gate, type Presence } from '../services/admin'
import { DoorOpen, Pencil, Plus, Trash2, X as XIcon, Radio, Bell, Users, LogIn, LogOut } from 'lucide-react'
import { useConfirm } from '../components/ConfirmProvider'
import { useToast } from '../components/ToastProvider'

type Form = { name: string; kind: 'hostel' | 'mess'; camera_id: string; hostel_block: string; allow_staff: boolean; enabled: boolean; mode: 'barrier' | 'monitor'; direction: 'in' | 'out' | 'toggle'; alert_on: 'all' | 'deny' | 'none'; open_url: string; meal_windows: string }
const EMPTY: Form = { name: '', kind: 'hostel', camera_id: '', hostel_block: '', allow_staff: false, enabled: true, mode: 'barrier', direction: 'in', alert_on: 'all', open_url: '', meal_windows: '' }

const REASONS: Record<string, string> = {
  hostler: 'Hostler of this block', booked: 'Meal booked', staff: 'Staff', unknown: 'Face not recognised',
  day_scholar: 'Day scholar', wrong_block: 'Another block', not_booked: 'No booking for this meal',
  already_used: 'Booking already used', no_meal_window: 'Outside meal hours', staff_not_allowed: 'Staff not allowed here',
  no_face: 'No face in picture',
}

export default function ManageGates() {
  const [gates, setGates] = useState<Gate[]>([])
  const [cameras, setCameras] = useState<CameraOption[]>([])
  const [camerasError, setCamerasError] = useState<string | null>(null)
  const [status, setStatus] = useState<any>(null)
  const [events, setEvents] = useState<AccessEvent[]>([])
  const [filterGate, setFilterGate] = useState<string>('')
  const [presence, setPresence] = useState<{ summary: Record<string, { in: number; out: number }>; rows: Presence[] } | null>(null)
  const [presenceStatus, setPresenceStatus] = useState<'in' | 'out'>('in')
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState<Form>(EMPTY)
  const [editing, setEditing] = useState<Gate | null>(null)
  const [isFormOpen, setIsFormOpen] = useState(false)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const { confirm } = useConfirm()
  const { showToast } = useToast()

  const load = async () => {
    try {
      const res = await gatesService.list()
      setGates(Array.isArray(res.data) ? res.data : [])
    } finally {
      setLoading(false)
    }
  }
  const loadLive = async () => {
    try {
      const [st, ev, pr] = await Promise.all([
        gatesService.status(),
        gatesService.events({ limit: 60, ...(filterGate ? { gate_id: Number(filterGate) } : {}) }),
        gatesService.presence({ status: presenceStatus }),
      ])
      setStatus(st.data)
      setEvents(Array.isArray(ev.data) ? ev.data : [])
      setPresence(pr.data)
    } catch {}
  }
  const loadCameras = async () => {
    try {
      const res = await camerasService.list()
      setCameras(Array.isArray(res.data) ? res.data : [])
      setCamerasError(null)
    } catch (e: any) {
      setCameras([])
      setCamerasError(String(e?.response?.data?.detail || 'Could not list OpenNVR cameras'))
    }
  }

  useEffect(() => { load(); loadCameras() }, [])
  useEffect(() => {
    loadLive()
    const id = setInterval(loadLive, 3000)
    return () => clearInterval(id)
  }, [filterGate, presenceStatus])

  const workerFor = (id: number) => (status?.workers || []).find((w: any) => w.gate_id === id)

  const openForm = (g: Gate | null) => {
    setEditing(g)
    setForm(g ? {
      name: g.name, kind: g.kind, camera_id: g.camera_id != null ? String(g.camera_id) : '', hostel_block: g.hostel_block || '',
      allow_staff: !!g.allow_staff, enabled: !!g.enabled, mode: g.mode || 'barrier', direction: g.direction || 'in', alert_on: g.alert_on || 'all',
      open_url: g.open_url || '', meal_windows: g.meal_windows || '',
    } : EMPTY)
    setError(null)
    setIsFormOpen(true)
  }

  const save = async () => {
    if (isSaving) return
    const cid = Number(form.camera_id || '0')
    if (!form.name.trim() || !cid) { setError('Name and an OpenNVR camera are required'); return }
    const payload = {
      name: form.name.trim(), kind: form.kind, camera_id: cid, hostel_block: form.kind === 'hostel' ? form.hostel_block.trim() : '',
      allow_staff: form.allow_staff, enabled: form.enabled, mode: form.mode, direction: form.kind === 'hostel' ? form.direction : 'in', alert_on: form.alert_on,
      open_url: form.open_url.trim(), meal_windows: form.kind === 'mess' ? form.meal_windows.trim() : '',
    }
    try {
      setIsSaving(true)
      if (editing) await gatesService.update(editing.id, payload)
      else await gatesService.create(payload)
      await load(); await loadLive()
      setIsFormOpen(false); setEditing(null)
      showToast('success', editing ? 'Gate updated' : 'Gate added')
    } catch (e: any) {
      setError(String(e?.response?.data?.detail || e?.message || 'Failed to save gate'))
    } finally {
      setIsSaving(false)
    }
  }

  const toggle = async (g: Gate) => {
    try {
      await gatesService.update(g.id, { enabled: !g.enabled })
      await load(); await loadLive()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to update gate'))
    }
  }

  const remove = async (g: Gate) => {
    const ok = await confirm({ description: `Delete gate "${g.name}"? Its decision log stays.`, confirmText: 'Delete' })
    if (!ok) return
    try {
      await gatesService.remove(g.id)
      await load(); await loadLive()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to delete gate'))
    }
  }

  const cameraLabel = (c: CameraOption) => `${c.name}${c.handle ? ` (${c.handle})` : ''}`
  const correct = async (p: Presence) => {
    try {
      await gatesService.setPresence(p.student_code, p.status === 'in' ? 'out' : 'in')
      await loadLive()
    } catch (e: any) {
      showToast('error', String(e?.response?.data?.detail || 'Failed to update'))
    }
  }
  const DIRECTION: Record<string, string> = { in: 'entry', out: 'exit', toggle: 'entry & exit' }
  const mealWindows = useMemo(() => Object.entries(status?.meal_windows || {}).map(([m, w]: any) => `${m} ${w[0]}–${w[1]}`).join(' · '), [status])

  return (
    <Layout title="Gates">
      <div className="space-y-4">
        <div className="bg-white border rounded-lg p-4 flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900">
            <DoorOpen className="w-5 h-5 text-gray-500" />
            <span>Flap barriers</span>
            <span className="text-sm font-normal text-gray-500">— a hostel block or the mess, watched by one OpenNVR camera</span>
          </div>
          <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => openForm(null)}><Plus className="w-4 h-4" /> Add Gate</button>
        </div>

        {camerasError && <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 rounded-lg px-4 py-2 text-sm">{camerasError}</div>}

        {status && (
          <div className="grid gap-3 sm:grid-cols-4 text-sm">
            <div className="bg-green-50 border border-green-200 rounded-lg p-3"><div className="text-2xl font-bold text-green-700">{status.today?.allowed ?? 0}</div><div className="text-gray-600">Allowed today</div></div>
            <div className="bg-red-50 border border-red-200 rounded-lg p-3"><div className="text-2xl font-bold text-red-700">{status.today?.denied ?? 0}</div><div className="text-gray-600">Denied today</div></div>
            <div className={`border rounded-lg p-3 ${status.bus?.configured ? 'bg-blue-50 border-blue-200' : 'bg-gray-50 border-gray-200'}`}>
              <div className="flex items-center gap-2 text-blue-700"><Radio className="w-4 h-4" /><span className="font-semibold">{status.bus?.configured ? `${status.bus.published} on the bus` : 'Bus not configured'}</span></div>
              <div className="text-gray-600 text-xs mt-1">{status.bus?.configured ? `access.decided.v1 → gate-controller · ${status.bus.alerts ?? 0} alerts raised` : 'Set NATS_URL for barriers and alerts'}{status.bus?.last_error ? ` · ${status.bus.last_error}` : ''}</div>
            </div>
            <div className="bg-gray-50 border border-gray-200 rounded-lg p-3"><div className="font-semibold text-gray-800">Meal hours</div><div className="text-gray-600 text-xs mt-1">{mealWindows || '—'}</div></div>
          </div>
        )}

        <div className="bg-white border rounded-lg p-4" aria-busy={loading}>
          {loading ? <div className="py-8 text-center text-sm text-gray-500">Loading gates…</div> : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {gates.map(g => {
                const w = workerFor(g.id)
                return (
                  <div key={g.id} className={`border rounded-lg p-4 ${g.enabled ? '' : 'opacity-60'}`}>
                    <div className="flex items-start justify-between">
                      <div className="min-w-0">
                        <div className="text-base font-semibold text-gray-900 flex items-center gap-2">
                          {g.name}
                          <span className={`text-[10px] uppercase px-1.5 py-0.5 rounded ${g.kind === 'mess' ? 'bg-amber-100 text-amber-800' : 'bg-indigo-100 text-indigo-800'}`}>{g.kind}</span>
                          <span className={`text-[10px] uppercase px-1.5 py-0.5 rounded ${g.mode === 'monitor' ? 'bg-sky-100 text-sky-800' : 'bg-gray-100 text-gray-700'}`} title={g.mode === 'monitor' ? 'No barrier: sightings raise alerts' : 'Decisions drive the barrier'}>{g.mode === 'monitor' ? 'monitor' : 'barrier'}</span>
                        </div>
                        <div className="mt-1 text-xs text-gray-500 truncate">{g.camera_name || (g.camera_id != null ? `Camera #${g.camera_id}` : 'No camera')}</div>
                        <div className="mt-1 text-xs text-gray-600">
                          {g.kind === 'hostel' ? (g.hostel_block ? `Block ${g.hostel_block}` : 'Every hostler') : (g.meal_windows || 'Default meal hours')}
                          {g.kind === 'hostel' ? ` · ${DIRECTION[g.direction] || g.direction}` : ''}
                          {g.allow_staff ? ' · staff allowed' : ''}
                          {g.open_url ? ' · HTTP trigger' : ''}
                          {g.alert_on === 'none' ? ' · no alerts' : g.alert_on === 'deny' ? ' · alerts on deny' : ' · alerts'}
                        </div>
                        <div className="mt-2 text-xs">
                          {w ? (
                            <span className={w.running ? 'text-green-700' : 'text-gray-500'}>
                              ● {w.running ? 'watching' : 'stopped'} · {w.polls} looks · {w.decisions} decisions
                              {w.last_event ? ` · last: ${w.last_event.decision} (${w.last_event.person_name || REASONS[w.last_event.reason] || w.last_event.reason})` : ''}
                              {w.last_error ? <span className="text-red-600"> · {w.last_error}</span> : null}
                            </span>
                          ) : <span className="text-gray-400">● {g.enabled ? 'starting…' : 'disabled'}</span>}
                        </div>
                      </div>
                      <div className="flex items-center gap-1 ml-2">
                        <button className={`px-2 py-1 text-xs rounded ${g.enabled ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-600'}`} onClick={() => toggle(g)}>{g.enabled ? 'On' : 'Off'}</button>
                        <button className="p-2 rounded-md bg-gray-100 hover:bg-gray-200" title="Edit" onClick={() => openForm(g)}><Pencil className="w-4 h-4" /></button>
                        <button className="p-2 rounded-md bg-red-50 text-red-700 hover:bg-red-100" title="Delete" onClick={() => remove(g)}><Trash2 className="w-4 h-4" /></button>
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
          {!loading && gates.length === 0 && <div className="py-8 text-sm text-gray-500 text-center">No gates yet. Add one per hostel block entrance and one for the mess.</div>}
        </div>

        <div className="bg-white border rounded-lg">
          <div className="px-4 py-3 border-b flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium text-gray-800 flex items-center gap-2"><Users className="w-4 h-4" /> Hostel presence
              <span className="font-normal text-gray-500">{Object.entries(presence?.summary || {}).map(([b, c]) => `Block ${b}: ${c.in} in / ${c.out} out`).join(' · ') || '— no sightings yet'}</span>
            </div>
            <div className="flex gap-1 text-xs">
              {(['in', 'out'] as const).map(st => <button key={st} className={`px-2 py-1 rounded ${presenceStatus === st ? 'bg-indigo-600 text-white' : 'bg-gray-100 text-gray-700'}`} onClick={() => setPresenceStatus(st)}>{st === 'in' ? 'Who is in' : 'Who is out'}</button>)}
            </div>
          </div>
          <div className="overflow-auto" style={{ maxHeight: '30vh' }}>
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 sticky top-0"><tr>{['Student', 'Block', 'Status', 'Since', 'Gate', ''].map((h, i) => <th key={i} className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">{h}</th>)}</tr></thead>
              <tbody className="divide-y">
                {(presence?.rows || []).map(p => (
                  <tr key={p.student_code}>
                    <td className="px-3 py-1 whitespace-nowrap">{p.student?.full_name || p.student_code} <span className="text-xs text-gray-400">{p.student_code}</span></td>
                    <td className="px-3 py-1 text-gray-600">{p.hostel_block || '-'}</td>
                    <td className="px-3 py-1"><span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs ${p.status === 'in' ? 'bg-green-100 text-green-800' : 'bg-gray-100 text-gray-700'}`}>{p.status === 'in' ? <LogIn className="w-3 h-3" /> : <LogOut className="w-3 h-3" />}{p.status}</span></td>
                    <td className="px-3 py-1 text-gray-500 whitespace-nowrap">{new Date(p.since).toLocaleString()}</td>
                    <td className="px-3 py-1 text-gray-500">{p.gate_name || '-'}</td>
                    <td className="px-3 py-1 text-right"><button className="text-xs px-2 py-0.5 rounded bg-gray-100 hover:bg-gray-200" title="Correct a missed sighting" onClick={() => correct(p)}>mark {p.status === 'in' ? 'out' : 'in'}</button></td>
                  </tr>
                ))}
                {(presence?.rows || []).length === 0 && <tr><td colSpan={6} className="px-3 py-4 text-center text-gray-500">Nobody is {presenceStatus} right now.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>

        <div className="bg-white border rounded-lg">
          <div className="px-4 py-3 border-b flex items-center justify-between">
            <div className="text-sm font-medium text-gray-800">Decisions (live)</div>
            <select className="rounded border px-2 py-1 text-sm" value={filterGate} onChange={e => setFilterGate(e.target.value)}>
              <option value="">All gates</option>
              {gates.map(g => <option key={g.id} value={String(g.id)}>{g.name}</option>)}
            </select>
          </div>
          <div className="overflow-auto" style={{ maxHeight: '45vh' }}>
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 sticky top-0">
                <tr>
                  {['Time', 'Gate', 'Person', 'Decision', 'Why', 'Movement', 'Similarity', 'Barrier', 'Alert'].map(h => <th key={h} className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">{h}</th>)}
                </tr>
              </thead>
              <tbody className="divide-y">
                {events.map(e => (
                  <tr key={e.id}>
                    <td className="px-3 py-1 whitespace-nowrap text-gray-500">{new Date(e.ts).toLocaleTimeString()} <span className="text-gray-400">{e.ts.slice(0, 10)}</span></td>
                    <td className="px-3 py-1 whitespace-nowrap">{e.gate_name}</td>
                    <td className="px-3 py-1 whitespace-nowrap">{e.person_name || <span className="text-gray-400">unknown</span>}{e.person_code ? <span className="text-gray-400 text-xs"> {e.person_code}</span> : null}</td>
                    <td className="px-3 py-1"><span className={`px-2 py-0.5 rounded text-xs ${e.decision === 'allow' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'}`}>{e.decision}</span></td>
                    <td className="px-3 py-1 text-gray-600">{REASONS[e.reason] || e.reason}{e.unit ? ` (${e.unit})` : ''}</td>
                    <td className="px-3 py-1">{e.movement ? <span className={`inline-flex items-center gap-1 text-xs ${e.movement === 'in' ? 'text-green-700' : 'text-gray-700'}`}>{e.movement === 'in' ? <LogIn className="w-3 h-3" /> : <LogOut className="w-3 h-3" />}{e.movement === 'in' ? 'came in' : 'went out'}</span> : '-'}</td>
                    <td className="px-3 py-1 text-gray-500">{e.similarity != null ? `${Math.round(e.similarity * 100)}%` : '-'}</td>
                    <td className="px-3 py-1 text-gray-500">{e.decision === 'allow' ? (e.actuated ? 'opened' : 'not sent') : '-'}{e.source === 'check' ? ' · check' : ''}</td>
                    <td className="px-3 py-1 text-gray-500">{e.alerted ? <Bell className="w-3.5 h-3.5 text-amber-600" /> : '-'}</td>
                  </tr>
                ))}
                {events.length === 0 && <tr><td colSpan={9} className="px-3 py-6 text-center text-gray-500">No decisions yet.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {isFormOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
            <div className="flex items-start justify-between mb-3">
              <div>
                <div className="text-lg font-semibold">{editing ? 'Edit Gate' : 'Add Gate'}</div>
                <div className="text-sm text-gray-500 mt-0.5">The barrier itself is wired in OpenNVR's gate-controller app under this camera's handle.</div>
              </div>
              <button className="p-1 text-gray-500 hover:text-gray-700" disabled={isSaving} onClick={() => { setIsFormOpen(false); setEditing(null) }}><XIcon className="w-5 h-5" /></button>
            </div>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Name</label>
                  <input className="w-full rounded border px-3 py-2" placeholder="e.g., Block B entrance" value={form.name} onChange={e => setForm({ ...form, name: e.target.value })} disabled={isSaving} />
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Kind</label>
                  <select className="w-full rounded border px-3 py-2" value={form.kind} onChange={e => setForm({ ...form, kind: e.target.value as any })} disabled={isSaving}>
                    <option value="hostel">Hostel block</option>
                    <option value="mess">Mess (meal bookings)</option>
                  </select>
                </div>
              </div>
              <div className="grid grid-cols-3 gap-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Mode</label>
                  <select className="w-full rounded border px-3 py-2" value={form.mode} onChange={e => setForm({ ...form, mode: e.target.value as any })} disabled={isSaving}>
                    <option value="barrier">Flap barrier</option>
                    <option value="monitor">Monitor only</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Direction</label>
                  <select className="w-full rounded border px-3 py-2" value={form.direction} onChange={e => setForm({ ...form, direction: e.target.value as any })} disabled={isSaving || form.kind !== 'hostel'}>
                    <option value="in">Entry (in)</option>
                    <option value="out">Exit (out)</option>
                    <option value="toggle">Both (alternate)</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">OpenNVR alerts</label>
                  <select className="w-full rounded border px-3 py-2" value={form.alert_on} onChange={e => setForm({ ...form, alert_on: e.target.value as any })} disabled={isSaving}>
                    <option value="all">Every sighting</option>
                    <option value="deny">Only refusals</option>
                    <option value="none">None</option>
                  </select>
                </div>
              </div>
              <p className="text-xs text-gray-500">{form.mode === 'monitor' ? 'No barrier here: nobody is stopped. Each student seen is logged as in or out and an alert names them in OpenNVR.' : 'Decisions are published for the gate-controller app, which opens the barrier on allow.'}</p>
              <div>
                <label className="block text-sm text-gray-700 mb-1">OpenNVR camera at the {form.mode === 'monitor' ? 'door' : 'barrier'}</label>
                <select className="w-full rounded border px-3 py-2" value={form.camera_id} onChange={e => setForm({ ...form, camera_id: e.target.value })} disabled={isSaving}>
                  <option value="">Select camera</option>
                  {cameras.map(c => <option key={c.id} value={String(c.id)}>{cameraLabel(c)}</option>)}
                </select>
              </div>
              {form.kind === 'hostel' ? (
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Hostel block <span className="text-gray-400">(blank = every hostler)</span></label>
                  <input className="w-full rounded border px-3 py-2" placeholder="e.g., Block B" value={form.hostel_block} onChange={e => setForm({ ...form, hostel_block: e.target.value })} disabled={isSaving} list="hostel-blocks" />
                  <datalist id="hostel-blocks">{(status?.hostel_blocks || []).map((b: string) => <option key={b} value={b} />)}</datalist>
                </div>
              ) : (
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Meal hours <span className="text-gray-400">(blank = default)</span></label>
                  <input className="w-full rounded border px-3 py-2 font-mono text-sm" placeholder="breakfast=07:00-09:30,lunch=12:00-14:30,dinner=19:00-21:30" value={form.meal_windows} onChange={e => setForm({ ...form, meal_windows: e.target.value })} disabled={isSaving} />
                </div>
              )}
              <div>
                <label className="block text-sm text-gray-700 mb-1">HTTP trigger URL <span className="text-gray-400">(optional, for a barrier with its own relay)</span></label>
                <input className="w-full rounded border px-3 py-2 font-mono text-sm" placeholder="http://192.168.1.50/relay/0?turn=on" value={form.open_url} onChange={e => setForm({ ...form, open_url: e.target.value })} disabled={isSaving} />
              </div>
              <div className="flex gap-6 text-sm">
                <label className="inline-flex items-center gap-2"><input type="checkbox" checked={form.allow_staff} onChange={e => setForm({ ...form, allow_staff: e.target.checked })} /> Staff may pass</label>
                <label className="inline-flex items-center gap-2"><input type="checkbox" checked={form.enabled} onChange={e => setForm({ ...form, enabled: e.target.checked })} /> Enabled</label>
              </div>
            </div>
            {error && <div className="mt-2 text-sm text-red-600">{error}</div>}
            <div className="mt-4 flex justify-end gap-2">
              <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" disabled={isSaving} onClick={() => { setIsFormOpen(false); setEditing(null) }}>Cancel</button>
              <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50" disabled={isSaving} onClick={save}>{isSaving ? 'Saving…' : (editing ? 'Update' : 'Save')}</button>
            </div>
          </div>
        </div>
      )}
    </Layout>
  )
}
