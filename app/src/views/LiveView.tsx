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

import { useEffect, useRef, useState, useMemo } from 'react'
import { apiService } from '../lib/apiService'
import { VideoPlayer, type VideoPlayerHandle } from '../components/VideoPlayer'
import { useFullscreen } from '../hooks/useFullscreen'
import { usePermissions } from '../hooks/usePermissions'
import { Camera, Maximize, Play, Settings, Save, Image as ImageIcon, Book, HardDrive, Power, X, Grid, Move, Square, Plus, Minus, ChevronDown, Video, Search, Loader2, CheckCircle, AlertCircle } from 'lucide-react'
import { 
  DndContext, 
  DragOverlay, 
  useDraggable, 
  useDroppable, 
  closestCenter, 
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent, 
  type DragStartEvent
} from '@dnd-kit/core'
import { restrictToWindowEdges } from '@dnd-kit/modifiers'

// Layout definitions - matching the WindowSettings
interface LayoutDefinition {
  name: string
  gridCols: number
  gridRows: number
  tiles: Array<{ row: number; col: number; rowSpan: number; colSpan: number }>
}

const PREDEFINED_LAYOUTS: Record<string, LayoutDefinition> = {
  '1x1': {
    name: '1×1',
    gridCols: 1,
    gridRows: 1,
    tiles: [{ row: 0, col: 0, rowSpan: 1, colSpan: 1 }]
  },
  '2x2': {
    name: '2×2',
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
    gridCols: 3,
    gridRows: 3,
    tiles: Array.from({ length: 9 }, (_, i) => ({ row: Math.floor(i / 3), col: i % 3, rowSpan: 1, colSpan: 1 }))
  },
  '4x4': {
    name: '4×4',
    gridCols: 4,
    gridRows: 4,
    tiles: Array.from({ length: 16 }, (_, i) => ({ row: Math.floor(i / 4), col: i % 4, rowSpan: 1, colSpan: 1 }))
  },
  '1+5': {
    name: '1+5',
    gridCols: 3,
    gridRows: 3,
    tiles: [
      { row: 0, col: 0, rowSpan: 2, colSpan: 2 },
      { row: 0, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 1, col: 2, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 0, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 1, rowSpan: 1, colSpan: 1 },
      { row: 2, col: 2, rowSpan: 1, colSpan: 1 },
    ]
  },
  '1+7': {
    name: '1+7',
    gridCols: 4,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 3, colSpan: 3 },
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
    gridCols: 4,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 2, colSpan: 2 },
      { row: 0, col: 2, rowSpan: 2, colSpan: 2 },
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
    gridCols: 4,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 3, colSpan: 3 },
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
    gridCols: 5,
    gridRows: 5,
    tiles: [
      { row: 0, col: 0, rowSpan: 2, colSpan: 2 },
      { row: 0, col: 2, rowSpan: 2, colSpan: 2 },
      { row: 2, col: 0, rowSpan: 2, colSpan: 2 },
      { row: 2, col: 2, rowSpan: 2, colSpan: 2 },
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
    gridCols: 5,
    gridRows: 4,
    tiles: [
      { row: 0, col: 0, rowSpan: 3, colSpan: 2 },
      { row: 0, col: 2, rowSpan: 3, colSpan: 2 },
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

interface WindowSettings {
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

export function LiveView() {
  const { hasPermission } = usePermissions()
  const canManageCameras = hasPermission('cameras.manage')
  const [currentLayout, setCurrentLayout] = useState<string>('3x3')
  const [windowSettings, setWindowSettings] = useState<WindowSettings | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [layoutMenuOpen, setLayoutMenuOpen] = useState(false)
  const gridRef = useRef<HTMLDivElement>(null)
  const { toggle: toggleFs, isFullscreen } = useFullscreen(gridRef as React.RefObject<HTMLDivElement>)
  // FS toolbar visibility on bottom hover
  const [fsToolbarVisible, setFsToolbarVisible] = useState(false)
  const isOverToolbarRef = useRef(false)
  const hideTimer = useRef<number | null>(null)
  const [availableCameras, setAvailableCameras] = useState<Array<{id: number, name: string}>>([])
  
  // Camera display order - array of camera IDs in display sequence
  // This determines which camera appears in which tile position
  const [cameraDisplayOrder, setCameraDisplayOrder] = useState<number[]>(() => {
    try {
      const saved = localStorage.getItem('liveview-camera-display-order')
      return saved ? JSON.parse(saved) : []
    } catch {
      return []
    }
  })
  
  // Drag state for overlay
  const [activeDragId, setActiveDragId] = useState<string | null>(null)
  
  // Configure sensors for drag and drop
  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: {
        distance: 8, // 8px movement required before drag starts
      },
    })
  )
  
  // Custom modifier to center overlay on cursor
  const centerOnCursor = ({ transform, activeNodeRect, activatorEvent }: any) => {
    if (activeNodeRect && activatorEvent) {
      // Calculate where within the element the user clicked
      const offsetX = activatorEvent.clientX - activeNodeRect.left
      const offsetY = activatorEvent.clientY - activeNodeRect.top
      
      // Overlay size: 128px x 88px, we want cursor at center
      const overlayHalfWidth = 64
      const overlayHalfHeight = 44
      
      return {
        ...transform,
        x: transform.x + offsetX - overlayHalfWidth,
        y: transform.y + offsetY - overlayHalfHeight,
      }
    }
    return transform
  }
  
  // Function to reload cameras
  const loadCameras = () => {
    apiService.getCameras().then(({ data }) => {
      const cameras = data.cameras || data || []
      const cameraList = cameras.map((cam: any) => ({ id: cam.id, name: cam.name }))
      setAvailableCameras(cameraList)
      
      // Update display order: add new cameras, remove deleted ones
      setCameraDisplayOrder(prevOrder => {
        const existingIds = new Set(cameraList.map((c: {id: number}) => c.id))
        // Filter out deleted cameras
        const filtered = prevOrder.filter(id => existingIds.has(id))
        // Add new cameras that aren't in the order yet
        const newCameras = cameraList
          .filter((c: {id: number}) => !filtered.includes(c.id))
          .map((c: {id: number}) => c.id)
        const updated = [...filtered, ...newCameras]
        // Persist to localStorage
        try {
          localStorage.setItem('liveview-camera-display-order', JSON.stringify(updated))
        } catch {}
        return updated
      })
    }).catch(console.error)
  }
  
  // Assign a camera to a specific tile position
  const assignCameraToTile = (tileIndex: number, cameraId: number) => {
    setCameraDisplayOrder(prev => {
      // Remove camera from current position if it exists
      const filtered = prev.filter(id => id !== cameraId)
      // Insert at the specified tile position
      const updated = [...filtered]
      // Ensure array is long enough
      while (updated.length < tileIndex) {
        updated.push(-1) // placeholder
      }
      updated.splice(tileIndex, 0, cameraId)
      // Clean up any -1 placeholders
      const cleaned = updated.filter(id => id !== -1)
      try {
        localStorage.setItem('liveview-camera-display-order', JSON.stringify(cleaned))
      } catch {}
      return cleaned
    })
  }
  
  // Swap two tile positions (for drag and drop)
  const swapTilePositions = (fromIndex: number, toIndex: number) => {
    setCameraDisplayOrder(prev => {
      const updated = [...prev]
      // Ensure array is long enough for both indices
      const maxIndex = Math.max(fromIndex, toIndex)
      while (updated.length <= maxIndex) {
        updated.push(0) // placeholder for empty slots
      }
      // Swap the cameras at these positions
      const temp = updated[fromIndex]
      updated[fromIndex] = updated[toIndex]
      updated[toIndex] = temp
      // Filter out any 0 placeholders (empty swaps)
      const cleaned = updated.filter(id => id !== 0)
      try {
        localStorage.setItem('liveview-camera-display-order', JSON.stringify(cleaned))
      } catch {}
      return cleaned
    })
  }
  
  // Handle drag end - swap tiles
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event
    setActiveDragId(null)
    
    if (!over || active.id === over.id) return
    
    const fromIndex = parseInt(String(active.id).replace('tile-', ''))
    const toIndex = parseInt(String(over.id).replace('tile-', ''))
    
    if (isNaN(fromIndex) || isNaN(toIndex)) return
    
    swapTilePositions(fromIndex, toIndex)
  }
  
  const handleDragStart = (event: DragStartEvent) => {
    setActiveDragId(String(event.active.id))
  }
  
  // Get current layout definition
  const getLayoutDef = (): LayoutDefinition => {
    // Check custom layouts first
    if (windowSettings?.custom_layouts) {
      const custom = windowSettings.custom_layouts.find(l => l.id === currentLayout && l.enabled)
      if (custom) {
        return {
          name: custom.name,
          gridCols: custom.grid_columns,
          gridRows: custom.grid_rows,
          tiles: custom.tiles.map(t => ({ row: t.row, col: t.col, rowSpan: t.rowSpan, colSpan: t.colSpan }))
        }
      }
    }
    // Fall back to predefined layouts
    return PREDEFINED_LAYOUTS[currentLayout] || PREDEFINED_LAYOUTS['3x3']
  }
  
  const layoutDef = getLayoutDef()

  // Load window settings on mount
  useEffect(() => {
    apiService.getWindowSettings().then(({ data }) => {
      setWindowSettings(data)
      // Set default layout if available
      if (data?.default_layout) {
        setCurrentLayout(data.default_layout)
      }
    }).catch(console.error)
  }, [])

  useEffect(() => {
    // Load available cameras
    loadCameras()
  }, [])

  useEffect(() => {
    const el = gridRef.current
    if (!el || !isFullscreen) return

    const onMouseMove = (e: MouseEvent) => {
      const viewportHeight = window.innerHeight
      const fromBottom = viewportHeight - e.clientY
      const threshold = 80
      if (fromBottom <= threshold) {
        // Near bottom edge -> show toolbar
        if (hideTimer.current) {
          window.clearTimeout(hideTimer.current)
          hideTimer.current = null
        }
        setFsToolbarVisible(true)
      } else if (!isOverToolbarRef.current) {
        // Away from bottom and not over toolbar -> hide after short delay
        if (hideTimer.current) window.clearTimeout(hideTimer.current)
        hideTimer.current = window.setTimeout(() => setFsToolbarVisible(false), 600)
      }
    }

    el.addEventListener('mousemove', onMouseMove)
    return () => {
      el.removeEventListener('mousemove', onMouseMove)
      if (hideTimer.current) window.clearTimeout(hideTimer.current)
      hideTimer.current = null
      setFsToolbarVisible(false)
      isOverToolbarRef.current = false
    }
  }, [isFullscreen])
  
  // Get all available layouts (enabled predefined + enabled custom)
  const getAvailableLayouts = () => {
    const layouts: Array<{ id: string; name: string; tiles: number }> = []
    
    // Add enabled predefined layouts
    Object.entries(PREDEFINED_LAYOUTS).forEach(([id, def]) => {
      if (!windowSettings || windowSettings.layouts_enabled[id] !== false) {
        layouts.push({ id, name: def.name, tiles: def.tiles.length })
      }
    })
    
    // Add enabled custom layouts
    if (windowSettings?.custom_layouts) {
      windowSettings.custom_layouts
        .filter(l => l.enabled)
        .forEach(l => {
          layouts.push({ id: l.id, name: l.name, tiles: l.tiles.length })
        })
    }
    
    return layouts
  }
  
  const availableLayouts = getAvailableLayouts()

  return (
    <section className="space-y-3">
      <header className="flex items-center gap-2">
        <h1 className="text-lg font-semibold">Live View</h1>
        <div className="ml-auto flex items-center gap-1">
          {/* Quick layout buttons for common layouts */}
          {['1x1', '2x2', '3x3', '4x4'].map((layoutId) => {
            const layout = PREDEFINED_LAYOUTS[layoutId]
            if (!layout) return null
            const isEnabled = !windowSettings || windowSettings.layouts_enabled[layoutId] !== false
            if (!isEnabled) return null
            return (
              <button
                key={layoutId}
                className={`px-2 py-1 text-xs border ${currentLayout === layoutId ? 'bg-[var(--accent)]/80 border-[var(--accent)]' : 'bg-[var(--panel-2)] border-neutral-700'}`}
                onClick={() => setCurrentLayout(layoutId)}
                title={layout.name}
              >
                {layout.name}
              </button>
            )
          })}
          {/* Layout dropdown for special layouts */}
          <div className="relative">
            <button
              className="px-2 py-1 text-xs border bg-[var(--panel-2)] border-neutral-700 inline-flex items-center gap-1"
              onClick={() => setLayoutMenuOpen(!layoutMenuOpen)}
            >
              <Grid size={12} />
              <span className="hidden sm:inline">More</span>
              <ChevronDown size={12} />
            </button>
            {layoutMenuOpen && (
              <div className="absolute right-0 top-full mt-1 z-50 bg-[var(--panel)] border border-neutral-700 shadow-lg min-w-[180px]">
                {availableLayouts.map(layout => (
                  <button
                    key={layout.id}
                    className={`w-full text-left px-3 py-2 text-xs hover:bg-[var(--panel-2)] flex items-center justify-between ${currentLayout === layout.id ? 'bg-[var(--accent)]/20 text-[var(--accent)]' : ''}`}
                    onClick={() => { setCurrentLayout(layout.id); setLayoutMenuOpen(false) }}
                  >
                    <span>{layout.name}</span>
                    <span className="text-[var(--text-dim)]">{layout.tiles} cameras</span>
                  </button>
                ))}
                <div className="border-t border-neutral-700 px-3 py-2">
                  <button
                    className="text-xs text-[var(--text-dim)] hover:text-[var(--accent)]"
                    onClick={() => {
                      setLayoutMenuOpen(false)
                      ;(window as any).routerNavigate?.('/settings/more-settings/window-settings')
                    }}
                  >
                    ⚙ Configure Layouts...
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      </header>

      <DndContext 
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div 
          ref={gridRef} 
          className="grid gap-2 relative"
          style={{ 
            gridTemplateColumns: `repeat(${layoutDef.gridCols}, minmax(0, 1fr))`,
            gridTemplateRows: `repeat(${layoutDef.gridRows}, minmax(0, 1fr))`,
          }}
        >
          {layoutDef.tiles.map((tile, i) => {
            // Sequential display: cameras fill tiles in order from cameraDisplayOrder
            // Empty slots only appear after all cameras
            const assignedCameraId = cameraDisplayOrder[i] ?? null
            
            return (
              <div
                key={i}
                style={{
                  gridRow: `${tile.row + 1} / span ${tile.rowSpan}`,
                  gridColumn: `${tile.col + 1} / span ${tile.colSpan}`,
                }}
              >
                <DroppableTile tileId={`tile-${i}`}>
                  <DraggableTile 
                    tileId={`tile-${i}`}
                    hasCameraAssigned={!!assignedCameraId}
                  >
                    <Tile 
                      index={i} 
                      availableCameras={availableCameras} 
                      assignedCameraId={assignedCameraId} 
                      onCameraSelected={(cameraId) => assignCameraToTile(i, cameraId)}
                      onCameraAdded={loadCameras}
                      isDragging={activeDragId === `tile-${i}`}
                      canManage={canManageCameras}
                    />
                  </DraggableTile>
                </DroppableTile>
              </div>
            )
          })}
          {/* Fullscreen hover toolbar overlay */}
          {isFullscreen && (
            <div className={`pointer-events-none absolute inset-x-0 bottom-0 z-40 transition-all duration-200 ${fsToolbarVisible ? 'opacity-100 translate-y-0' : 'opacity-0 translate-y-2'}`}>
              <div
                className="pointer-events-auto mt-2 flex items-center gap-2 bg-[var(--bg-2)] border border-neutral-700 p-2 text-xs"
                onMouseEnter={() => {
                  if (hideTimer.current) {
                    window.clearTimeout(hideTimer.current)
                    hideTimer.current = null
                  }
                  isOverToolbarRef.current = true
                  setFsToolbarVisible(true)
                }}
                onMouseLeave={() => {
                  isOverToolbarRef.current = false
                  if (hideTimer.current) window.clearTimeout(hideTimer.current)
                  hideTimer.current = window.setTimeout(() => setFsToolbarVisible(false), 500)
                }}
              >
                <ToolbarContents 
                  currentLayout={currentLayout}
                  setCurrentLayout={setCurrentLayout}
                  availableLayouts={availableLayouts}
                  onOpenMenu={() => setMenuOpen(true)} 
                  onToggleFullscreen={toggleFs} 
                />
              </div>
            </div>
          )}
          {/* Menu overlay inside fullscreen so it appears over the live view */}
          {isFullscreen && menuOpen && <MenuOverlay onClose={() => setMenuOpen(false)} />}
        </div>
        
        {/* Drag overlay - small tile preview centered on cursor */}
        <DragOverlay dropAnimation={null} modifiers={[centerOnCursor]}>
          {activeDragId ? (() => {
            const tileIndex = parseInt(activeDragId.replace('tile-', ''))
            const cameraId = cameraDisplayOrder[tileIndex]
            const camera = availableCameras.find(c => c.id === cameraId)
            return (
              <div className="w-32 bg-[var(--bg-2)] border-2 border-[var(--accent)] shadow-2xl rounded overflow-hidden pointer-events-none">
                <div className="flex flex-col">
                  {/* Camera name header */}
                  <div className="bg-black/80 px-2 py-1 flex items-center justify-between">
                    <span className="text-[10px] font-medium text-white truncate">
                      {camera?.name || `Camera ${cameraId}`}
                    </span>
                    <span className="text-[8px] bg-red-600 px-1 rounded text-white">LIVE</span>
                  </div>
                  {/* Preview area */}
                  <div className="h-16 bg-neutral-800 flex items-center justify-center">
                    <div className="text-center">
                      <Move size={14} className="mx-auto text-[var(--accent)]" />
                    </div>
                  </div>
                </div>
              </div>
            )
          })() : null}
        </DragOverlay>
      </DndContext>

      {/* Bottom toolbar (normal mode) */}
      {!isFullscreen && (
        <div className="mt-2">
          <div className="flex items-center gap-2 bg-[var(--bg-2)] border border-neutral-700 p-2 text-xs">
            <ToolbarContents 
              currentLayout={currentLayout}
              setCurrentLayout={setCurrentLayout}
              availableLayouts={availableLayouts}
              onOpenMenu={() => setMenuOpen(true)} 
              onToggleFullscreen={toggleFs} 
            />
          </div>
        </div>
      )}

  {menuOpen && !isFullscreen && <MenuOverlay onClose={() => setMenuOpen(false)} />}
    </section>
  )
}

// Droppable wrapper for tiles
function DroppableTile({ tileId, children }: { tileId: string; children: React.ReactNode }) {
  const { setNodeRef, isOver } = useDroppable({ id: tileId })
  
  return (
    <div 
      ref={setNodeRef} 
      className={`h-full transition-all duration-150 ${isOver ? 'ring-2 ring-[var(--accent)] ring-offset-1 ring-offset-[var(--bg)]' : ''}`}
    >
      {children}
    </div>
  )
}

// Draggable wrapper for tiles - entire tile is draggable
function DraggableTile({ 
  tileId, 
  hasCameraAssigned,
  children 
}: { 
  tileId: string
  hasCameraAssigned: boolean
  children: React.ReactNode 
}) {
  const { attributes, listeners, setNodeRef, isDragging } = useDraggable({ 
    id: tileId,
    disabled: !hasCameraAssigned // Only allow dragging tiles with cameras
  })
  
  return (
    <div 
      ref={setNodeRef}
      {...(hasCameraAssigned ? listeners : {})}
      {...(hasCameraAssigned ? attributes : {})}
      className={`h-full relative ${isDragging ? 'opacity-50 scale-95' : ''} ${hasCameraAssigned ? 'cursor-grab active:cursor-grabbing' : ''}`}
    >
      {children}
    </div>
  )
}

function Tile({ 
  index, 
  availableCameras, 
  assignedCameraId,
  onCameraSelected,
  onCameraAdded,
  isDragging = false,
  canManage = false
}: { 
  index: number
  availableCameras: Array<{id: number, name: string}>
  assignedCameraId?: number | null
  onCameraSelected?: (cameraId: number) => void
  onCameraAdded?: () => void
  isDragging?: boolean
  canManage?: boolean
}) {
  const [cameraId, setCameraId] = useState<number | null>(null)
  const [cameraName, setCameraName] = useState<string>('')
  const [urls, setUrls] = useState<{ whep?: string; hls?: string; token?: string } | null>(null)
  const playerRef = useRef<VideoPlayerHandle>(null)
  const [ptzOpen, setPtzOpen] = useState(false)
  const [showCameraDialog, setShowCameraDialog] = useState(false)

  useEffect(() => {
    let alive = true
    // Use assigned camera if provided, otherwise no camera
    const camera = assignedCameraId 
      ? availableCameras.find(c => c.id === assignedCameraId) 
      : null
    if (camera) {
      setCameraId(camera.id)
      setCameraName(camera.name)
      ;(async () => {
        try {
          const { data } = await apiService.getStreamUrls(camera.id)
          if (!alive) return
          setUrls({ 
            whep: data.urls?.webrtc, 
            hls: data.urls?.hls,
            token: data.token
          })
        } catch {}
      })()
    } else {
      setCameraId(null)
      setCameraName('')
      setUrls(null)
    }
    return () => { alive = false }
  }, [assignedCameraId, availableCameras])

  const hasLink = !!urls?.whep || !!urls?.hls
  const displayName = cameraName || `Camera ${cameraId || index + 1}`
  
  const handleSnapshot = (dataUrl: string) => {
    const a = document.createElement('a')
    a.href = dataUrl
    a.download = `${displayName.replace(/\s+/g, '-')}-${Date.now()}.jpg`
    document.body.appendChild(a)
    a.click()
    a.remove()
  }

  const handleCameraSelected = (cameraId?: number) => {
    setShowCameraDialog(false)
    if (cameraId) {
      onCameraSelected?.(cameraId)
    }
    onCameraAdded?.()
  }

  const handleExistingCameraSelected = (cameraId: number) => {
    onCameraSelected?.(cameraId)
    setShowCameraDialog(false)
  }
  
  return (
    <div className="flex flex-col bg-[var(--bg-2)] border border-neutral-700 relative overflow-hidden h-full">
      {/* Video container */}
      <div className="aspect-video relative flex-1">
        {!cameraId && <div className="absolute right-2 top-2 z-20 text-[10px] uppercase tracking-wide bg-black/60 px-1 py-0.5">NO CAMERA</div>}
        {!hasLink && cameraId && <div className="absolute right-2 top-2 z-20 text-[10px] uppercase tracking-wide bg-black/60 px-1 py-0.5">NO LINK</div>}

        <div className="w-full h-full">
          {hasLink ? (
            <VideoPlayer
              ref={playerRef}
              mode="live"
              whepUrl={urls?.whep}
              hlsUrl={urls?.hls}
              mediamtxToken={urls?.token}
              title={displayName}
              preferredStreamType="webrtc"
              autoPlay
              muted
              onSnapshot={handleSnapshot}
              className="w-full h-full"
            />
          ) : (
            <div className="w-full h-full flex flex-col items-center justify-center text-xs text-[var(--text-dim)] gap-3">
              {cameraId ? (
                <span>No stream available</span>
              ) : canManage ? (
                <>
                  <button
                    className="w-16 h-16 rounded-full bg-[var(--panel)] border-2 border-dashed border-neutral-600 hover:border-[var(--accent)] hover:bg-[var(--accent)]/10 transition-colors flex items-center justify-center group"
                    onClick={() => setShowCameraDialog(true)}
                    title="Add Camera"
                  >
                    <Plus size={28} className="text-neutral-500 group-hover:text-[var(--accent)]" />
                  </button>
                  <span className="text-neutral-500">Click to add camera</span>
                </>
              ) : (
                <span className="text-neutral-500">No camera assigned</span>
              )}
            </div>
          )}
        </div>

        {/* Mini PTZ pad */}
        {ptzOpen && cameraId && (
          <div className="absolute left-2 bottom-16 z-30 bg-black/80 p-2 border border-neutral-700 text-[10px] rounded">
            <div className="grid grid-cols-3 gap-1">
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&uarr;</button>
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700" onClick={() => ptzStop(cameraId)}><Square size={12} /></button>
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, -0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&darr;</button>
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, -0.5, 0)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&larr;</button>
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-red-500/30" onClick={() => setPtzOpen(false)}>Close</button>
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0.5, 0)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&rarr;</button>
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0, 0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}><Plus size={12} /></button>
              <div />
              <button className="px-1 py-1 bg-[var(--panel-2)] border border-neutral-700 hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0, -0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}><Minus size={12} /></button>
            </div>
          </div>
        )}

        {/* PTZ toggle button - floating */}
        {cameraId && hasLink && (
          <button 
            className="absolute left-2 bottom-2 z-30 px-2 py-1 bg-black/60 hover:bg-black/80 border border-neutral-700 rounded text-[10px] flex items-center gap-1 transition-colors"
            onClick={() => setPtzOpen((s) => !s)}
          >
            <Move size={12} /> PTZ
          </button>
        )}
      </div>

      {/* Camera Selection/Add Dialog */}
      {showCameraDialog && (
        <AddCameraDialog 
          onClose={() => setShowCameraDialog(false)}
          onCameraAdded={handleCameraSelected}
          onCameraSelected={handleExistingCameraSelected}
          existingCameras={availableCameras}
        />
      )}
    </div>
  )
}

