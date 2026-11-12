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
import type { AspectOverride } from '../lib/aspect'
import { QrScanner } from '../components/QrScanner'
import { AddCameraDialog } from '../components/AddCameraDialog'
import { useFullscreen } from '../hooks/useFullscreen'
import { useClickOutside } from '../hooks/useClickOutside'
import { usePermissions } from '../hooks/usePermissions'
import { useCameraStatus } from '../hooks/useCameraStatus'
import { Camera, CameraOff, Maximize, Minimize, Play, Settings, Save, Image as ImageIcon, Book, HardDrive, Power, LayoutGrid, Menu as MenuIcon, Move, Square, Plus, Minus, ChevronDown, ChevronUp, Video, Search, AlertCircle, Expand, Scan, ScanEye, Tv, ArrowLeftRight, Minimize2 } from 'lucide-react'
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
import { useTranslation } from '../i18n'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useSnackbar } from '../components/Snackbar'
import { Modal } from '../components/Modal'
import { useConfirm } from '../components/ui/ConfirmDialog'
import { useCameras } from '../lib/queries'
import { MiniSegment, STATE_INK, type CamState } from './dashboard/WidgetFrame'
import { clsx } from 'clsx'
import { PREDEFINED_LAYOUTS, type LayoutDefinition } from '../lib/liveLayouts'
import { ToursMenu } from '../components/live/ToursMenu'
import { webcamOfflineLabel } from '../lib/hostAgent'

