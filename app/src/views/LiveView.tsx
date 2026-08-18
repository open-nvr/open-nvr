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

import { useCallback, useEffect, useRef, useState, useMemo } from 'react'
import { apiService } from '../lib/apiService'
import { rebaseToCurrentOrigin } from '../lib/streamUrl'
import { VideoPlayer, type VideoPlayerHandle } from '../components/VideoPlayer'
import { QrScanner } from '../components/QrScanner'
import { AddCameraDialog } from '../components/AddCameraDialog'
import { useFullscreen } from '../hooks/useFullscreen'
import { useClickOutside } from '../hooks/useClickOutside'
import { usePermissions } from '../hooks/usePermissions'
import { useCameraStatus } from '../hooks/useCameraStatus'
import { Camera, Maximize, Play, Settings, Save, Image as ImageIcon, Book, HardDrive, Power, X, Grid, Move, Square, Plus, Minus, ChevronDown, ChevronUp, Video, Search, AlertCircle, Expand, Scan } from 'lucide-react'
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
  const containerRef = useRef<HTMLDivElement>(null)
  const gridRef = useRef<HTMLDivElement>(null)
  const { toggle: toggleFs, isFullscreen } = useFullscreen(gridRef as React.RefObject<HTMLDivElement>)
  // FS toolbar visibility — toggled by the bottom-center handle button
  const [fsToolbarVisible, setFsToolbarVisible] = useState(false)
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
  
  // Grid sizing mode, persisted per browser. Fill (default) stretches cells
  // to use the whole area (feeds letterbox inside against the panel bg);
  // Fit keeps strict 16:9 cells centered.
  const [fillMode, setFillMode] = useState<boolean>(() => {
    try {
      return localStorage.getItem('liveview-grid-mode') !== 'fit'
    } catch {
      return true
    }
  })
  const toggleFillMode = () => setFillMode(prev => {
    const next = !prev
    try { localStorage.setItem('liveview-grid-mode', next ? 'fill' : 'fit') } catch { /* private mode */ }
    return next
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

  // Fit the grid to the available area while keeping every cell 16:9: the
  // largest cell size is computed from the container's width AND height, so
  // videos fill their tiles edge-to-edge (overlays sit on the feed, not on
  // pillarbox bars) and the toolbar below stays on screen.
  const [gridSize, setGridSize] = useState<{ w: number; h: number } | null>(null)
  const GRID_GAP = 8 // matches the grid's Tailwind gap-2
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const compute = () => {
      const cols = layoutDef.gridCols
      const rows = layoutDef.gridRows
      const availW = el.clientWidth - GRID_GAP * (cols - 1)
      const availH = el.clientHeight - GRID_GAP * (rows - 1)
      const cellW = Math.max(0, Math.min(availW / cols, (availH / rows) * (16 / 9)))
      setGridSize({
        w: cellW * cols + GRID_GAP * (cols - 1),
        h: cellW * (9 / 16) * rows + GRID_GAP * (rows - 1),
      })
    }
    compute()
    const ro = new ResizeObserver(compute)
    ro.observe(el)
    return () => ro.disconnect()
  }, [layoutDef.gridCols, layoutDef.gridRows])

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

  // Reset the toolbar when entering/leaving fullscreen so it never starts open.
  useEffect(() => {
    setFsToolbarVisible(false)
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
    // Fixed viewport-height layout: header + grid + toolbar must all fit
    // without a page scrollbar. 3rem = app header, 2rem = main's p-4.
    <section className="flex flex-col gap-2 h-[calc(100vh-5rem)]">
      {/* Header doubles as the toolbar — one row of chrome instead of two */}
      <header className="flex-shrink-0 flex items-center gap-2 bg-[var(--bg-2)] border border-[var(--border)] p-2 text-xs">
        <h1 className="text-lg font-semibold whitespace-nowrap mr-2">Live View</h1>
        <ToolbarContents
          currentLayout={currentLayout}
          setCurrentLayout={setCurrentLayout}
          availableLayouts={availableLayouts}
          onOpenMenu={() => setMenuOpen(true)}
          onToggleFullscreen={toggleFs}
          fillMode={fillMode}
          onToggleFillMode={toggleFillMode}
        />
      </header>

      <DndContext 
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div ref={containerRef} className="flex-1 min-h-0 flex items-center justify-center">
        <div
          ref={gridRef}
          className="grid gap-2 relative"
          style={{
            gridTemplateColumns: `repeat(${layoutDef.gridCols}, minmax(0, 1fr))`,
            gridTemplateRows: `repeat(${layoutDef.gridRows}, minmax(0, 1fr))`,
            // Fullscreen and Fill mode: take the whole container (cells may
            // deviate from 16:9; the player letterboxes internally). Fit mode
            // uses the fitted 16:9-cell size (see effect).
            ...(isFullscreen || fillMode || !gridSize
              ? { width: '100%', height: '100%' }
              : { width: gridSize.w, height: gridSize.h }),
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
          {/* Fullscreen toolbar overlay — opened via the bottom-center handle
              so it never pops up over the bottom tiles' own controls */}
          {isFullscreen && (
            <div className="pointer-events-none absolute inset-x-0 bottom-0 z-40 flex flex-col items-center">
              <button
                className="pointer-events-auto flex items-center justify-center w-12 h-5 bg-black/50 hover:bg-black/80 text-white/60 hover:text-white transition-colors"
                onClick={() => setFsToolbarVisible((v) => !v)}
                title={fsToolbarVisible ? 'Hide toolbar' : 'Show toolbar'}
              >
                {fsToolbarVisible ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
              </button>
              {fsToolbarVisible && (
                <div className="pointer-events-auto self-stretch flex items-center gap-2 bg-[var(--bg-2)] border border-[var(--border)] p-2 text-xs">
                  <ToolbarContents
                    currentLayout={currentLayout}
                    setCurrentLayout={setCurrentLayout}
                    availableLayouts={availableLayouts}
                    onOpenMenu={() => setMenuOpen(true)}
                    onToggleFullscreen={toggleFs}
                    dropUp
                    showFitToggle={false}
                  />
                </div>
              )}
            </div>
          )}
          {/* Menu overlay inside fullscreen so it appears over the live view */}
          {isFullscreen && menuOpen && <MenuOverlay onClose={() => setMenuOpen(false)} />}
        </div>
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

  // Close the PTZ pad when the tile's camera changes.
  useEffect(() => {
    setPtzOpen(false)
  }, [assignedCameraId])
  // Backend-pushed connectivity: `status` drives the offline overlay;
  // `version` bumps on each recovery, re-running the URL fetch below (fresh
  // 60-min stream token) and remounting the player so the stream resumes
  // without any user action.
  const { status: connectivity, version: streamVersion } = useCameraStatus(assignedCameraId)
  // Bumped when the player reports an auth-rejected stream request — the
  // 60-min token expired mid-session (e.g. a stream hiccup hours in; the
  // backend never saw the camera go offline, so streamVersion won't bump).
  // Re-runs the URL fetch below for a fresh token. Throttled so a
  // persistent non-auth 400 can't hammer the token endpoint.
  const [tokenVersion, setTokenVersion] = useState(0)
  const lastTokenRefreshRef = useRef(0)
  const handleAuthExpired = useCallback(() => {
    const now = Date.now()
    if (now - lastTokenRefreshRef.current < 10000) return
    lastTokenRefreshRef.current = now
    setTokenVersion((v) => v + 1)
  }, [])

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
            // Rebase onto the origin the UI is being served from — the
            // backend pins these to the LAN IP, which breaks playback
            // when browsing via https://localhost (see lib/streamUrl.ts).
            whep: rebaseToCurrentOrigin(data.urls?.webrtc),
            hls: rebaseToCurrentOrigin(data.urls?.hls),
            token: data.token
          })
        } catch {
          // Show NO LINK instead of silently keeping stale URLs/token.
          if (alive) setUrls(null)
        }
      })()
    } else {
      setCameraId(null)
      setCameraName('')
      setUrls(null)
    }
    return () => { alive = false }
  }, [assignedCameraId, availableCameras, streamVersion, tokenVersion])

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
    <div className="flex flex-col bg-[var(--bg-2)] border border-[var(--border)] relative overflow-hidden h-full">
      {/* Video container — fills the grid cell. Width comes from the grid
          column and height from the 1fr row, so the whole layout fits the
          viewport; object-contain letter/pillarboxes the stream inside. */}
      <div className="relative w-full flex-1 min-h-0 overflow-hidden">
        {!cameraId && <div className="absolute right-2 top-2 z-20 text-[10px] uppercase tracking-wide bg-black/60 px-1 py-0.5">NO CAMERA</div>}
        {!hasLink && cameraId && <div className="absolute right-2 top-2 z-20 text-[10px] uppercase tracking-wide bg-black/60 px-1 py-0.5">NO LINK</div>}

        {/* Absolute so the <video>'s intrinsic size (e.g. a 1:1 stream) can't
            stretch the box taller than 16:9 — the box height comes only from
            aspect-video, and object-contain letter/pillarboxes the stream. */}
        <div className="absolute inset-0">
          {hasLink ? (
            <VideoPlayer
              key={`${cameraId}-${streamVersion}`}
              ref={playerRef}
              mode="live"
              whepUrl={urls?.whep}
              hlsUrl={urls?.hls}
              mediamtxToken={urls?.token}
              onAuthExpired={handleAuthExpired}
              title={displayName}
              preferredStreamType="webrtc"
              autoPlay
              muted
              onSnapshot={handleSnapshot}
              onTogglePtz={() => setPtzOpen((s) => !s)}
              ptzActive={ptzOpen}
              overlay={ptzOpen && cameraId ? (
                /* Mini PTZ pad — rendered inside the player so it survives
                   the player element going fullscreen; sits above the
                   controls bar (~72px incl. gradient) */
                <div className="absolute left-2 bottom-20 z-30 bg-black/80 p-2 border border-[var(--border)] text-[10px]">
                  <div className="grid grid-cols-3 gap-1">
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&uarr;</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)]" onClick={() => ptzStop(cameraId)}><Square size={12} /></button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, -0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&darr;</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, -0.5, 0)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&larr;</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-red-500/30" onClick={() => setPtzOpen(false)}>Close</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0.5, 0)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&rarr;</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0, 0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}><Plus size={12} /></button>
                    <div />
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0, -0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}><Minus size={12} /></button>
                  </div>
                </div>
              ) : null}
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

        {/* Offline overlay — driven by backend camera_status events. Clears
            itself (and the player restarts via the key above) when the
            camera comes back; no user interaction needed. */}
        {cameraId && connectivity === 'offline' && (
          <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-2 bg-black/70 text-center">
            <AlertCircle size={24} className="text-yellow-400" />
            <div className="text-xs uppercase tracking-wide text-yellow-300">Camera offline</div>
            <div className="text-[11px] text-[var(--text-dim)]">Waiting for camera to reconnect…</div>
          </div>
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
  dropUp = false,
  fillMode,
  onToggleFillMode,
  showFitToggle = true,
}: {
  currentLayout: string
  setCurrentLayout: (layout: string) => void
  availableLayouts: Array<{ id: string; name: string; tiles: number }>
  onOpenMenu: () => void
  onToggleFullscreen: () => void
  /** Layout dropdown direction: up when the bar sits at the bottom (fullscreen). */
  dropUp?: boolean
  fillMode?: boolean
  onToggleFillMode?: () => void
  showFitToggle?: boolean
}) {
  const [layoutDropdownOpen, setLayoutDropdownOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)
  useClickOutside(dropdownRef, layoutDropdownOpen, () => setLayoutDropdownOpen(false))

  return (
    <>
      <button className="inline-flex items-center gap-1 px-2 py-1 bg-[var(--panel-2)] border border-[var(--border)]" onClick={onOpenMenu}>
        <Grid size={14} /> Menu
      </button>
      <div className="ml-auto flex items-center gap-1">
        {/* Quick layout buttons — collapse into the dropdown below md */}
        {['1x1', '2x2', '3x3', '4x4'].map((layoutId) => {
          const available = availableLayouts.find(l => l.id === layoutId)
          if (!available) return null
          return (
            <button
              key={layoutId}
              className={`px-2 py-1 border hidden md:inline-flex ${currentLayout === layoutId ? 'bg-[var(--accent)]/80 border-[var(--accent)]' : 'bg-[var(--panel-2)] border-[var(--border)]'}`}
              onClick={() => setCurrentLayout(layoutId)}
            >
              {available.name}
            </button>
          )
        })}
        {/* More layouts dropdown */}
        <div className="relative" ref={dropdownRef}>
          <button
            className="px-2 py-1 bg-[var(--panel-2)] border border-[var(--border)] inline-flex items-center gap-1"
            onClick={() => setLayoutDropdownOpen(!layoutDropdownOpen)}
          >
            <Grid size={14} />
            <ChevronDown size={12} />
          </button>
          {layoutDropdownOpen && (
            <div className={`absolute right-0 z-50 bg-[var(--panel)] border border-[var(--border)] shadow-lg min-w-[160px] ${dropUp ? 'bottom-full mb-1' : 'top-full mt-1'}`}>
              {availableLayouts.map(layout => (
                <button
                  key={layout.id}
                  className={`w-full text-left px-3 py-2 text-xs hover:bg-[var(--panel-2)] ${currentLayout === layout.id ? 'bg-[var(--accent)]/20 text-[var(--accent)]' : ''}`}
                  onClick={() => { setCurrentLayout(layout.id); setLayoutDropdownOpen(false) }}
                >
                  {layout.name} ({layout.tiles})
                </button>
              ))}
              <div className="border-t border-[var(--border)] px-3 py-2">
                <button
                  className="text-xs text-[var(--text-dim)] hover:text-[var(--accent)]"
                  onClick={() => {
                    setLayoutDropdownOpen(false)
                    ;(window as any).routerNavigate?.('/settings/more-settings/window-settings')
                  }}
                >
                  ⚙ Configure Layouts...
                </button>
              </div>
            </div>
          )}
        </div>
        {/* Fit/Fill toggle (hidden in fullscreen, which always fills) */}
        {showFitToggle && onToggleFillMode && (
          <button
            className={`px-2 py-1 border inline-flex items-center gap-1 ${fillMode ? 'bg-[var(--accent)]/80 border-[var(--accent)]' : 'bg-[var(--panel-2)] border-[var(--border)]'}`}
            onClick={onToggleFillMode}
            title={fillMode ? 'Fill: grid uses all available space' : 'Fit: strict 16:9 cells, centered'}
          >
            {fillMode ? <Expand size={14} /> : <Scan size={14} />}
            <span className="hidden sm:inline">{fillMode ? 'Fill' : 'Fit'}</span>
          </button>
        )}
        <button className="px-2 py-1 bg-[var(--panel-2)] border border-[var(--border)] inline-flex items-center gap-1" onClick={onToggleFullscreen} title="Fullscreen">
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