async function ptzMove(cameraId: number, x: number, y: number, z: number = 0) {
  console.log('PTZ Move:', { cameraId, x, y, z })
  try {
    const result = await apiService.ptzMove(cameraId, x, y, z)
    console.log('PTZ Move result:', result)
  } catch (err) {
    console.error('PTZ move failed:', err)
  }
}

async function ptzStop(cameraId: number) {
  console.log('PTZ Stop:', { cameraId })
  try {
    const result = await apiService.ptzStop(cameraId)
    console.log('PTZ Stop result:', result)
  } catch (err) {
    console.error('PTZ stop failed:', err)
  }
}

function ToolbarContents({
  currentLayout,
  setCurrentLayout,
  availableLayouts,
  onOpenMenu,
  onToggleFullscreen,
}: {
  currentLayout: string
  setCurrentLayout: (layout: string) => void
  availableLayouts: Array<{ id: string; name: string; tiles: number }>
  onOpenMenu: () => void
  onToggleFullscreen: () => void
}) {
  const [layoutDropdownOpen, setLayoutDropdownOpen] = useState(false)
  
  return (
    <>
      <button className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel-2)] border border-neutral-700" onClick={onOpenMenu}>
        <Grid size={14} /> Menu
      </button>
      <div className="ml-auto flex items-center gap-1">
        {/* Quick layout buttons */}
        {['1x1', '2x2', '3x3', '4x4'].map((layoutId) => {
          const available = availableLayouts.find(l => l.id === layoutId)
          if (!available) return null
          return (
            <button 
              key={layoutId} 
              className={`px-2 py-1 border ${currentLayout === layoutId ? 'bg-[var(--accent)]/80 border-[var(--accent)]' : 'bg-[var(--panel-2)] border-neutral-700'}`} 
              onClick={() => setCurrentLayout(layoutId)}
            >
              {available.name}
            </button>
          )
        })}
        {/* More layouts dropdown */}
        <div className="relative">
          <button 
            className="px-2 py-1 bg-[var(--panel-2)] border border-neutral-700 inline-flex items-center gap-1"
            onClick={() => setLayoutDropdownOpen(!layoutDropdownOpen)}
          >
            <Grid size={14} />
            <ChevronDown size={12} />
          </button>
          {layoutDropdownOpen && (
            <div className="absolute right-0 bottom-full mb-1 z-50 bg-[var(--panel)] border border-neutral-700 shadow-lg min-w-[160px]">
              {availableLayouts.map(layout => (
                <button
                  key={layout.id}
                  className={`w-full text-left px-3 py-2 text-xs hover:bg-[var(--panel-2)] ${currentLayout === layout.id ? 'bg-[var(--accent)]/20 text-[var(--accent)]' : ''}`}
                  onClick={() => { setCurrentLayout(layout.id); setLayoutDropdownOpen(false) }}
                >
                  {layout.name} ({layout.tiles})
                </button>
              ))}
            </div>
          )}
        </div>
        <button className="px-2 py-1 bg-[var(--panel-2)] border border-neutral-700 inline-flex items-center gap-1" onClick={onToggleFullscreen} title="Fullscreen">
          <Maximize size={14} />
          <span className="hidden sm:inline">Fullscreen</span>
        </button>
      </div>
    </>
  )
}

