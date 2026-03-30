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

import { useEffect, useState } from 'react'
import { apiService } from '../../../lib/apiService'
import { Plus, Trash2, Grid, LayoutGrid, Eye, EyeOff } from 'lucide-react'

// Predefined layout definitions
const PREDEFINED_LAYOUTS: Record<string, { name: string; description: string; gridCols: number; gridRows: number; tiles: Array<{ row: number; col: number; rowSpan: number; colSpan: number }> }> = {
  '1x1': {
    name: '1×1',
    description: '1 camera full screen',
    gridCols: 1,
    gridRows: 1,
    tiles: [{ row: 0, col: 0, rowSpan: 1, colSpan: 1 }]
  },
  '2x2': {
    name: '2×2',
    description: '4 cameras in 2×2 grid',
    gridCols: 2,
    gridRows: 2,
    tiles: [
      { row: 0, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 0, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 1, rowSpan: 1, colSpan: 1 },
    ]
  },
  '3x3': {
    name: '3×3',
    description: '9 cameras in 3×3 grid',
    gridCols: 3,
    gridRows: 3,
    tiles: Array.from({ length: 9 }, (_, i) => ({ row: Math.floor(i / 3), col: i % 3, rowSpan: 1, colSpan: 1 }))
  },
  '4x4': {
    name: '4×4',
    description: '16 cameras in 4×4 grid',
    gridCols: 4,
    gridRows: 4,
    tiles: Array.from({ length: 16 }, (_, i) => ({ row: Math.floor(i / 4), col: i % 4, rowSpan: 1, colSpan: 1 }))
  },
  '1+5': {
    name: '1+5',
    description: '1 large + 5 small cameras',
    gridCols: 3,
    gridRows: 3,
    tiles: [
      { row: 0, col: 0, rowSpan: 2, colSpan: 2 }, // Large
      { row: 0, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 2, rowSpan: 1, colSpan: 1 },
    ]
  },
  '1+7': {
    name: '1+7',
    description: '1 large + 7 small cameras',
    gridCols: 4,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 3, colSpan: 3 }, // Large
      { row: 0, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 3, rowSpan: 1, colSpan: 1 },
    ]
  },
  '2+8': {
    name: '2+8',
    description: '2 large + 8 small cameras',
    gridCols: 4,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 2, colSpan: 2 }, // Large 1
      { row: 0, col: 2, rowSpan: 2, colSpan: 2 }, // Large 2
      { row: 2, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 3, rowSpan: 1, colSpan: 1 },
    ]
  },
  '1+12': {
    name: '1+12',
    description: '1 large + 12 small cameras',
    gridCols: 4,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 3, colSpan: 3 }, // Large
      { row: 0, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 3, rowSpan: 1, colSpan: 1 },
    ]
  },
  '4+9': {
    name: '4+9',
    description: '4 medium + 9 small cameras',
    gridCols: 5,
    gridRows: 5,
    tiles: [
      { row: 0, col: 0, rowSpan: 2, colSpan: 2 }, // Medium 1
      { row: 0, col: 2, rowSpan: 2, colSpan: 2 }, // Medium 2
      { row: 2, col: 0, rowSpan: 2, colSpan: 2 }, // Medium 3
      { row: 2, col: 2, rowSpan: 2, colSpan: 2 }, // Medium 4
      { row: 0, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 4, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 4, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 4, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 4, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 4, col: 4, rowSpan: 1, colSpan: 1 },
    ]
  },
  '1+1+10': {
    name: '1+1+10',
    description: '2 large side-by-side + 10 small',
    gridCols: 5,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 3, colSpan: 2 }, // Large 1
      { row: 0, col: 2, rowSpan: 3, colSpan: 2 }, // Large 2
      { row: 0, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 4, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 3, rowSpan: 1, colSpan: 1 },
      { row: 3, col: 4, rowSpan: 1, colSpan: 1 },
    ]
  },
}

interface WindowDivisionSettings {
  layouts_enabled: Record<string, boolean>
  custom_layouts: Array<{
    id: string
    name: string
    description?: string
    enabled: boolean
    grid_columns: number
    grid_rows: number
    tiles: Array<{ row: number; col: number; rowSpan: number; colSpan: number }>
  }>
  default_layout: string
}