/** The dashboard wall's still-tile background, for tiles with no picture. */
const NO_PICTURE_BG = 'repeating-linear-gradient(135deg, #07090d 0 10px, #0b0f16 10px 20px)'

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
  const { t } = useTranslation()
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
  const [availableCameras, setAvailableCameras] = useState<Array<{id: number, name: string, live_online?: boolean | null, display_aspect_ratio?: AspectOverride | null}>>([])
  const [camerasLoaded, setCamerasLoaded] = useState(false)
  const { showWarning } = useSnackbar()

  // Cameras → View and the dashboard wall link here as /live?camera=<id>:
  // that camera is shown on its own. Deliberately kept out of the saved
  // display order — following a link must not rearrange the operator's wall.
  const [searchParams, setSearchParams] = useSearchParams()
  const focusParam = Number(searchParams.get('camera'))
  const focusCameraId = Number.isInteger(focusParam) && focusParam > 0 ? focusParam : null
  const focusCamera = focusCameraId ? availableCameras.find(c => c.id === focusCameraId) : undefined
  const exitFocus = () => setSearchParams({}, { replace: true })
  useEffect(() => {
    if (focusCameraId && camerasLoaded && !focusCamera) {
      showWarning(t('live.notFound', { id: focusCameraId }))
      setSearchParams({}, { replace: true })
    }
  }, [focusCameraId, camerasLoaded, focusCamera, setSearchParams, showWarning, t])

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

  // Detection overlay: boxes + label + score from Tier-0, drawn over every
  // live tile. Per-browser preference like the grid mode — an operator at
  // the wall who wants a clean picture turns it off once and it stays
  // off. On by default: the boxes are what shows the AI is working, and a
  // first look at the Live view should show it.
  const [showDetections, setShowDetections] = useState<boolean>(() => {
    try {
      return localStorage.getItem('liveview-detections') !== 'off'
    } catch {
      return true
    }
  })
  const toggleDetections = () => setShowDetections(prev => {
    const next = !prev
    try { localStorage.setItem('liveview-detections', next ? 'on' : 'off') } catch { /* private mode */ }
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
      // live_online is kept so a tile that mounts while its camera is already
      // down can say so on first paint. The events socket only reports
      // transitions, so without this the overlay waits for the camera to
      // change state — which, for one that is simply still offline, never
      // happens, and the tile just shows a dead player.
      const cameraList = cameras.map((cam: any) => ({
        id: cam.id,
        name: cam.name,
        live_online: cam.live_online,
        // The display-aspect override rides along so the tile can render an
        // anamorphic stream at its true shape — see lib/aspect.ts (#354).
        display_aspect_ratio: cam.display_aspect_ratio,
      }))
      setAvailableCameras(cameraList)
      setCamerasLoaded(true)

      // Update display order: add new cameras, remove deleted ones
      setCameraDisplayOrder(prevOrder => {
        const existingIds = new Set(cameraList.map((c: {id: number}) => c.id))
        // Deleted cameras become empty slots (0), NOT dropped — dropping
        // would shift every later camera left and lose the empty-tile
        // positions that drag-drop and tile assignment create.
        const filtered = prevOrder.map(id => (id === 0 || existingIds.has(id) ? id : 0))
        // Add new cameras that aren't in the order yet
        const newCameras = cameraList
          .filter((c: {id: number}) => !filtered.includes(c.id))
          .map((c: {id: number}) => c.id)
        const updated = [...filtered, ...newCameras]
        // Trim trailing empties.
        while (updated.length > 0 && !updated[updated.length - 1]) {
          updated.pop()
        }
        // Persist to localStorage
        try {
          localStorage.setItem('liveview-camera-display-order', JSON.stringify(updated))
        } catch {}
        return updated
      })
    }).catch(console.error)
  }
  
  // Assign a camera to a specific tile position. Same sparse convention as
  // swapTilePositions: 0 marks an empty tile, so the camera lands on exactly
  // the tile that was clicked (the old dense-pack version collapsed
  // placeholders, which made "select existing camera" a visible no-op).
  const assignCameraToTile = (tileIndex: number, cameraId: number) => {
    setCameraDisplayOrder(prev => {
      const updated = [...prev]
      while (updated.length <= tileIndex) {
        updated.push(0) // empty-slot placeholder
      }
      const from = updated.indexOf(cameraId)
      const displaced = updated[tileIndex]
      updated[tileIndex] = cameraId
      if (from !== -1 && from !== tileIndex) {
        // Move/swap: the clicked tile's previous camera (if any) takes the
        // selected camera's old slot.
        updated[from] = displaced || 0
      } else if (from === -1 && displaced && displaced !== cameraId) {
        // Camera wasn't placed yet but the tile was occupied — keep the
        // displaced camera visible by appending it.
        updated.push(displaced)
      }
      // Trim trailing empties so shorter layouts aren't padded forever.
      while (updated.length > 0 && !updated[updated.length - 1]) {
        updated.pop()
      }
      try {
        localStorage.setItem('liveview-camera-display-order', JSON.stringify(updated))
      } catch {}
      return updated
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
  
  const layoutDef = focusCameraId ? PREDEFINED_LAYOUTS['1x1'] : getLayoutDef()

  // Which tile each on-screen camera sits on, for the picker's "On tile N".
  const tileOf = new Map<number, number>()
  if (!focusCameraId) {
    layoutDef.tiles.forEach((_, i) => {
      if (cameraDisplayOrder[i]) tileOf.set(cameraDisplayOrder[i], i)
    })
  }

  // Picking a layout leaves the single-camera view.
  const selectLayout = (id: string) => {
    if (focusCameraId) exitFocus()
    setCurrentLayout(id)
  }

  // Fit the grid to the available area while keeping every cell 16:9: the
  // largest cell size is computed from the container's width AND height, so
  // videos fill their tiles edge-to-edge (overlays sit on the feed, not on
  // pillarbox bars) and the toolbar below stays on screen.
  const [gridSize, setGridSize] = useState<{ w: number; h: number } | null>(null)
  const GRID_GAP = 1 // matches the grid's Tailwind gap-px
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
    // One panel in the dashboard widgets' grammar: a slim header, then the
    // wall owns every remaining pixel, tiles split by hairlines.
    <section className="flex flex-col h-[calc(100vh-5rem)] border border-[var(--border)] bg-[var(--panel-2)] overflow-hidden">
      <header className="h-8 shrink-0 flex items-center gap-2 px-2.5 border-b border-[var(--border)] bg-[color-mix(in_oklab,var(--bg-2)_55%,var(--panel-2))]">
        <Tv size={13} className="text-[var(--text-dim)] shrink-0" aria-hidden="true" />
        <h1 className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text)] whitespace-nowrap">{t('live.title')}</h1>
        {focusCameraId ? (
          <>
            <span className="text-[11px] text-[var(--text)] truncate min-w-0">{focusCamera?.name ?? ''}</span>
            <HeadButton onClick={exitFocus} title={t('dashboard.backToWall')}>
              <Minimize2 size={11} aria-hidden="true" />
              {t('dashboard.backToWall')}
            </HeadButton>
          </>
        ) : (
          <span className="hidden sm:inline text-[11px] text-[var(--text-dim)] font-mono tabular-nums truncate min-w-0">
            {availableCameras.length} {t('dashboard.camerasPlural')} · {layoutDef.name}
          </span>
        )}
        <ToolbarContents
          currentLayout={focusCameraId ? '' : currentLayout}
          setCurrentLayout={selectLayout}
          availableLayouts={availableLayouts}
          onOpenMenu={() => setMenuOpen(true)}
          onToggleFullscreen={toggleFs}
          isFullscreen={isFullscreen}
          fillMode={fillMode}
          onToggleFillMode={toggleFillMode}
          showDetections={showDetections}
          onToggleDetections={toggleDetections}
        />
      </header>

      <DndContext 
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragStart={handleDragStart}
        onDragEnd={handleDragEnd}
      >
        <div ref={containerRef} className="flex-1 min-h-0 flex items-center justify-center bg-black">
        <div
          ref={gridRef}
          className="grid gap-px relative bg-[#1a1f29]"
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
            // cameraDisplayOrder maps tile index → camera id; 0 (or missing)
            // marks an empty tile, so empty slots can sit anywhere.
            const assignedCameraId = focusCameraId ?? (cameraDisplayOrder[i] || null)

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
                    hasCameraAssigned={!focusCameraId && !!assignedCameraId}
                  >
                    <Tile
                      index={i}
                      availableCameras={availableCameras}
                      assignedCameraId={assignedCameraId}
                      tileOf={tileOf}
                      onCameraSelected={(cameraId) =>
                        focusCameraId
                          ? setSearchParams({ camera: String(cameraId) }, { replace: true })
                          : assignCameraToTile(i, cameraId)}
                      onCameraAdded={loadCameras}
                      isDragging={activeDragId === `tile-${i}`}
                      canManage={canManageCameras}
                      showDetections={showDetections}
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
                title={fsToolbarVisible ? t('live.hideToolbar') : t('live.showToolbar')}
              >
                {fsToolbarVisible ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
              </button>
              {fsToolbarVisible && (
                <div className="pointer-events-auto self-stretch h-8 flex items-center gap-2 px-2.5 bg-[var(--panel-2)] border-t border-[var(--border)]">
                  <ToolbarContents
                    currentLayout={focusCameraId ? '' : currentLayout}
                    setCurrentLayout={selectLayout}
                    availableLayouts={availableLayouts}
                    onOpenMenu={() => setMenuOpen(true)}
                    onToggleFullscreen={toggleFs}
                    isFullscreen={isFullscreen}
                    showDetections={showDetections}
                    onToggleDetections={toggleDetections}
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
              <div className="w-32 bg-[var(--bg-2)] border-2 border-[var(--accent)] shadow-2xl overflow-hidden pointer-events-none">
                <div className="flex flex-col">
                  {/* Camera name header */}
                  <div className="bg-black/80 px-2 py-1 flex items-center justify-between">
                    <span className="text-[10px] font-medium text-white truncate">
                      {camera?.name || `Camera ${cameraId}`}
                    </span>
                    <span className="text-[8px] bg-[var(--critical)] px-1 text-white">{t('live.live')}</span>
                  </div>
                  {/* Preview area */}
                  <div className="h-16 bg-[var(--panel-2)] flex items-center justify-center">
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
    <div ref={setNodeRef} className="h-full relative">
      {children}
      {isOver && <div className="pointer-events-none absolute inset-0 z-40 ring-2 ring-inset ring-[var(--accent)]" />}
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
  tileOf,
  onCameraSelected,
  onCameraAdded,
  isDragging = false,
  canManage = false,
  showDetections = false,
}: {
  index: number
  availableCameras: Array<{id: number, name: string, live_online?: boolean | null, display_aspect_ratio?: AspectOverride | null}>
  assignedCameraId?: number | null
  tileOf: Map<number, number>
  onCameraSelected?: (cameraId: number) => void
  onCameraAdded?: () => void
  isDragging?: boolean
  showDetections?: boolean
  canManage?: boolean
}) {
  const { t } = useTranslation()
  const [cameraId, setCameraId] = useState<number | null>(null)
  const [cameraName, setCameraName] = useState<string>('')
  const [urls, setUrls] = useState<{ whep?: string; hls?: string; token?: string } | null>(null)
  const playerRef = useRef<VideoPlayerHandle>(null)
  const [ptzOpen, setPtzOpen] = useState(false)
  const [showCameraDialog, setShowCameraDialog] = useState(false)
  const [showPicker, setShowPicker] = useState(false)

  // Close the PTZ pad when the tile's camera changes.
  useEffect(() => {
    setPtzOpen(false)
  }, [assignedCameraId])
  // Backend-pushed connectivity: `status` drives the offline overlay;
  // `version` bumps on each recovery, re-running the URL fetch below (fresh
  // 60-min stream token) and remounting the player so the stream resumes
  // without any user action.
  const { status: connectivity, version: streamVersion } = useCameraStatus(assignedCameraId)
  // The socket carries transitions, so `connectivity` stays undefined for a
  // camera that was already down when this mounted. Fall back to the state the
  // camera list reported. Deliberately NOT fed into streamVersion: the player
  // remount on recovery has to stay driven by a real transition, or a list
  // refresh would tear down a healthy stream and re-mint its token.
  const effectiveConnectivity =
    connectivity ??
    (availableCameras.find(c => c.id === assignedCameraId)?.live_online === false
      ? 'offline'
      : undefined)
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
  const assignedCamera = availableCameras.find((c) => c.id === assignedCameraId)
  
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
  
  // Label state, in the dashboard wall's terms: the list query refreshes on
  // its own, so REC and the dot follow the recorder without a remount.
  const cams = useCameras()
  const listed = cams.data?.cameras?.find((c) => c.id === cameraId)
  const rec = listed?.recording_state
  const state: CamState =
    effectiveConnectivity === 'offline' ? 'offline'
      : connectivity === 'online' || (listed?.live_online ?? assignedCamera?.live_online) === true ? 'online'
        : 'degraded'

  return (
    <div className="group/tile relative h-full overflow-hidden bg-black">
      {/* Absolute so the <video>'s intrinsic size (e.g. a 1:1 stream) can't
          stretch this box — the player sizes itself to the stream's DISPLAY
          aspect inside it and letterboxes against black. */}
      <div className="absolute inset-0">
        {hasLink ? (
          <VideoPlayer
            key={`${cameraId}-${streamVersion}`}
            ref={playerRef}
            mode="live"
            chrome="controls"
            whepUrl={urls?.whep}
            hlsUrl={urls?.hls}
            mediamtxToken={urls?.token}
            onAuthExpired={handleAuthExpired}
            title={displayName}
            preferredStreamType="webrtc"
            autoPlay
            muted
            onSnapshot={handleSnapshot}
            displayAspectOverride={assignedCamera?.display_aspect_ratio}
            offlineLabel={webcamOfflineLabel(assignedCamera as any)}
            cameraId={cameraId}
            showDetections={showDetections}
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
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[color-mix(in_oklab,var(--danger)_30%,transparent)]" onClick={() => setPtzOpen(false)}>Close</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0.5, 0)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}>&rarr;</button>
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0, 0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}><Plus size={12} /></button>
                    <div />
                    <button className="px-1 py-1 bg-[var(--panel-2)] border border-[var(--border)] hover:bg-[var(--accent)]/30" onMouseDown={() => ptzMove(cameraId, 0, 0, -0.5)} onMouseUp={() => ptzStop(cameraId)} onMouseLeave={() => ptzStop(cameraId)}><Minus size={12} /></button>
                  </div>
                </div>
              ) : null}
            className="w-full h-full !bg-black"
          />
        ) : (
          <div className="w-full h-full flex flex-col items-center justify-center gap-2 text-white/45" style={{ background: NO_PICTURE_BG }}>
            {cameraId ? (
              <>
                <CameraOff size={18} />
                <span className="text-[10px] font-mono uppercase tracking-[0.2em]">{t('live.noStream')}</span>
              </>
            ) : canManage ? (
              <>
                <button
                  className="w-9 h-9 border border-dashed border-white/20 hover:border-[var(--accent)] hover:bg-[var(--accent)]/10 transition-colors flex items-center justify-center group"
                  onClick={() => setShowCameraDialog(true)}
                  title={t('live.addCamera')}
                >
                  <Plus size={16} className="text-white/50 group-hover:text-[var(--accent)]" />
                </button>
                <span className="text-[10px] font-mono uppercase tracking-[0.2em]">{t('live.clickToAdd')}</span>
              </>
            ) : (
              <>
                <span className="text-[10px] font-mono uppercase tracking-[0.2em]">{t('live.noAssigned')}</span>
                {availableCameras.length > 0 && (
                  <button
                    type="button"
                    className="text-[10px] px-2 py-1 border border-white/20 hover:border-[var(--accent)] hover:text-[var(--accent)]"
                    onClick={() => setShowPicker(true)}
                  >
                    {t('live.chooseCamera')}
                  </button>
                )}
              </>
            )}
          </div>
        )}
      </div>

      {/* Offline overlay — from the camera list on first paint, then from
          backend camera_status events. Clears itself (and the player
          restarts via the key above) when the camera comes back; no user
          interaction needed. */}
      {cameraId && effectiveConnectivity === 'offline' && (
        <div className="absolute inset-0 z-20 flex flex-col items-center justify-center gap-1.5 text-white/45 text-center" style={{ background: NO_PICTURE_BG }}>
          <AlertCircle size={18} className="text-[var(--on-video-warn)]" />
          <div className="text-[10px] font-mono uppercase tracking-[0.2em] text-[var(--on-video-warn)]">{t('live.cameraOffline')}</div>
          <div className="text-[10px] text-white/35">{t('live.waitingReconnect')}</div>
        </div>
      )}

      {/* Label band, as on the dashboard wall: state dot, name, REC — legible
          over any picture without a solid bar. Above the offline overlay so
          a dead tile still says which camera it is. */}
      {cameraId && (
        <>
          <div className="pointer-events-none absolute inset-x-0 top-0 z-30 h-9 bg-gradient-to-b from-black/75 to-transparent" />
          <div className="pointer-events-none absolute left-2 right-2 top-1.5 z-30 flex items-center gap-1.5 text-white">
            <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: STATE_INK[state] }} aria-hidden="true" />
            <span className="text-[11px] font-medium truncate [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">{displayName}</span>
            <span className="ml-auto shrink-0 flex items-center gap-2 leading-none text-[9px] font-bold tracking-[0.15em] [text-shadow:0_1px_2px_rgba(0,0,0,0.9)]">
              {!hasLink && <span className="text-[var(--on-video-warn)]">{t('live.noLink')}</span>}
              {rec === 'recording' ? (
                <span className="flex items-center gap-1 text-[var(--on-video-danger)]">
                  <span className="w-1.5 h-1.5 rounded-full bg-[var(--on-video-danger)] animate-pulse" aria-hidden="true" />REC
                </span>
              ) : rec === 'stalled' ? (
                <span className="text-[var(--on-video-warn)]">{t('dashboard.stalled').toUpperCase()}</span>
              ) : rec === 'degraded' ? (
                <span className="text-[var(--on-video-danger)]">{t('dashboard.statusDegraded').toUpperCase()}</span>
              ) : null}
            </span>
          </div>
        </>
      )}

      {/* Change camera — any tile that has one. Choosing which camera sits
          where is the viewer's own wall, so it needs no manage permission.
          Below the label band, clear of the player's bottom controls. */}
      {cameraId && (
        <button
          type="button"
          onPointerDown={(e) => e.stopPropagation()}
          onClick={() => setShowPicker(true)}
          className="absolute right-1.5 top-8 z-30 p-1.5 bg-black/60 hover:bg-black/80 text-white/90 border border-white/10 opacity-0 group-hover/tile:opacity-100 focus-visible:opacity-100 transition-opacity"
          title={t('live.changeCamera')}
          aria-label={t('live.changeCamera')}
        >
          <ArrowLeftRight size={12} />
        </button>
      )}

      {showPicker && (
        // The picker renders inside the draggable tile; keep its pointer
        // events (scrolling the list, say) from starting a tile drag.
        <div onPointerDown={(e) => e.stopPropagation()}>
        <CameraPicker
          cameras={availableCameras}
          currentId={cameraId}
          tileOf={tileOf}
          onPick={(id) => {
            setShowPicker(false)
            if (id !== cameraId) onCameraSelected?.(id)
          }}
          onClose={() => setShowPicker(false)}
        />
        </div>
      )}

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
  isFullscreen = false,
  dropUp = false,
  fillMode,
  onToggleFillMode,
  showDetections,
  onToggleDetections,
  showFitToggle = true,
}: {
  currentLayout: string
  setCurrentLayout: (layout: string) => void
  availableLayouts: Array<{ id: string; name: string; tiles: number }>
  onOpenMenu: () => void
  onToggleFullscreen: () => void
  isFullscreen?: boolean
  /** Layout dropdown direction: up when the bar sits at the bottom (fullscreen). */
  dropUp?: boolean
  fillMode?: boolean
  onToggleFillMode?: () => void
  showDetections?: boolean
  onToggleDetections?: () => void
  showFitToggle?: boolean
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [layoutDropdownOpen, setLayoutDropdownOpen] = useState(false)
  const dropdownRef = useRef<HTMLDivElement>(null)
  useClickOutside(dropdownRef, layoutDropdownOpen, () => setLayoutDropdownOpen(false))

  const quick = ['1x1', '2x2', '3x3', '4x4']
    .map((id) => availableLayouts.find((l) => l.id === id))
    .filter((l): l is { id: string; name: string; tiles: number } => !!l)

  return (
    <div className="ml-auto flex items-center gap-1 shrink-0">
      {/* Start a camera tour on this screen (views/TourView). */}
      <ToursMenu dropUp={dropUp} />
      {/* Quick layouts — collapse into the dropdown below md */}
      {quick.length > 0 && (
        <div className="hidden md:flex">
          <MiniSegment
            label={t('live.layouts')}
            value={currentLayout}
            options={quick.map((l) => ({ value: l.id, label: l.name }))}
            onChange={setCurrentLayout}
          />
        </div>
      )}
      {/* More layouts dropdown */}
      <div className="relative" ref={dropdownRef}>
        <HeadButton
          onClick={() => setLayoutDropdownOpen(!layoutDropdownOpen)}
          aria-haspopup="menu"
          aria-expanded={layoutDropdownOpen}
          aria-label={t('live.layouts')}
          title={t('live.layouts')}
        >
          <LayoutGrid size={12} />
          <ChevronDown size={11} />
        </HeadButton>
        {layoutDropdownOpen && (
          <div className={`absolute right-0 z-50 bg-[var(--panel)] border border-[var(--border)] shadow-lg min-w-[160px] ${dropUp ? 'bottom-full mb-1' : 'top-full mt-1'}`}>
            {availableLayouts.map(layout => (
              <button
                key={layout.id}
                className={`w-full text-left px-3 py-1.5 text-xs hover:bg-[var(--panel-2)] ${currentLayout === layout.id ? 'bg-[var(--accent)]/20 text-[var(--accent)]' : ''}`}
                onClick={() => { setCurrentLayout(layout.id); setLayoutDropdownOpen(false) }}
              >
                {layout.name} <span className="text-[var(--text-dim)] font-mono">({layout.tiles})</span>
              </button>
            ))}
            <div className="border-t border-[var(--border)] px-3 py-1.5">
              <button
                className="text-xs text-[var(--text-dim)] hover:text-[var(--accent)]"
                onClick={() => {
                  setLayoutDropdownOpen(false)
                  navigate('/settings/more-settings/window-settings')
                }}
              >
                ⚙ {t('live.configureLayouts')}
              </button>
            </div>
          </div>
        )}
      </div>
      <span className="w-px h-4 bg-[var(--border)] mx-0.5" aria-hidden="true" />
      {/* Fit/Fill toggle (hidden in fullscreen, which always fills) */}
      {showFitToggle && onToggleFillMode && (
        <HeadButton active={fillMode} onClick={onToggleFillMode} title={fillMode ? t('live.fill') : t('live.fit')}>
          {fillMode ? <Expand size={12} /> : <Scan size={12} />}
          <span className="hidden sm:inline">{fillMode ? t('live.fill') : t('live.fit')}</span>
        </HeadButton>
      )}
      {/* Detection overlay on/off — icon only, lit when on like Fit/Fill;
          the tooltip says what it draws. */}
      {onToggleDetections && (
        <HeadButton
          active={showDetections}
          onClick={onToggleDetections}
          aria-pressed={showDetections}
          aria-label={t('live.boxes')}
          title={showDetections
            ? `${t('live.boxes')}: on — tracked objects are outlined with label and confidence`
            : `${t('live.boxes')}: off — outline tracked objects with label and confidence`}
        >
          <ScanEye size={12} />
        </HeadButton>
      )}
      <HeadButton
        onClick={onToggleFullscreen}
        aria-label={isFullscreen ? t('video.exitFullscreen') : t('live.fullscreen')}
        title={isFullscreen ? t('video.exitFullscreen') : t('live.fullscreen')}
      >
        {isFullscreen ? <Minimize size={12} /> : <Maximize size={12} />}
      </HeadButton>
      <HeadButton onClick={onOpenMenu} title={t('live.menu')} aria-label={t('live.menu')}>
        <MenuIcon size={12} />
      </HeadButton>
    </div>
  )
}