function MenuOverlay({ onClose }: { onClose: () => void }) {
  const items = [
    { icon: <Play />, label: 'Live View', action: 'live' },
    { icon: <Save />, label: 'Export', action: 'export' },
    { icon: <ImageIcon />, label: 'Image Search', action: 'image' },
    { icon: <Book />, label: 'Manual', action: 'manual' },
    { icon: <HardDrive />, label: 'HDD', action: 'hdd' },
    { icon: <Camera />, label: 'Camera', action: 'camera' },
    { icon: <Settings />, label: 'Configuration', action: 'settings', highlight: true },
    { icon: <Power />, label: 'Shutdown', action: 'shutdown' },
  ]
  return (
    <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center">
      <div className="bg-[var(--panel)] border border-[var(--accent)]/60 p-4 w-[520px] max-w-[90vw]">
        <div className="flex items-center mb-3">
          <div className="text-sm font-semibold">Menu</div>
          <button className="ml-auto px-2 py-1 bg-[var(--panel-2)] border border-neutral-700 inline-flex items-center gap-1" onClick={onClose}>
            <X size={14} /> Close
          </button>
        </div>
        <div className="grid grid-cols-3 gap-3">
          {items.map((it) => (
            <MenuItem key={it.label} item={it as any} onClose={onClose} />
          ))}
        </div>
      </div>
    </div>
  )
}

