import { useEffect, useMemo, useRef, useState } from 'react'
import Layout from '../components/Layout'
import { camerasService, departmentsService, staffService, type CameraOption, type Department, type Staff as StaffRow } from '../services/admin'
import { BriefcaseBusiness, Building2, Pencil, Plus, Trash2, X as XIcon, Camera, Images } from 'lucide-react'
import { useToast } from '../components/ToastProvider'
import { useConfirm } from '../components/ConfirmProvider'

type StaffForm = { full_name: string; email: string; department_id: string; designation: string }
type DeptForm = { name: string; camera_id: string; work_start: string; work_end: string }
const EMPTY_STAFF: StaffForm = { full_name: '', email: '', department_id: '', designation: '' }
const EMPTY_DEPT: DeptForm = { name: '', camera_id: '', work_start: '09:00', work_end: '18:00' }

export default function Staff() {
  const [staff, setStaff] = useState<StaffRow[]>([])
  const [departments, setDepartments] = useState<Department[]>([])
  const [cameras, setCameras] = useState<CameraOption[]>([])
  const [camerasError, setCamerasError] = useState<string | null>(null)
  const [workers, setWorkers] = useState<any[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const { showToast } = useToast()
  const { confirm } = useConfirm()

  // staff form (enrol with photos, or edit details)
  const [staffOpen, setStaffOpen] = useState(false)
  const [editingStaff, setEditingStaff] = useState<StaffRow | null>(null)
  const [staffForm, setStaffForm] = useState<StaffForm>(EMPTY_STAFF)
  const [photos, setPhotos] = useState<File[]>([])
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [warnings, setWarnings] = useState<string[]>([])
  const photoRef = useRef<HTMLInputElement | null>(null)

  // department form
  const [deptOpen, setDeptOpen] = useState(false)
  const [editingDept, setEditingDept] = useState<Department | null>(null)
  const [deptForm, setDeptForm] = useState<DeptForm>(EMPTY_DEPT)

  const load = async () => {
    try {
      const [s, d] = await Promise.all([staffService.list(), departmentsService.list()])
      setStaff(Array.isArray(s.data) ? s.data : [])
      setDepartments(Array.isArray(d.data) ? d.data : [])
    } finally {
      setLoading(false)
    }
  }
  const loadWorkers = async () => {
    try { const r = await staffService.engineStatus(); setWorkers(r.data?.workers || []) } catch {}
  }
  useEffect(() => {
    load()
    camerasService.list().then(r => { setCameras(Array.isArray(r.data) ? r.data : []); setCamerasError(null) })
      .catch(e => setCamerasError(String(e?.response?.data?.detail || 'Could not list OpenNVR cameras')))
    loadWorkers()
    const id = setInterval(loadWorkers, 10000)
    return () => clearInterval(id)
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return staff
    return staff.filter(s => s.full_name.toLowerCase().includes(q) || s.staff_code.toLowerCase().includes(q) || (s.email || '').toLowerCase().includes(q) || (s.department_name || '').toLowerCase().includes(q))
  }, [staff, query])

  const openStaff = (s: StaffRow | null) => {
    setEditingStaff(s)
    setStaffForm(s ? { full_name: s.full_name, email: s.email || '', department_id: s.department_id != null ? String(s.department_id) : '', designation: s.designation || '' } : EMPTY_STAFF)
    setPhotos([]); setError(null); setWarnings([])
    setStaffOpen(true)
  }

  const saveStaff = async () => {
    if (saving) return
    if (!staffForm.full_name.trim()) { setError('Full name is required'); return }
    if (!editingStaff && photos.length === 0) { setError('Add at least one clear face photo'); return }
    setError(null); setWarnings([])
    try {
      setSaving(true)
      const dep = staffForm.department_id ? Number(staffForm.department_id) : null
      if (editingStaff) {
        await staffService.update(editingStaff.staff_code, { full_name: staffForm.full_name.trim(), email: staffForm.email.trim() || null, department_id: dep, designation: staffForm.designation.trim() || null })
        if (photos.length) {
          const form = new FormData(); photos.forEach(p => form.append('images', p))
          const r = await staffService.registerFaces(editingStaff.staff_code, form)
          setWarnings(r.data?.validation_errors || [])
        }
        showToast('success', 'Staff updated')
      } else {
        const form = new FormData()
        form.append('full_name', staffForm.full_name.trim())
        if (staffForm.email.trim()) form.append('email', staffForm.email.trim())
        if (dep) form.append('department_id', String(dep))
        if (staffForm.designation.trim()) form.append('designation', staffForm.designation.trim())
        photos.forEach(p => form.append('images', p))
        const r = await staffService.registerNew(form)
        showToast('success', `${r.data.staff.full_name} enrolled with ${r.data.num_faces_registered} photo(s)`)
        if (r.data.validation_errors?.length) { setWarnings(r.data.validation_errors); await load(); return }
      }
      await load()
      setStaffOpen(false); setEditingStaff(null)
    } catch (e: any) {
      const d = e?.response?.data?.detail
      setError(typeof d === 'object' && d ? `${d.message}: ${(d.errors || []).join('; ')}` : String(d || e?.message || 'Failed to save'))
    } finally {
      setSaving(false)
    }
  }

  const removeStaff = async (s: StaffRow) => {
    const ok = await confirm({ description: `Delete ${s.full_name} (${s.staff_code}) and their enrolled faces?`, confirmText: 'Delete' })
    if (!ok) return
    try { await staffService.remove(s.staff_code); await load() } catch (e: any) { showToast('error', String(e?.response?.data?.detail || 'Failed to delete')) }
  }

  const openDept = (d: Department | null) => {
    setEditingDept(d)
    setDeptForm(d ? { name: d.name, camera_id: d.camera_id != null ? String(d.camera_id) : '', work_start: d.work_start, work_end: d.work_end } : EMPTY_DEPT)
    setError(null)
    setDeptOpen(true)
  }
  const saveDept = async () => {
    if (saving) return
    const cid = Number(deptForm.camera_id || '0')
    if (!deptForm.name.trim() || !cid) { setError('Name and an OpenNVR camera are required'); return }
    try {
      setSaving(true); setError(null)
      const payload = { name: deptForm.name.trim(), camera_id: cid, work_start: deptForm.work_start, work_end: deptForm.work_end }
      if (editingDept) await departmentsService.update(editingDept.id, payload); else await departmentsService.create(payload)
      await load(); await loadWorkers()
      setDeptOpen(false); setEditingDept(null)
      showToast('success', editingDept ? 'Department updated' : 'Department added')
    } catch (e: any) {
      setError(String(e?.response?.data?.detail || e?.message || 'Failed to save department'))
    } finally {
      setSaving(false)
    }
  }
  const removeDept = async (d: Department) => {
    const ok = await confirm({ description: `Delete department "${d.name}"? Staff stay, unassigned.`, confirmText: 'Delete' })
    if (!ok) return
    try { await departmentsService.remove(d.id); await load(); await loadWorkers() } catch (e: any) { showToast('error', String(e?.response?.data?.detail || 'Failed to delete')) }
  }

  const workerFor = (id: number) => workers.find(w => w.department_id === id)
  const cameraLabel = (c: CameraOption) => `${c.name}${c.handle ? ` (${c.handle})` : ''}`

  return (
    <Layout title="Staff">
      <div className="space-y-4">
        {camerasError && <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 rounded-lg px-4 py-2 text-sm">{camerasError}</div>}

        <div className="bg-white border rounded-lg p-4">
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <div className="flex items-center gap-2 text-base font-medium text-gray-900">
              <Building2 className="w-5 h-5 text-gray-500" />
              <span>Departments</span>
              <span className="text-sm font-normal text-gray-500">— the camera that marks a department's staff, and its working hours</span>
            </div>
            <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => openDept(null)}><Plus className="w-4 h-4" /> Add Department</button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {departments.map(d => {
              const w = workerFor(d.id)
              return (
                <div key={d.id} className="border rounded-lg p-3">
                  <div className="flex items-start justify-between">
                    <div className="min-w-0">
                      <div className="font-semibold text-gray-900">{d.name}</div>
                      <div className="text-xs text-gray-500 flex items-center gap-1 truncate"><Camera className="w-3.5 h-3.5" />{d.camera_name || `Camera #${d.camera_id}`}</div>
                      <div className="text-xs text-gray-600 mt-1">{d.work_start}–{d.work_end} · {staff.filter(s => s.department_id === d.id).length} staff</div>
                      <div className="text-xs mt-1">{w ? <span className={w.active ? 'text-green-700' : 'text-gray-500'}>● {w.active ? 'watching' : 'outside hours'} · {w.polls} looks{w.last_codes?.length ? ` · saw ${w.last_codes.length}` : ''}</span> : <span className="text-gray-400">● starting…</span>}</div>
                    </div>
                    <div className="flex gap-1 ml-2">
                      <button className="p-1.5 rounded bg-gray-100 hover:bg-gray-200" onClick={() => openDept(d)}><Pencil className="w-4 h-4" /></button>
                      <button className="p-1.5 rounded bg-red-50 text-red-700 hover:bg-red-100" onClick={() => removeDept(d)}><Trash2 className="w-4 h-4" /></button>
                    </div>
                  </div>
                </div>
              )
            })}
            {departments.length === 0 && <div className="text-sm text-gray-500 py-4">No departments yet.</div>}
          </div>
        </div>

        <div className="bg-white border rounded-lg p-4" aria-busy={loading}>
          <div className="flex flex-wrap items-center justify-between gap-3 mb-3">
            <div className="flex items-center gap-2 text-base font-medium text-gray-900">
              <BriefcaseBusiness className="w-5 h-5 text-gray-500" />
              <span>Staff</span>
              <span className="text-sm font-normal text-gray-500">— {staff.length} enrolled</span>
            </div>
            <div className="flex items-center gap-2">
              <input className="px-3 py-2 rounded border w-64" placeholder="Search name, code, department" value={query} onChange={e => setQuery(e.target.value)} />
              <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => openStaff(null)}><Plus className="w-4 h-4" /> Enrol Staff</button>
            </div>
          </div>
          <div className="overflow-auto" style={{ maxHeight: '50vh' }}>
            <table className="min-w-full text-sm">
              <thead className="bg-gray-50 sticky top-0">
                <tr>{['Code', 'Name', 'Email', 'Department', 'Designation', ''].map((h, i) => <th key={i} className="px-3 py-1 text-left text-[10px] font-medium text-gray-500 uppercase tracking-wider">{h}</th>)}</tr>
              </thead>
              <tbody className="divide-y">
                {filtered.map(s => (
                  <tr key={s.staff_code}>
                    <td className="px-3 py-1 font-mono text-xs text-gray-500">{s.staff_code}</td>
                    <td className="px-3 py-1 whitespace-nowrap">{s.full_name}</td>
                    <td className="px-3 py-1 text-gray-500">{s.email || '-'}</td>
                    <td className="px-3 py-1 text-gray-500">{s.department_name || '-'}</td>
                    <td className="px-3 py-1 text-gray-500">{s.designation || '-'}</td>
                    <td className="px-3 py-1 text-right whitespace-nowrap">
                      <button className="p-1.5 rounded bg-gray-100 hover:bg-gray-200 mr-1" title="Edit / add photos" onClick={() => openStaff(s)}><Pencil className="w-4 h-4" /></button>
                      <button className="p-1.5 rounded bg-red-50 text-red-700 hover:bg-red-100" title="Delete" onClick={() => removeStaff(s)}><Trash2 className="w-4 h-4" /></button>
                    </td>
                  </tr>
                ))}
                {!loading && filtered.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-gray-500">No staff match.</td></tr>}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {staffOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
            <div className="flex items-start justify-between mb-3">
              <div>
                <div className="text-lg font-semibold">{editingStaff ? 'Edit Staff' : 'Enrol Staff'}</div>
                <div className="text-sm text-gray-500 mt-0.5">{editingStaff ? 'Update details; add photos to improve recognition' : 'Clear, front-facing photos; the face model checks each one'}</div>
              </div>
              <button className="p-1 text-gray-500 hover:text-gray-700" disabled={saving} onClick={() => setStaffOpen(false)}><XIcon className="w-5 h-5" /></button>
            </div>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div><label className="block text-sm text-gray-700 mb-1">Full name</label><input className="w-full rounded border px-3 py-2" value={staffForm.full_name} onChange={e => setStaffForm({ ...staffForm, full_name: e.target.value })} disabled={saving} /></div>
                <div><label className="block text-sm text-gray-700 mb-1">Email</label><input type="email" className="w-full rounded border px-3 py-2" value={staffForm.email} onChange={e => setStaffForm({ ...staffForm, email: e.target.value })} disabled={saving} /></div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Department</label>
                  <select className="w-full rounded border px-3 py-2" value={staffForm.department_id} onChange={e => setStaffForm({ ...staffForm, department_id: e.target.value })} disabled={saving}>
                    <option value="">None</option>
                    {departments.map(d => <option key={d.id} value={String(d.id)}>{d.name}</option>)}
                  </select>
                </div>
                <div><label className="block text-sm text-gray-700 mb-1">Designation</label><input className="w-full rounded border px-3 py-2" placeholder="e.g., Professor" value={staffForm.designation} onChange={e => setStaffForm({ ...staffForm, designation: e.target.value })} disabled={saving} /></div>
              </div>
              <div>
                <label className="block text-sm text-gray-700 mb-1 flex items-center gap-1"><Images className="w-4 h-4" /> Face photos {editingStaff ? '(optional, up to 10)' : '(1 to 10)'}</label>
                <input ref={photoRef} type="file" accept="image/*" multiple className="text-sm" disabled={saving} onChange={e => setPhotos(Array.from(e.target.files || []).slice(0, 10))} />
                {photos.length > 0 && <div className="mt-2 flex gap-2 flex-wrap">{photos.map((p, i) => <img key={i} src={URL.createObjectURL(p)} className="h-16 w-16 object-cover rounded border" />)}</div>}
              </div>
            </div>
            {error && <div className="mt-2 text-sm text-red-600 whitespace-pre-wrap">{error}</div>}
            {warnings.length > 0 && <div className="mt-2 text-xs text-yellow-800 bg-yellow-50 border border-yellow-200 rounded p-2">{warnings.map((w, i) => <div key={i}>{w}</div>)}</div>}
            <div className="mt-4 flex justify-end gap-2">
              <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" disabled={saving} onClick={() => setStaffOpen(false)}>Cancel</button>
              <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50" disabled={saving} onClick={saveStaff}>{saving ? 'Saving…' : (editingStaff ? 'Update' : 'Enrol')}</button>
            </div>
          </div>
        </div>
      )}

      {deptOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-md rounded-lg bg-white border shadow-lg p-5">
            <div className="flex items-start justify-between mb-3">
              <div className="text-lg font-semibold">{editingDept ? 'Edit Department' : 'Add Department'}</div>
              <button className="p-1 text-gray-500 hover:text-gray-700" disabled={saving} onClick={() => setDeptOpen(false)}><XIcon className="w-5 h-5" /></button>
            </div>
            <div className="space-y-3">
              <div><label className="block text-sm text-gray-700 mb-1">Name</label><input className="w-full rounded border px-3 py-2" placeholder="e.g., Physics" value={deptForm.name} onChange={e => setDeptForm({ ...deptForm, name: e.target.value })} disabled={saving} /></div>
              <div>
                <label className="block text-sm text-gray-700 mb-1">OpenNVR camera</label>
                <select className="w-full rounded border px-3 py-2" value={deptForm.camera_id} onChange={e => setDeptForm({ ...deptForm, camera_id: e.target.value })} disabled={saving}>
                  <option value="">Select camera</option>
                  {cameras.map(c => <option key={c.id} value={String(c.id)}>{cameraLabel(c)}</option>)}
                </select>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div><label className="block text-sm text-gray-700 mb-1">Work starts</label><input type="time" className="w-full rounded border px-3 py-2" value={deptForm.work_start} onChange={e => setDeptForm({ ...deptForm, work_start: e.target.value })} disabled={saving} /></div>
                <div><label className="block text-sm text-gray-700 mb-1">Work ends</label><input type="time" className="w-full rounded border px-3 py-2" value={deptForm.work_end} onChange={e => setDeptForm({ ...deptForm, work_end: e.target.value })} disabled={saving} /></div>
              </div>
            </div>
            {error && <div className="mt-2 text-sm text-red-600">{error}</div>}
            <div className="mt-4 flex justify-end gap-2">
              <button className="px-3 py-1.5 rounded-md bg-gray-100 hover:bg-gray-200" disabled={saving} onClick={() => setDeptOpen(false)}>Cancel</button>
              <button className="px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700 disabled:opacity-50" disabled={saving} onClick={saveDept}>{saving ? 'Saving…' : (editingDept ? 'Update' : 'Save')}</button>
            </div>
          </div>
        </div>
      )}
    </Layout>
  )
}
