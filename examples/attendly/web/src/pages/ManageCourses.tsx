import { useEffect, useMemo, useRef, useState } from 'react'
import Layout from '../components/Layout'
import { coursesService } from '../services/admin'
import { Plus, BookOpen, Pencil, Trash2, X as XIcon, Tags } from 'lucide-react'
import { useConfirm } from '../components/ConfirmProvider'
import { messages } from '../messages'

type Course = { code: string; name?: string; sections_list?: string[] }

export default function ManageCourses() {
  const [courses, setCourses] = useState<Course[]>([])
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [sections, setSections] = useState<string[]>([])
  const [newSection, setNewSection] = useState('')
  const [editing, setEditing] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [isFormOpen, setIsFormOpen] = useState(false)
  const [loading, setLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [toast, setToast] = useState<{ type: 'success' | 'error'; message: string } | null>(null)
  const toastTimeoutRef = useRef<number | null>(null)
  const { confirm } = useConfirm()

  const load = async () => {
    try { setLoading(true) } catch {}
    const res = await coursesService.list()
    const list = Array.isArray(res.data) ? res.data : []
    setCourses(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined, sections_list: Array.isArray(c.sections_list) ? c.sections_list.map(String) : undefined })))
    try { setLoading(false) } catch {}
  }

  useEffect(() => { load() }, [])

  // Toast helper
  const showToast = (type: 'success' | 'error', message: string) => {
    if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current)
    setToast({ type, message })
    toastTimeoutRef.current = window.setTimeout(() => setToast(null), 2500)
  }
  useEffect(() => {
    return () => {
      if (toastTimeoutRef.current) window.clearTimeout(toastTimeoutRef.current)
    }
  }, [])

  const addSection = () => {
    const s = newSection.trim()
    if (!s) return
    if (sections.includes(s)) return
    setSections(prev => [...prev, s])
    setNewSection('')
  }

  const removeSection = (s: string) => {
    setSections(prev => prev.filter(x => x !== s))
  }

  const save = async () => {
    if (isSaving) return
    setError(null)
    const c = code.trim()
    const nm = name.trim() || undefined
    if (!c) { setError('Course code is required'); return }
    const list = Array.from(new Set(sections.map(s => s.trim()).filter(Boolean)))
    try {
      setIsSaving(true)
      await coursesService.create(c, nm, list)
      await load()
      setCode(''); setName(''); setSections([]); setNewSection(''); setEditing(null)
      setIsFormOpen(false)
      showToast('success', 'Course added')
    } catch (e: any) {
      const errorDetail = e?.response?.data?.detail || e?.response?.data?.message || e?.message || 'Failed to save course'
      let errorMessage = 'Failed to save course'
      let toastMessage = 'Failed to save course'
      
      // Check for specific error types
      if (typeof errorDetail === 'string') {
        if (errorDetail.toLowerCase().includes('already exists') || 
            errorDetail.toLowerCase().includes('duplicate') ||
            errorDetail.toLowerCase().includes('unique constraint')) {
          errorMessage = `Course code "${code}" already exists. Please use a different course code.`
          toastMessage = 'Course code already exists'
        } else {
          errorMessage = errorDetail
          toastMessage = errorDetail
        }
      }
      
      setError(errorMessage)
      showToast('error', toastMessage)
    } finally {
      setIsSaving(false)
    }
  }

  const startEdit = (c: Course) => {
    setEditing(c.code)
    setCode(c.code)
    setName(c.name || '')
    setSections([...(c.sections_list || [])])
    setNewSection('')
  }

  const update = async () => {
    if (isSaving) return
    if (!editing) return
    setError(null)
    const nm = name.trim() || undefined
    const list = Array.from(new Set(sections.map(s => s.trim()).filter(Boolean)))
    try {
      setIsSaving(true)
      await coursesService.update(editing, { name: nm, sections_list: list })
      await load()
      setEditing(null)
      setCode(''); setName(''); setSections([]); setNewSection('')
      setIsFormOpen(false)
      showToast('success', 'Course updated')
    } catch (e: any) {
      const errorDetail = e?.response?.data?.detail || e?.response?.data?.message || e?.message || 'Failed to update course'
      let errorMessage = 'Failed to update course'
      let toastMessage = 'Failed to update course'
      
      // Check for specific error types
      if (typeof errorDetail === 'string') {
        if (errorDetail.toLowerCase().includes('already exists') || 
            errorDetail.toLowerCase().includes('duplicate') ||
            errorDetail.toLowerCase().includes('unique constraint')) {
          errorMessage = `Course code "${code}" already exists. Please use a different course code.`
          toastMessage = 'Course code already exists'
        } else {
          errorMessage = errorDetail
          toastMessage = errorDetail
        }
      }
      
      setError(errorMessage)
      showToast('error', toastMessage)
    } finally {
      setIsSaving(false)
    }
  }

  const remove = async (courseCode: string) => {
    const ok = await confirm({ description: messages.manage.confirmDeleteCourse, confirmText: messages.common.delete })
    if (!ok) return
    await coursesService.remove(courseCode)
    await load()
  }

  const filteredCourses = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return courses
    return courses.filter(c => c.code.toLowerCase().includes(q) || (c.name || '').toLowerCase().includes(q))
  }, [courses, query])

  return (
    <Layout title="Manage Courses">
      {toast && (
        <div className="fixed right-4 top-4 z-[60]">
          <div className={`px-4 py-2 rounded-md shadow border text-sm ${toast.type === 'success' ? 'bg-green-50 text-green-800 border-green-200' : 'bg-red-50 text-red-800 border-red-200'}`}>
            {toast.message}
          </div>
        </div>
      )}
      <div className="space-y-6">
        {/* Toolbar */}
        <div className="bg-white border rounded-lg p-4 flex items-center justify-between">
          <div className="flex items-center gap-2 text-base font-medium text-gray-900">
            <BookOpen className="w-5 h-5 text-gray-500" />
            <span>Courses</span>
            <span className="text-sm font-normal text-gray-500">— Manage courses and sections</span>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative">
              <span className="absolute left-2 top-1/2 -translate-y-1/2 text-gray-400">🔎</span>
              <input className="pl-8 pr-3 py-2 rounded border w-64" placeholder="Search course code or name" value={query} onChange={e => setQuery(e.target.value)} />
            </div>
            <button className="inline-flex items-center gap-2 px-3 py-1.5 rounded-md bg-indigo-600 text-white hover:bg-indigo-700" onClick={() => { setEditing(null); setCode(''); setName(''); setSections([]); setIsFormOpen(true) }}><Plus className="w-4 h-4" /> Add Course</button>
          </div>
        </div>

        {/* Courses list */}
        <div className="bg-white border rounded-lg p-4" aria-busy={loading}>
          <div className="overflow-auto" style={{ maxHeight: 'calc(100vh - 290px)' }}>
            {loading ? (
              <div className="flex items-center justify-center py-12">
                <div className="inline-flex items-center gap-2 text-sm text-gray-600">
                  <svg className="animate-spin h-4 w-4 text-gray-600" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z"></path>
                  </svg>
                  Loading courses...
                </div>
              </div>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  {filteredCourses.map(c => (
                    <div key={c.code} className="border rounded-lg p-4 hover:shadow-sm">
                      <div className="flex items-start justify-between">
                        <div className="min-w-0">
                          <div className="font-mono font-medium truncate" title={c.code}>{c.code}</div>
                          {c.name && (
                            <div className="text-sm text-gray-600 break-words">{c.name}</div>
                          )}
                        </div>
                        <div className="flex items-center gap-1 ml-2">
                          <button
                            className="inline-flex items-center justify-center p-2 rounded-md bg-gray-100 hover:bg-gray-200"
                            title="Edit"
                            aria-label="Edit"
                            onClick={() => { startEdit(c); setIsFormOpen(true) }}
                          >
                            <Pencil className="w-4 h-4" />
                          </button>
                          <button
                            className="inline-flex items-center justify-center p-2 rounded-md bg-red-50 text-red-700 hover:bg-red-100"
                            title="Delete"
                            aria-label="Delete"
                            onClick={() => remove(c.code)}
                          >
                            <Trash2 className="w-4 h-4" />
                          </button>
                        </div>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-1">
                        {(c.sections_list || []).map(s => (
                          <span key={s} className="px-2 py-0.5 text-xs rounded-full bg-gray-100 text-gray-700 inline-flex items-center gap-1"><Tags className="w-3 h-3" />{s}</span>
                        ))}
                        {(c.sections_list || []).length === 0 && <span className="text-xs text-gray-500">No sections</span>}
                      </div>
                    </div>
                  ))}
                </div>
                {filteredCourses.length === 0 && (
                  <div className="py-8 text-sm text-gray-500 text-center">No courses match your search.</div>
                )}
              </>
            )}
          </div>
        </div>

        {/* Add/Edit Course Modal */}
        {isFormOpen && (
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
            <div className="w-full max-w-lg rounded-lg bg-white border shadow-lg p-5">
              <div className="flex items-start justify-between mb-3">
                <div>
                  <div className="text-lg font-semibold">{editing ? 'Edit Course' : 'Add Course'}</div>
                  <div className="text-sm text-gray-500 mt-0.5">Define course details and sections</div>
                </div>
                <button className={`p-1 ${isSaving ? 'text-gray-300 cursor-not-allowed' : 'text-gray-500 hover:text-gray-700'}`} disabled={isSaving} onClick={() => { if (isSaving) return; setIsFormOpen(false) }}><XIcon className="w-5 h-5" /></button>
              </div>
              <div className="space-y-3">
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Course Code</label>
                  <input className="w-full rounded border px-3 py-2" disabled={!!editing || isSaving} value={code} onChange={e => setCode(e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Course Name</label>
                  <input className="w-full rounded border px-3 py-2" disabled={isSaving} value={name} onChange={e => setName(e.target.value)} />
                </div>
                <div>
                  <label className="block text-sm text-gray-700 mb-1">Sections</label>
                  <div className="flex gap-2">
                    <input className="flex-1 rounded border px-3 py-2" placeholder="Section name (e.g., A, B, C1)" disabled={isSaving} value={newSection} onChange={e => setNewSection(e.target.value)} />
                    <button className={`px-3 py-2 rounded-md ${isSaving ? 'bg-gray-100 text-gray-400 cursor-not-allowed' : 'bg-gray-100 hover:bg-gray-200'}`} disabled={isSaving} onClick={addSection}>Add</button>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2">
                    {sections.map(s => (
                      <span key={s} className="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-gray-100 text-gray-800 text-xs">
                        <Tags className="w-3 h-3" />{s}
                        <button className={`text-gray-500 ${isSaving ? 'cursor-not-allowed opacity-50' : 'hover:text-gray-700'}`} disabled={isSaving} onClick={() => removeSection(s)}>×</button>
                      </span>
                    ))}
                    {sections.length === 0 && <div className="text-xs text-gray-500">No sections added yet.</div>}
                  </div>
                </div>
              </div>
              {error && <div className="mt-2 text-sm text-red-600">{error}</div>}
              <div className="mt-4 flex justify-end gap-2">
                <button className={`px-3 py-1.5 rounded-md ${isSaving ? 'bg-gray-100 text-gray-400 cursor-not-allowed' : 'bg-gray-100 hover:bg-gray-200'}`} disabled={isSaving} onClick={() => { if (isSaving) return; setIsFormOpen(false) }}>Cancel</button>
                {editing ? (
                  <button className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md ${!isSaving ? 'bg-indigo-600 text-white hover:bg-indigo-700' : 'bg-gray-200 text-gray-500 cursor-not-allowed'}`} disabled={isSaving} onClick={update}>
                    {isSaving && <span className="inline-block h-4 w-4 border-2 border-white/70 border-t-transparent rounded-full animate-spin" />}
                    {isSaving ? 'Updating…' : 'Update'}
                  </button>
                ) : (
                  <button className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-md ${!isSaving ? 'bg-indigo-600 text-white hover:bg-indigo-700' : 'bg-gray-200 text-gray-500 cursor-not-allowed'}`} disabled={isSaving} onClick={save}>
                    {isSaving && <span className="inline-block h-4 w-4 border-2 border-white/70 border-t-transparent rounded-full animate-spin" />}
                    {isSaving ? 'Saving…' : 'Save'}
                  </button>
                )}
              </div>
            </div>
          </div>
        )}
      </div>
    </Layout>
  )
}