/** A header-sized button, matching the dashboard's MiniSegment: lit when active. */
function HeadButton({ active = false, className, children, ...rest }: React.ButtonHTMLAttributes<HTMLButtonElement> & { active?: boolean }) {
  return (
    <button
      type="button"
      {...rest}
      className={clsx(
        'h-5 px-1.5 inline-flex items-center gap-1 text-[10px] leading-none border',
        active
          ? 'border-[var(--accent)] bg-[var(--accent)] text-white'
          : 'border-[var(--border)] text-[var(--text-dim)] hover:text-[var(--text)] hover:bg-[var(--panel)]',
        className,
      )}
    >
      {children}
    </button>
  )
}

/**
 * Pick the camera for one tile from every camera there is — the way to put a
 * camera on screen when there are more cameras than tiles. Choosing one that
 * is already on another tile swaps the two (see assignCameraToTile).
 */
function CameraPicker({
  cameras,
  currentId,
  tileOf,
  onPick,
  onClose,
}: {
  cameras: Array<{ id: number; name: string; live_online?: boolean | null }>
  currentId: number | null
  tileOf: Map<number, number>
  onPick: (cameraId: number) => void
  onClose: () => void
}) {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const q = query.trim().toLowerCase()
  const list = useMemo(
    () =>
      cameras
        .filter((c) => !q || c.name.toLowerCase().includes(q) || String(c.id) === q)
        .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true })),
    [cameras, q],
  )

  return (
    <Modal open title={t('live.chooseCamera')} onClose={onClose} widthClassName="w-[420px] max-w-[92vw]">
      <div className="relative">
        <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--text-dim)]" aria-hidden="true" />
        <input
          type="search"
          autoFocus
          className="w-full bg-[var(--bg-2)] border border-[var(--border)] focus:border-[var(--accent)] outline-none pl-8 pr-3 py-1.5 text-sm"
          placeholder={t('cameraDialog.search')}
          aria-label={t('cameraDialog.search')}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && list.length > 0) {
              e.preventDefault()
              onPick(list[0].id)
            }
          }}
        />
      </div>
      <div className="mt-2 max-h-[55vh] overflow-y-auto space-y-px">
        {list.length === 0 ? (
          <div className="text-center text-sm text-[var(--text-dim)] py-6">{t('catalog.noResults')}</div>
        ) : (
          list.map((c) => {
            const tile = tileOf.get(c.id)
            const current = c.id === currentId
            return (
              <button
                key={c.id}
                type="button"
                aria-current={current || undefined}
                onClick={() => onPick(c.id)}
                className={clsx(
                  'w-full text-left px-2.5 py-1.5 flex items-center gap-2 text-sm border',
                  current
                    ? 'border-[var(--accent)] bg-[var(--accent)]/10'
                    : 'border-transparent hover:bg-[var(--panel)] hover:border-[var(--border)]',
                )}
              >
                <span
                  className="w-1.5 h-1.5 rounded-full shrink-0"
                  style={{ background: STATE_INK[c.live_online === false ? 'offline' : c.live_online ? 'online' : 'degraded'] }}
                  aria-hidden="true"
                />
                <span className="truncate">{c.name}</span>
                {tile !== undefined && (
                  <span className="ml-auto shrink-0 text-[10px] font-mono text-[var(--text-dim)]">
                    {t('live.onTile', { n: tile + 1 })}
                  </span>
                )}
              </button>
            )
          })
        )}
      </div>
    </Modal>
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
    <Modal open title="Menu" onClose={onClose} widthClassName="w-[520px] max-w-[92vw]">
      <div className="grid grid-cols-3 gap-3">
        {items.map((it) => (
          <MenuItem key={it.label} item={it as any} onClose={onClose} />
        ))}
      </div>
    </Modal>
  )
}