function MenuItem({ item, onClose }: { item: { icon: React.ReactNode; label: string; action: string; highlight?: boolean }, onClose: () => void }) {
  const navigate = (window as any).routerNavigate as ((path: string) => void) | undefined
  async function handleClick() {
    switch (item.action) {
      case 'live':
        navigate && navigate('/live')
        break
      case 'export':
        navigate && navigate('/playback')
        break
      case 'settings':
        navigate && navigate('/settings/webrtc')
        break
      case 'hdd':
        navigate && navigate('/settings/media-source')
        break
      case 'image':
        navigate && navigate('/ai-engine')
        break
      case 'shutdown':
        try {
          const ok = window.confirm('Are you sure you want to shutdown the system?')
          if (!ok) break
          await apiService.systemShutdown()
          alert('Shutdown requested. The system may go offline shortly.')
        } catch (e: any) {
          alert(e?.message || 'Failed to request shutdown')
        }
        break
      default:
        break
    }
    onClose()
  }
  return (
    <button onClick={handleClick} className={`flex flex-col items-center gap-2 py-3 bg-[var(--bg-2)] border ${item.highlight ? 'border-[var(--accent)]' : 'border-neutral-700'} hover:border-[var(--accent)]`}>
      <div className="w-10 h-10 flex items-center justify-center bg-[var(--panel-2)] border border-neutral-700">
        {item.icon}
      </div>
      <span className="text-xs">{item.label}</span>
    </button>
  )
}

