import { useEffect, useMemo, useRef, useState } from 'react'
import Layout from '../components/Layout'
import { camerasService, roomsService, type CameraOption } from '../services/admin'
import { Plus, Camera, Building2, Pencil, Trash2, X as XIcon } from 'lucide-react'
import { useConfirm } from '../components/ConfirmProvider'
import { messages } from '../messages'

type Room = { id: number; name: string; camera_id: number | null; camera_name: string }

export default function ManageRooms() {
  const [rooms, setRooms] = useState<Room[]>([])
  const [cameras, setCameras] = useState<CameraOption[]>([])
  const [camerasError, setCamerasError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [cameraId, setCameraId] = useState('')
  const [editing, setEditing] = useState<Room | null>(null)
  const [query, setQuery] = useState('')
  const [isFormOpen, setIsFormOpen] = useState(false)

  const [loading, setLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const toastTimeoutRef = useRef<number | null>(null)
  const { confirm } = useConfirm()

  const load = async () => {
    setLoading(true)
    try {
      const res = await roomsService.list()
      const list = Array.isArray(res.data) ? res.data : []
      setRooms(list.map((r: any) => ({
        id: Number(r.id), name: String(r.name),
        camera_id: r.camera_id == null ? null : Number(r.camera_id),
        camera_name: String(r.camera_name || ''),
      })))
    } finally {
      setLoading(false)
    }
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

  const showToast = (type: 'success' | 'error', message: string) => {
    if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current)
    setToast({ type, message })
    toastTimeoutRef.current = window.setTimeout(() => setToast(null), 2500)
  }
  useEffect(() => {
    return () => { if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current) }
  }, [])

  const cameraLabel = (c: CameraOption) => `${c.name}${c.handle ? ` (${c.handle})` : ''}`

  const save = async () => {
    if (isSaving) return
    setError(null)
    const nm = name.trim(); const cid = Number(cameraId || '0')
    if (!nm || !cid) { setError('Name and an OpenNVR camera are required'); return }
    try {
      setIsSaving(true)
      await roomsService.create(nm, cid)
      await load()
      setName(''); setCameraId('')
      setIsFormOpen(false)
      showToast('success', 'Room added')
    } catch (e: any) {
      const msg = String(e?.response?.data?.detail || e?.message || 'Failed to save room')
      setError(msg)
      showToast('error', 'Failed to save room')
    } finally {
      setIsSaving(false)
    }
  }

  const update = async () => {
    if (isSaving || !editing) return
    setError(null)
    const nm = name.trim(); const cid = Number(cameraId || '0')
    if (!nm || !cid) { setError('Name and an OpenNVR camera are required'); return }
    try {
      setIsSaving(true)
      await roomsService.update(editing.id, { name: nm, camera_id: cid })
      await load()
      setEditing(null)
      setName(''); setCameraId('')
      setIsFormOpen(false)
      showToast('success', 'Room updated')
    } catch (e: any) {
      const msg = String(e?.response?.data?.detail || e?.message || 'Failed to update room')
      setError(msg)
      showToast('error', 'Failed to update room')
    } finally {
      setIsSaving(false)
    }
  }

  const remove = async (id: number) => {
    const ok = await confirm({ description: messages.manage.confirmDeleteRoom, confirmText: messages.common.delete })
    if (!ok) return
    await roomsService.remove(id)
    await load()
  }

  const filteredRooms = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return rooms
    return rooms.filter(r => r.name.toLowerCase().includes(q) || (r.camera_name || '').toLowerCase().includes(q))
  }, [rooms, query])

  const openForm = (room: Room | null) => {
    setEditing(room)
    setName(room?.name || '')
    setCameraId(room?.camera_id != null ? String(room.camera_id) : '')
    setError(null)
    setIsFormOpen(true)
  }

  return (
    <Layout title="Manage Rooms">
      {toast && (
        <div className="fixed right-4 top-4 z-[60]">
          <div className={`px-4 py-2 rounded-md shadow border text-sm ${toast.type === 'success' ? 'bg-green-50 text-green-800 border-green-200' : 'bg-red-50 text-red-800 border-red-200'}`}>
            {toast.message}
          </div>
        </div>
      )}
      <div className="space-y-6">
        <div className="bg-white border rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900">
            <Building2 className="w-5 h-5 text-gray-500" />
            <span>Rooms</span>
            <span className="text-sm font-normal text-gray-500">— Each room is one of your OpenNVR cameras</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400">🔎</span>
              <input className="pl-8 pr-3 py-2 rounded border w-64" placeholder="Search room or camera" value={query} onChange={e => setQuery(e.target.value)} />
            </div>
            <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => openForm(null)}><Plus className="w-4 h-4" /> Add Room</button>
          </div>
        </div>

        {camerasError && (
          <div className="bg-yellow-50 border border-yellow-200 text-yellow-800 rounded-lg px-4 py-2 text-sm">{camerasError}</div>
        )}

        <div className="bg-white border rounded-lg p-4" aria-busy={loading}>
          <div className="overflow-auto" style={{ maxHeight: 'calc(100vh - 290px)' }}>
            {loading ? (
              <div className="flex items-center justify-center py-12">
                <div className="inline-flex items-center gap-2 text-sm text-gray-600">
                  <svg className="animate-spin h-4 w-4 text-gray-600" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                  </svg>
                  Loading rooms...
                </div>
              </div>
            ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {filteredRooms.map(r => (
                <div key={r.id} className="border rounded-lg p-4 hover:shadow-sm">
                  <div className="flex items-start justify-between">
                    <div className="min-w-0">
                      <div className="text-base font-semibold text-gray-900">{r.name}</div>
                      <div className="mt-1 text-xs text-gray-500 flex items-center gap-1 min-w-0">
                        <Camera className="w-4 h-4 shrink-0 text-gray-400" />
                        <span className="truncate flex-1 min-w-0" title={r.camera_name}>
                          {r.camera_name || (r.camera_id != null ? `Camera #${r.camera_id}` : 'No camera')}
                        </span>
                      </div>
                    </div>
                    <div className="flex items-center gap-1 ml-2">
                      <button className="inline-flex items-center justify-center p-2 rounded-md bg-gray-100 hover:bg-gray-200" title="Edit" aria-label="Edit" onClick={() => openForm(r)}>
                        <Pencil className="w-4 h-4" />
                      </button>
                      <button className="inline-flex items-center justify-center p-2 rounded-md bg-red-50 text-red-700 hover:bg-red-100" title="Delete" aria-label="Delete" onClick={() => remove(r.id)}>
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
            )}
            {!loading && filteredRooms.length === 0 && (
              <div className="py-8 text-sm text-gray-500 text-center">No rooms match your filters.</div>
            )}
          </div>
        </div>
      </div>

      {isFormOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
          <div className="w-full max-w-md rounded-lg bg-white border shadow-lg p-5">
            <div className="flex items-start justify-between mb-3">
              <div>
                <div className="text-lg font-semibold">{editing ? 'Edit Room' : 'Add Room'}</div>
                <div className="text-sm text-gray-500 mt-0.5">{editing ? 'Update classroom details' : 'Bind a classroom to an OpenNVR camera'}</div>
              </div>
              <button className={`p-1 ${isSaving ? 'text-gray-300 cursor-not-allowed' : 'text-gray-500 hover:text-gray-700'}`} disabled={isSaving} onClick={() => { if (isSaving) return; setIsFormOpen(false); setEditing(null) }}><XIcon className="w-5 h-5" /></button>
            </div>
            <div className="space-y-3">
              <div>
                <label className="block text-sm text-gray-700 mb-1">Name</label>
                <input className="w-full rounded border px-3 py-2" placeholder="e.g., Room 101" disabled={isSaving} value={name} onChange={e => setName(e.target.value)} />
              </div>
              <div>
                <label className="block text-sm text-gray-700 mb-1">OpenNVR camera</label>
                <select className="w-full rounded border px-3 py-2" disabled={isSaving} value={cameraId} onChange={e => setCameraId(e.target.value)}>
                  <option value="">Select camera</option>
                  {cameras.map(c => (
                    <option key={c.id} value={String(c.id)}>{cameraLabel(c)}</option>
                  ))}
                </select>
                {cameras.length === 0 && <div className="mt-1 text-xs text-gray-500">No cameras found. Add cameras in OpenNVR first.</div>}
              </div>
            </div>
            {error && <div className="mt-2 text-sm text-red-600">{error}</div>}
            <div className="mt-4 flex justify-end gap-2">
              <button className={`px-3 py-1.5 rounded-md ${isSaving ? 'bg-gray-100 text-gray-400 cursor-not-allowed' : 'bg-gray-100 hover:bg-gray-200'}`} disabled={isSaving} onClick={() => { if (isSaving) return; setIsFormOpen(false); setEditing(null) }}>Cancel</button>
              <button className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md ${!isSaving ? 'bg-indigo-600 text-white hover:bg-indigo-700' : 'bg-gray-200 text-gray-500 cursor-not-allowed'}`} disabled={isSaving} onClick={editing ? update : save}>
                {isSaving && <span className="inline-block h-4 w-4 border-2 border-white/70 border-t-transparent rounded-full animate-spin" />}
                {isSaving ? (editing ? 'Updating…' : 'Saving…') : (editing ? 'Update' : 'Save')}
              </button>
            </div>
          </div>
        </div>
      )}
    </Layout>
  )
}