function MenuItem({ item, onClose }: { item: { icon: React.ReactNode; label: string; action: string; highlight?: boolean }, onClose: () => void }) {
  const navigate = useNavigate()
  const { showSuccess, showError } = useSnackbar()
  const confirm = useConfirm()
  async function handleClick() {
    switch (item.action) {
      case 'live':
        navigate('/live')
        break
      case 'export':
        navigate('/playback/sync')
        break
      // /settings/webrtc was never a tab key; this is the real first tab.
      case 'settings':
        navigate('/settings/camera-config/device')
        break
      case 'hdd':
        navigate('/settings/media-source')
        break
      case 'image':
        navigate('/search')
        break
      // These two tiles did nothing at all before: no case handled them.
      case 'manual':
        navigate('/support')
        break
      case 'camera':
        navigate('/cameras')
        break
      case 'shutdown':
        try {
          const ok = await confirm({
            title: 'Shut down the system?',
            message: 'Live view, recording and every app stop until the machine is powered on again.',
            confirmLabel: 'Shut down',
            danger: true,
          })
          if (!ok) break
          await apiService.systemShutdown()
          showSuccess('Shutdown requested. The system may go offline shortly.')
        } catch (e: any) {
          showError(e?.message || 'Failed to request shutdown')
        }
        break
      default:
        break
    }
    onClose()
  }
  return (
    <button onClick={handleClick} className={`flex flex-col items-center gap-2 py-3 bg-[var(--bg-2)] border ${item.highlight ? 'border-[var(--accent)]' : 'border-[var(--border)]'} hover:border-[var(--accent)]`}>
      <div className="w-10 h-10 flex items-center justify-center bg-[var(--panel-2)] border border-[var(--border)]">
        {item.icon}
      </div>
      <span className="text-xs">{item.label}</span>
    </button>
  )
}