// Add Camera Dialog Component
function AddCameraDialog({ 
  onClose, 
  onCameraAdded,
  onCameraSelected,
  existingCameras 
}: { 
  onClose: () => void
  onCameraAdded: (cameraId?: number) => void
  onCameraSelected?: (cameraId: number) => void
  existingCameras: Array<{id: number, name: string}>
}) {
  const [mode, setMode] = useState<'discover' | 'select' | 'manual'>('discover')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  
  // ONVIF discovery state
  const [discovering, setDiscovering] = useState(false)
  const [discoveredCameras, setDiscoveredCameras] = useState<Array<{ip: string, name?: string, manufacturer?: string}>>([])
  const [selectedCamera, setSelectedCamera] = useState<{ip: string, name?: string} | null>(null)
  
  // Authentication step
  const [authStep, setAuthStep] = useState(false)
  const [credentials, setCredentials] = useState({ username: 'admin', password: '' })
  const [authenticating, setAuthenticating] = useState(false)
  const [profiles, setProfiles] = useState<Array<{token: string, name: string, stream_uri?: string, width?: number, height?: number}>>([])
  const [selectedProfile, setSelectedProfile] = useState<string>('')
  const [rtspUrl, setRtspUrl] = useState('')
  const [deviceInfo, setDeviceInfo] = useState<{
    manufacturer?: string
    model?: string
    firmwareversion?: string
    serialnumber?: string
    hardwareid?: string
  } | null>(null)
  const [cameraName, setCameraName] = useState('')
  
  // Manual entry form
  const [form, setForm] = useState({
    name: '',
    ip_address: '',
    port: 554,
    username: '',
    password: '',
    rtsp_url: '',
  })

  // Discover cameras on network via ONVIF
  const handleDiscover = async () => {
    setDiscovering(true)
    setError(null)
    setDiscoveredCameras([])
    
    try {
      const response = await apiService.onvifDiscover()
      const devices = response?.data?.devices || []
      setDiscoveredCameras(devices.map((d: any) => ({
        ip: d.ip || d.host || d.address,
        name: d.name || d.model || `Camera`,
        manufacturer: d.manufacturer || d.mfr || ''
      })))
      if (devices.length === 0) {
        setError('No ONVIF cameras found on network. Try manual entry.')
      }
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Discovery failed. Try manual entry.')
    } finally {
      setDiscovering(false)
    }
  }
  
  // Start discovery on mount
  useEffect(() => {
    if (mode === 'discover') {
      handleDiscover()
    }
  }, [])

  // Select a discovered camera and show auth step
  const handleSelectDiscovered = (camera: {ip: string, name?: string}) => {
    setSelectedCamera(camera)
    setAuthStep(true)
    setError(null)
    setCameraName('') // Reset camera name for user input
  }
  
  // Authenticate and get RTSP URL using HTTP Digest (Hikvision compatible)
  const handleAuthenticate = async () => {
    if (!selectedCamera || !credentials.username || !credentials.password) {
      setError('Username and password are required')
      return
    }
    
    setAuthenticating(true)
    setError(null)
    
    try {
      // Use new onvifConnect endpoint which uses HTTP Digest auth
      // This works with Hikvision and other devices that don't support WS-Security
      const response = await apiService.onvifConnect(selectedCamera.ip, {
        username: credentials.username,
        password: credentials.password,
        port: 80
      })
      
      const data = response?.data || {}
      const profileList = data.profiles || []
      
      if (profileList.length > 0) {
        setProfiles(profileList)
        setDeviceInfo(data.device_info || null)
        
        // Select first profile that has a stream URI
        const firstWithUri = profileList.find((p: any) => p.stream_uri) || profileList[0]
        setSelectedProfile(firstWithUri.token)
        setRtspUrl(firstWithUri.stream_uri || '')
        
        if (!firstWithUri.stream_uri) {
          setError('Could not get RTSP URL. Check camera settings.')
        }
      } else {
        setError('No stream profiles found. Check credentials.')
      }
    } catch (e: any) {
      const detail = e?.response?.data?.detail || e?.data?.detail || e?.message || ''
      if (detail.includes('401') || detail.toLowerCase().includes('authentication')) {
        setError('Authentication failed. Check username and password.')
      } else if (detail.includes('timeout') || detail.includes('connect')) {
        setError('Cannot connect to camera. Check IP address and network.')
      } else {
        setError(detail || 'Connection failed. Check credentials and network.')
      }
    } finally {
      setAuthenticating(false)
    }
  }
  
  // Handle profile change - use stored stream_uri from profiles list
  const handleProfileChange = (profileToken: string) => {
    setSelectedProfile(profileToken)
    const profile = profiles.find(p => p.token === profileToken)
    if (profile?.stream_uri) {
      setRtspUrl(profile.stream_uri)
    }
  }

  // Helper to embed credentials into RTSP URL
  const embedCredentialsInRtspUrl = (url: string, username: string, password: string): string => {
    try {
      // Parse the URL
      const urlObj = new URL(url)
      // URL-encode the password (handle special chars like @)
      const encodedPassword = encodeURIComponent(password)
      // Set credentials
      urlObj.username = username
      urlObj.password = encodedPassword
      return urlObj.toString()
    } catch {
      // If URL parsing fails, try manual insertion
      if (url.startsWith('rtsp://')) {
        const encodedPassword = encodeURIComponent(password)
        return url.replace('rtsp://', `rtsp://${username}:${encodedPassword}@`)
      }
      return url
    }
  }

  // Add discovered camera
  const handleAddDiscoveredCamera = async () => {
    if (!selectedCamera || !rtspUrl) {
      setError('RTSP URL not available')
      return
    }
    
    if (!cameraName.trim()) {
      setError('Camera name is required')
      return
    }
    
    setLoading(true)
    setError(null)
    
    try {
      // Embed credentials into RTSP URL for MediaMTX
      const rtspWithCredentials = embedCredentialsInRtspUrl(rtspUrl, credentials.username, credentials.password)
      
      const response = await apiService.createCamera({
        name: cameraName.trim(),
        ip_address: selectedCamera.ip,
        port: 554,
        username: credentials.username,
        password: credentials.password,
        rtsp_url: rtspWithCredentials,
        // ONVIF device metadata
        manufacturer: deviceInfo?.manufacturer || undefined,
        model: deviceInfo?.model || undefined,
        firmware_version: deviceInfo?.firmwareversion || undefined,
        serial_number: deviceInfo?.serialnumber || undefined,
        hardware_id: deviceInfo?.hardwareid || undefined,
      })
      const newCameraId = response?.data?.id
      
      // Auto-provision to MediaMTX
      if (newCameraId) {
        try {
          await apiService.provisionCameraMediaMTX(newCameraId, { enable_recording: false })
        } catch (e) {
          console.warn('Auto-provision failed, camera added but not streaming:', e)
        }
      }
      
      onCameraAdded(newCameraId)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to add camera')
    } finally {
      setLoading(false)
    }
  }

  // Add manual camera
  const handleAddManualCamera = async () => {
    if (!form.name.trim() || !form.ip_address.trim()) {
      setError('Name and IP address are required')
      return
    }
    if (!form.rtsp_url.trim()) {
      setError('RTSP URL is required')
      return
    }

    setLoading(true)
    setError(null)
    
    try {
      const response = await apiService.createCamera({
        name: form.name,
        ip_address: form.ip_address,
        port: form.port,
        username: form.username || undefined,
        password: form.password || undefined,
        rtsp_url: form.rtsp_url,
      })
      const newCameraId = response?.data?.id
      
      // Auto-provision to MediaMTX
      if (newCameraId) {
        try {
          await apiService.provisionCameraMediaMTX(newCameraId, { enable_recording: false })
        } catch (e) {
          console.warn('Auto-provision failed, camera added but not streaming:', e)
        }
      }
      
      onCameraAdded(newCameraId)
    } catch (e: any) {
      setError(e?.data?.detail || e?.message || 'Failed to add camera')
    } finally {
      setLoading(false)
    }
  }

  const handleSelectExisting = (cameraId: number) => {
    onCameraSelected?.(cameraId)
    onClose()
  }


  const filteredCameras = existingCameras.filter(c => 
    c.name.toLowerCase().includes(searchQuery.toLowerCase())
  )

  return (
    <div className="fixed inset-0 bg-black/70 z-50 flex items-center justify-center p-4">
      <div className="bg-[var(--panel)] border border-neutral-600 w-full max-w-lg shadow-xl max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between p-4 border-b border-neutral-700">
          <h3 className="font-semibold flex items-center gap-2">
            <Video size={18} />
            Add Camera to Tile
          </h3>
          <button 
            className="p-1 hover:bg-[var(--panel-2)] rounded"
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </div>

        {/* Tab Switcher */}
        <div className="flex border-b border-neutral-700">
          <button
            className={`flex-1 px-3 py-2 text-xs ${mode === 'discover' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
            onClick={() => { setMode('discover'); setAuthStep(false); setSelectedCamera(null); }}
          >
            <Search size={12} className="inline mr-1" />
            Discover
          </button>
          <button
            className={`flex-1 px-3 py-2 text-xs ${mode === 'manual' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
            onClick={() => setMode('manual')}
          >
            <Plus size={12} className="inline mr-1" />
            Manual
          </button>
          <button
            className={`flex-1 px-3 py-2 text-xs ${mode === 'select' ? 'bg-[var(--accent)]/20 text-[var(--accent)] border-b-2 border-[var(--accent)]' : 'text-[var(--text-dim)] hover:bg-[var(--panel-2)]'}`}
            onClick={() => setMode('select')}
          >
            <Camera size={12} className="inline mr-1" />
            Existing
          </button>
        </div>

        {/* Content */}
        <div className="p-4 overflow-auto flex-1">
          {error && (
            <div className="mb-4 p-2 bg-red-900/20 border border-red-800 text-red-400 text-sm">
              {error}
            </div>
          )}

          {/* DISCOVER MODE */}
          {mode === 'discover' && !authStep && (
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <span className="text-sm text-[var(--text-dim)]">
                  {discovering ? 'Scanning network...' : `Found ${discoveredCameras.length} camera(s)`}
                </span>
                <button
                  className="px-3 py-1 text-xs bg-blue-600 hover:bg-blue-700 text-white flex items-center gap-1"
                  onClick={handleDiscover}
                  disabled={discovering}
                >
                  {discovering ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />}
                  {discovering ? 'Scanning...' : 'Rescan'}
                </button>
              </div>
              
              <div className="max-h-60 overflow-auto space-y-2">
                {discovering && discoveredCameras.length === 0 && (
                  <div className="text-center py-8">
                    <Loader2 size={24} className="animate-spin mx-auto mb-2 text-[var(--accent)]" />
                    <div className="text-sm text-[var(--text-dim)]">Discovering ONVIF cameras...</div>
                  </div>
                )}
                
                {!discovering && discoveredCameras.length === 0 && (
                  <div className="text-center py-8 text-sm text-[var(--text-dim)]">
                    No cameras found. Try Manual entry.
                  </div>
                )}
                
                {discoveredCameras.map((camera, i) => (
                  <button
                    key={i}
                    className="w-full text-left px-3 py-3 bg-[var(--bg-2)] border border-neutral-700 hover:border-[var(--accent)] flex items-center gap-3 transition-colors"
                    onClick={() => handleSelectDiscovered(camera)}
                  >
                    <div className="w-10 h-10 bg-[var(--panel)] border border-neutral-600 flex items-center justify-center rounded">
                      <Camera size={20} className="text-[var(--accent)]" />
                    </div>
                    <div className="flex-1">
                      <div className="text-sm font-medium">{camera.name || 'ONVIF Camera'}</div>
                      <div className="text-xs text-[var(--text-dim)]">{camera.ip}</div>
                      {camera.manufacturer && (
                        <div className="text-xs text-[var(--text-dim)]">{camera.manufacturer}</div>
                      )}
                    </div>
                    <ChevronDown size={16} className="text-[var(--text-dim)] -rotate-90" />
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* DISCOVER MODE - AUTH STEP */}
          {mode === 'discover' && authStep && selectedCamera && (
            <div className="space-y-4">
              <button 
                className="text-xs text-[var(--accent)] flex items-center gap-1 hover:underline"
                onClick={() => { setAuthStep(false); setSelectedCamera(null); setProfiles([]); setRtspUrl(''); setCameraName(''); }}
              >
                ← Back to camera list
              </button>
              
              <div className="p-3 bg-[var(--bg-2)] border border-neutral-700 rounded">
                <div className="flex items-center gap-3">
                  <Camera size={24} className="text-[var(--accent)]" />
                  <div>
                    <div className="font-medium">{selectedCamera.name || 'ONVIF Camera'}</div>
                    <div className="text-xs text-[var(--text-dim)]">{selectedCamera.ip}</div>
                  </div>
                </div>
              </div>
              
              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">Camera Name *</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                  placeholder="e.g., Front Door, Lobby"
                  value={cameraName}
                  onChange={(e) => setCameraName(e.target.value)}
                />
              </label>
              
              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Username</span>
                  <input
                    type="text"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    value={credentials.username}
                    onChange={(e) => setCredentials(c => ({ ...c, username: e.target.value }))}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Password</span>
                  <input
                    type="password"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    value={credentials.password}
                    onChange={(e) => setCredentials(c => ({ ...c, password: e.target.value }))}
                  />
                </label>
              </div>
              
              {!rtspUrl && (
                <button
                  className="w-full px-4 py-2 text-sm bg-blue-600 hover:bg-blue-700 text-white flex items-center justify-center gap-2"
                  onClick={handleAuthenticate}
                  disabled={authenticating || !credentials.password}
                >
                  {authenticating ? (
                    <>
                      <Loader2 size={14} className="animate-spin" />
                      Connecting...
                    </>
                  ) : (
                    <>
                      <CheckCircle size={14} />
                      Connect & Get Stream
                    </>
                  )}
                </button>
              )}
              
              {profiles.length > 0 && (
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Stream Profile</span>
                  <select
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    value={selectedProfile}
                    onChange={(e) => handleProfileChange(e.target.value)}
                  >
                    {profiles.map(p => (
                      <option key={p.token} value={p.token}>
                        {p.name || p.token}
                        {p.width && p.height ? ` (${p.width}x${p.height})` : ''}
                      </option>
                    ))}
                  </select>
                </label>
              )}
              
              {deviceInfo && (
                <div className="p-3 bg-[var(--bg-2)] border border-neutral-700 rounded text-xs space-y-1">
                  <div className="font-medium text-sm mb-2">Device Information</div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[var(--text-dim)]">
                    <span>Manufacturer:</span>
                    <span className="text-[var(--text)]">{deviceInfo.manufacturer || 'Unknown'}</span>
                    <span>Model:</span>
                    <span className="text-[var(--text)]">{deviceInfo.model || 'Unknown'}</span>
                    {deviceInfo.serialnumber && (
                      <>
                        <span>Serial Number:</span>
                        <span className="text-[var(--text)] font-mono">{deviceInfo.serialnumber}</span>
                      </>
                    )}
                    {deviceInfo.firmwareversion && (
                      <>
                        <span>Firmware:</span>
                        <span className="text-[var(--text)]">{deviceInfo.firmwareversion}</span>
                      </>
                    )}
                    {deviceInfo.hardwareid && (
                      <>
                        <span>Hardware ID:</span>
                        <span className="text-[var(--text)] font-mono">{deviceInfo.hardwareid}</span>
                      </>
                    )}
                  </div>
                </div>
              )}
              
              {rtspUrl && (
                <div className="p-3 bg-green-900/20 border border-green-700 rounded">
                  <div className="flex items-center gap-2 text-green-400 text-sm mb-1">
                    <CheckCircle size={14} />
                    Stream URL Retrieved
                  </div>
                  <div className="text-xs font-mono text-[var(--text-dim)] break-all">{rtspUrl}</div>
                </div>
              )}
            </div>
          )}

          {/* MANUAL MODE */}
          {mode === 'manual' && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 gap-3">
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Camera Name *</span>
                  <input
                    type="text"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    placeholder="e.g., Front Door"
                    value={form.name}
                    onChange={(e) => setForm(f => ({ ...f, name: e.target.value }))}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">IP Address *</span>
                  <input
                    type="text"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    placeholder="192.168.1.100"
                    value={form.ip_address}
                    onChange={(e) => setForm(f => ({ ...f, ip_address: e.target.value }))}
                  />
                </label>
              </div>

              <div className="grid grid-cols-3 gap-3">
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Port</span>
                  <input
                    type="number"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    value={form.port}
                    onChange={(e) => setForm(f => ({ ...f, port: parseInt(e.target.value) || 554 }))}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Username</span>
                  <input
                    type="text"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    placeholder="admin"
                    value={form.username}
                    onChange={(e) => setForm(f => ({ ...f, username: e.target.value }))}
                  />
                </label>
                <label className="flex flex-col gap-1">
                  <span className="text-xs text-[var(--text-dim)]">Password</span>
                  <input
                    type="password"
                    className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm"
                    value={form.password}
                    onChange={(e) => setForm(f => ({ ...f, password: e.target.value }))}
                  />
                </label>
              </div>

              <label className="flex flex-col gap-1">
                <span className="text-xs text-[var(--text-dim)]">RTSP URL *</span>
                <input
                  type="text"
                  className="bg-[var(--bg-2)] border border-neutral-700 px-3 py-2 text-sm font-mono text-xs"
                  placeholder="rtsp://admin:password@192.168.1.100:554/stream1"
                  value={form.rtsp_url}
                  onChange={(e) => setForm(f => ({ ...f, rtsp_url: e.target.value }))}
                />
                <span className="text-[10px] text-[var(--text-dim)]">
                  Hikvision NVR: rtsp://user:pass@ip:554/Streaming/Channels/101
                </span>
              </label>
            </div>
          )}

          {/* SELECT EXISTING MODE */}
          {mode === 'select' && (
            <div className="space-y-3">
              <div className="relative">
                <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-neutral-500" />
                <input
                  type="text"
                  className="w-full bg-[var(--bg-2)] border border-neutral-700 pl-10 pr-3 py-2 text-sm"
                  placeholder="Search cameras..."
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                />
              </div>
              
              <div className="max-h-60 overflow-auto space-y-1">
                {filteredCameras.length === 0 ? (
                  <div className="text-center text-sm text-[var(--text-dim)] py-8">
                    {existingCameras.length === 0 
                      ? 'No cameras available. Add a new camera first.'
                      : 'No cameras match your search.'}
                  </div>
                ) : (
                  filteredCameras.map(camera => (
                    <button
                      key={camera.id}
                      className="w-full text-left px-3 py-2 bg-[var(--bg-2)] border border-neutral-700 hover:border-[var(--accent)] flex items-center gap-3 transition-colors"
                      onClick={() => handleSelectExisting(camera.id)}
                    >
                      <div className="w-8 h-8 bg-[var(--panel)] border border-neutral-600 flex items-center justify-center">
                        <Camera size={16} className="text-[var(--text-dim)]" />
                      </div>
                      <div>
                        <div className="text-sm font-medium">{camera.name}</div>
                        <div className="text-xs text-[var(--text-dim)]">Camera ID: {camera.id}</div>
                      </div>
                    </button>
                  ))
                )}
              </div>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 p-4 border-t border-neutral-700">
          <button
            className="px-4 py-2 text-sm border border-neutral-600 hover:bg-[var(--panel-2)]"
            onClick={onClose}
          >
            Cancel
          </button>
          {mode === 'discover' && authStep && rtspUrl && (
            <button
              className="px-4 py-2 text-sm bg-[var(--accent)] text-white disabled:opacity-50"
              onClick={handleAddDiscoveredCamera}
              disabled={loading || !cameraName.trim()}
            >
              {loading ? 'Adding...' : 'Add Camera'}
            </button>
          )}
          {mode === 'manual' && (
            <button
              className="px-4 py-2 text-sm bg-[var(--accent)] text-white disabled:opacity-50"
              onClick={handleAddManualCamera}
              disabled={loading || !form.name.trim() || !form.ip_address.trim() || !form.rtsp_url.trim()}
            >
              {loading ? 'Adding...' : 'Add Camera'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}