const defaultSettings: WindowDivisionSettings = {
  layouts_enabled: {
    '1x1': true,
    '2x2': true,
    '3x3': true,
    '4x4': true,
    '1+5': true,
    '1+7': true,
    '2+8': true,
    '1+12': true,
    '4+9': true,
    '1+1+10': true,
  },
  custom_layouts: [],
  default_layout: '2x2',
}

export function WindowSettings() {
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [settings, setSettings] = useState<WindowDivisionSettings>(defaultSettings)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  // Custom layout editor state
  const [showEditor, setShowEditor] = useState(false)
  const [editingLayout, setEditingLayout] = useState<WindowDivisionSettings['custom_layouts'][0] | null>(null)

  useEffect(() => {
    let mounted = true
    ;(async () => {
      setLoading(true)
      try {
        const { data } = await apiService.getWindowSettings()
        if (!mounted) return
        // Merge with defaults in case new layouts are added
        setSettings({
          ...defaultSettings,
          ...data,
          layouts_enabled: { ...defaultSettings.layouts_enabled, ...data.layouts_enabled },
        })
      } catch (e: any) {
        setError(e?.data?.detail || e?.message || 'Failed to load settings')
      } finally {
        setLoading(false)
      }
    })()
    return () => { mounted = false }
  }, [])

  const onSave = async () => {
    setSaving(true)
    setError(null)
    try {
      await apiService.updateWindowSettings(settings)
      setNotice('Settings saved successfully')
      setTimeout(() => setNotice(null), 3000)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to save settings')
    } finally {
      setSaving(false)
    }
  }

  const toggleLayout = (id: string) => {
    setSettings(prev => ({
      ...prev,
      layouts_enabled: {
        ...prev.layouts_enabled,
        [id]: !prev.layouts_enabled[id],
      },
    }))
  }

  const setDefaultLayout = (id: string) => {
    setSettings(prev => ({ ...prev, default_layout: id }))
  }

  const deleteCustomLayout = (id: string) => {
    setSettings(prev => ({
      ...prev,
      custom_layouts: prev.custom_layouts.filter(l => l.id !== id),
    }))
  }

  const openEditor = (layout?: WindowDivisionSettings['custom_layouts'][0]) => {
    if (layout) {
      setEditingLayout({ ...layout })
    } else {
      setEditingLayout({
        id: `custom-${Date.now()}`,
        name: 'New Layout',
        description: '',
        enabled: true,
        grid_columns: 4,
        grid_rows: 4,
        tiles: [],
      })
    }
    setShowEditor(true)
  }

  const saveCustomLayout = (layout: WindowDivisionSettings['custom_layouts'][0]) => {
    setSettings(prev => {
      const existing = prev.custom_layouts.findIndex(l => l.id === layout.id)
      if (existing >= 0) {
        const updated = [...prev.custom_layouts]
        updated[existing] = layout
        return { ...prev, custom_layouts: updated }
      }
      return { ...prev, custom_layouts: [...prev.custom_layouts, layout] }
    })
    setShowEditor(false)
    setEditingLayout(null)
  }

  if (loading) return <div className="text-sm text-[var(--text-dim)]">Loading…</div>

  const allLayouts = Object.keys(PREDEFINED_LAYOUTS)

  return (
    <div className="space-y-6">
      <h2 className="text-base font-semibold flex items-center gap-2">
        <LayoutGrid size={18} />
        Window Division Settings
      </h2>

      {error && <div className="text-sm text-red-400 bg-red-900/20 border border-red-800 p-2">{error}</div>}
      {notice && <div className="text-sm text-emerald-400 bg-emerald-900/20 border border-emerald-800 p-2">{notice}</div>}

      {/* Predefined Layouts */}
      <section className="space-y-3">
        <h3 className="text-sm font-medium text-[var(--text-dim)]">Predefined Layouts</h3>
        <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {allLayouts.map(id => {
            const layout = PREDEFINED_LAYOUTS[id]
            const enabled = settings.layouts_enabled[id] ?? true
            const isDefault = settings.default_layout === id

            return (
              <div
                key={id}
                className={`relative border p-3 ${enabled ? 'border-neutral-600 bg-[var(--panel-2)]' : 'border-neutral-800 bg-[var(--bg-2)] opacity-60'}`}
              >
                {/* Preview Grid */}
                <div
                  className="aspect-video bg-[var(--bg)] mb-2 relative"
                  style={{
                    display: 'grid',
                    gridTemplateColumns: `repeat(${layout.gridCols}, 1fr)`,
                    gridTemplateRows: `repeat(${layout.gridRows}, 1fr)`,
                    gap: '2px',
                    padding: '4px',
                  }}
                >
                  {layout.tiles.map((tile, i) => (
                    <div
                      key={i}
                      className="bg-[var(--accent)]/30 border border-[var(--accent)]/50"
                      style={{
                        gridRow: `${tile.row + 1} / span ${tile.rowSpan}`,
                        gridColumn: `${tile.col + 1} / span ${tile.colSpan}`,
                      }}
                    />
                  ))}
                </div>

                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-sm font-medium">{layout.name}</div>
                    <div className="text-xs text-[var(--text-dim)]">{layout.description}</div>
                  </div>
                </div>

                <div className="mt-2 flex items-center gap-2">
                  <button
                    className={`px-2 py-1 text-xs border ${enabled ? 'border-emerald-600 bg-emerald-900/20 text-emerald-400' : 'border-neutral-600 bg-neutral-800 text-neutral-400'}`}
                    onClick={() => toggleLayout(id)}
                    title={enabled ? 'Disable' : 'Enable'}
                  >
                    {enabled ? <Eye size={12} className="inline mr-1" /> : <EyeOff size={12} className="inline mr-1" />}
                    {enabled ? 'Enabled' : 'Disabled'}
                  </button>
                  <button
                    className={`px-2 py-1 text-xs border ${isDefault ? 'border-[var(--accent)] bg-[var(--accent)]/20 text-[var(--accent)]' : 'border-neutral-600 bg-neutral-800 text-neutral-400'}`}
                    onClick={() => setDefaultLayout(id)}
                    disabled={!enabled}
                    title="Set as default"
                  >
                    {isDefault ? '★ Default' : 'Set Default'}
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </section>

      {/* Custom Layouts */}
      <section className="space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium text-[var(--text-dim)]">Custom Layouts</h3>
          <button
            className="px-3 py-1.5 text-xs border border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)] inline-flex items-center gap-1"
            onClick={() => openEditor()}
          >
            <Plus size={14} /> Add Custom Layout
          </button>
        </div>

        {settings.custom_layouts.length === 0 ? (
          <div className="text-sm text-[var(--text-dim)] bg-[var(--bg-2)] border border-neutral-700 p-4 text-center">
            No custom layouts defined. Click "Add Custom Layout" to create one.
          </div>
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {settings.custom_layouts.map(layout => {
              const isDefault = settings.default_layout === layout.id

              return (
                <div
                  key={layout.id}
                  className={`relative border p-3 ${layout.enabled ? 'border-neutral-600 bg-[var(--panel-2)]' : 'border-neutral-800 bg-[var(--bg-2)] opacity-60'}`}
                >
                  {/* Preview Grid */}
                  <div
                    className="aspect-video bg-[var(--bg)] mb-2 relative"
                    style={{
                      display: 'grid',
                      gridTemplateColumns: `repeat(${layout.grid_columns}, 1fr)`,
                      gridTemplateRows: `repeat(${layout.grid_rows}, 1fr)`,
                      gap: '2px',
                      padding: '4px',
                    }}
                  >
                    {layout.tiles.map((tile, i) => (
                      <div
                        key={i}
                        className="bg-blue-500/30 border border-blue-500/50"
                        style={{
                          gridRow: `${tile.row + 1} / span ${tile.rowSpan}`,
                          gridColumn: `${tile.col + 1} / span ${tile.colSpan}`,
                        }}
                      />
                    ))}
                  </div>

                  <div className="flex items-center justify-between">
                    <div>
                      <div className="text-sm font-medium">{layout.name}</div>
                      <div className="text-xs text-[var(--text-dim)]">{layout.description || 'Custom layout'}</div>
                    </div>
                  </div>

                  <div className="mt-2 flex items-center gap-2 flex-wrap">
                    <button
                      className="px-2 py-1 text-xs border border-neutral-600 bg-neutral-800"
                      onClick={() => openEditor(layout)}
                    >
                      Edit
                    </button>
                    <button
                      className={`px-2 py-1 text-xs border ${isDefault ? 'border-[var(--accent)] bg-[var(--accent)]/20 text-[var(--accent)]' : 'border-neutral-600 bg-neutral-800'}`}
                      onClick={() => setDefaultLayout(layout.id)}
                      disabled={!layout.enabled}
                    >
                      {isDefault ? '★ Default' : 'Set Default'}
                    </button>
                    <button
                      className="px-2 py-1 text-xs border border-red-600 bg-red-900/20 text-red-400"
                      onClick={() => deleteCustomLayout(layout.id)}
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </section>

      {/* Save Button */}
      <div className="flex justify-end pt-4 border-t border-neutral-700">
        <button
          disabled={saving}
          onClick={onSave}
          className="px-4 py-2 bg-[var(--accent)] text-white disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save Settings'}
        </button>
      </div>

      {/* Custom Layout Editor Modal */}
      {showEditor && editingLayout && (
        <CustomLayoutEditor
          layout={editingLayout}
          onSave={saveCustomLayout}
          onCancel={() => { setShowEditor(false); setEditingLayout(null) }}
        />
      )}
    </div>
  )
}

// Custom Layout Editor Component
function CustomLayoutEditor({
  layout,
  onSave,
  onCancel,
}: {
  layout: WindowDivisionSettings['custom_layouts'][0]
  onSave: (layout: WindowDivisionSettings['custom_layouts'][0]) => void
  onCancel: () => void
}) {
  const [form, setForm] = useState(layout)
  const [selectedTile, setSelectedTile] = useState<number | null>(null)

  const addTile = () => {
    setForm(prev => ({
      ...prev,
      tiles: [...prev.tiles, { row: 0, col: 0, rowSpan: 1, colSpan: 1 }],
    }))
  }

  const removeTile = (index: number) => {
    setForm(prev => ({
      ...prev,
      tiles: prev.tiles.filter((_, i) => i !== index),
    }))
    setSelectedTile(null)
  }

  const updateTile = (index: number, field: string, value: number) => {
    setForm(prev => ({
      ...prev,
      tiles: prev.tiles.map((t, i) =>
        i === index ? { ...t, [field]: value } : t
      ),
    }))
  }

  return (
    <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4">
      <div className="bg-[var(--panel)] border border-neutral-600 w-full max-w-4xl max-h-[90vh] overflow-auto">
        <div className="p-4 border-b border-neutral-700 flex items-center justify-between">
          <h3 className="font-semibold">Custom Layout Editor</h3>
          <button className="px-2 py-1 text-xs border border-neutral-600" onClick={onCancel}>
            Cancel
          </button>
        </div>

        <div className="p-4 grid grid-cols-2 gap-6">
          {/* Left: Settings */}
          <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Layout Name</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-2 py-1 text-sm"
                  value={form.name}
                  onChange={(e) => setForm(prev => ({ ...prev, name: e.target.value }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Description</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-2 py-1 text-sm"
                  value={form.description || ''}
                  onChange={(e) => setForm(prev => ({ ...prev, description: e.target.value }))}
                />
              </label>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Grid Columns</span>
                <input
                  type="number"
                  min={1}
                  max={8}
                  className="bg-[var(--bg-2)] border border-neutral-700 px-2 py-1 text-sm"
                  value={form.grid_columns}
                  onChange={(e) => setForm(prev => ({ ...prev, grid_columns: parseInt(e.target.value) || 4 }))}
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Grid Rows</span>
                <input
                  type="number"
                  min={1}
                  max={8}
                  className="bg-[var(--bg-2)] border border-neutral-700 px-2 py-1 text-sm"
                  value={form.grid_rows}
                  onChange={(e) => setForm(prev => ({ ...prev, grid_rows: parseInt(e.target.value) || 4 }))}
                />
              </label>
            </div>

            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => setForm(prev => ({ ...prev, enabled: e.target.checked }))}
              />
              <span className="text-sm">Enabled</span>
            </label>

            {/* Tiles List */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">Tiles ({form.tiles.length})</span>
                <button
                  className="px-2 py-1 text-xs border border-[var(--accent)] bg-[var(--accent)]/10 text-[var(--accent)] inline-flex items-center gap-1"
                  onClick={addTile}
                >
                  <Plus size={12} /> Add Tile
                </button>
              </div>

              <div className="max-h-60 overflow-auto space-y-1">
                {form.tiles.map((tile, i) => (
                  <div
                    key={i}
                    className={`p-2 border text-xs cursor-pointer ${selectedTile === i ? 'border-[var(--accent)] bg-[var(--accent)]/10' : 'border-neutral-700 bg-[var(--bg-2)]'}`}
                    onClick={() => setSelectedTile(i)}
                  >
                    <div className="flex items-center justify-between">
                      <span>Tile {i + 1}</span>
                      <button
                        className="text-red-400 hover:text-red-300"
                        onClick={(e) => { e.stopPropagation(); removeTile(i) }}
                      >
                        <Trash2 size={12} />
                      </button>
                    </div>
                    <div className="mt-1 grid grid-cols-4 gap-2">
                      <label className="flex flex-col">
                        <span className="text-[10px] text-[var(--text-dim)]">Row</span>
                        <input
                          type="number"
                          min={0}
                          max={form.grid_rows - 1}
                          className="bg-[var(--panel)] border border-neutral-600 px-1 py-0.5 w-full"
                          value={tile.row}
                          onChange={(e) => updateTile(i, 'row', parseInt(e.target.value) || 0)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </label>
                      <label className="flex flex-col">
                        <span className="text-[10px] text-[var(--text-dim)]">Col</span>
                        <input
                          type="number"
                          min={0}
                          max={form.grid_columns - 1}
                          className="bg-[var(--panel)] border border-neutral-600 px-1 py-0.5 w-full"
                          value={tile.col}
                          onChange={(e) => updateTile(i, 'col', parseInt(e.target.value) || 0)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </label>
                      <label className="flex flex-col">
                        <span className="text-[10px] text-[var(--text-dim)]">Height</span>
                        <input
                          type="number"
                          min={1}
                          max={form.grid_rows}
                          className="bg-[var(--panel)] border border-neutral-600 px-1 py-0.5 w-full"
                          value={tile.rowSpan}
                          onChange={(e) => updateTile(i, 'rowSpan', parseInt(e.target.value) || 1)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </label>
                      <label className="flex flex-col">
                        <span className="text-[10px] text-[var(--text-dim)]">Width</span>
                        <input
                          type="number"
                          min={1}
                          max={form.grid_columns}
                          className="bg-[var(--panel)] border border-neutral-600 px-1 py-0.5 w-full"
                          value={tile.colSpan}
                          onChange={(e) => updateTile(i, 'colSpan', parseInt(e.target.value) || 1)}
                          onClick={(e) => e.stopPropagation()}
                        />
                      </label>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>

          {/* Right: Preview */}
          <div className="space-y-2">
            <span className="text-sm font-medium">Preview</span>
            <div
              className="aspect-video bg-[var(--bg)] border border-neutral-700"
              style={{
                display: 'grid',
                gridTemplateColumns: `repeat(${form.grid_columns}, 1fr)`,
                gridTemplateRows: `repeat(${form.grid_rows}, 1fr)`,
                gap: '2px',
                padding: '4px',
              }}
            >
              {form.tiles.map((tile, i) => (
                <div
                  key={i}
                  className={`flex items-center justify-center text-xs ${selectedTile === i ? 'bg-[var(--accent)]/50 border-2 border-[var(--accent)]' : 'bg-blue-500/30 border border-blue-500/50'}`}
                  style={{
                    gridRow: `${tile.row + 1} / span ${tile.rowSpan}`,
                    gridColumn: `${tile.col + 1} / span ${tile.colSpan}`,
                  }}
                  onClick={() => setSelectedTile(i)}
                >
                  {i + 1}
                </div>
              ))}
            </div>
            <div className="text-xs text-[var(--text-dim)]">
              Click on tiles in the preview or list to select. Use the inputs to adjust position and size.
            </div>
          </div>
        </div>

        <div className="p-4 border-t border-neutral-700 flex justify-end gap-2">
          <button
            className="px-4 py-2 border border-neutral-600 bg-neutral-800"
            onClick={onCancel}
          >
            Cancel
          </button>
          <button
            className="px-4 py-2 bg-[var(--accent)] text-white"
            onClick={() => onSave(form)}
          >
            Save Layout
          </button>
        </div>
      </div>
    </div>
  )
}
