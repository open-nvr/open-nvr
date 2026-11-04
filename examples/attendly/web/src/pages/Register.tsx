import { useEffect, useRef, useState, useMemo } from 'react'
import { authService } from '../services/auth'
import axios from 'axios'
import Layout from '../components/Layout'
import { config } from '../config/environment'

const API = config.API_BASE_URL

type AngleTag = 'front' | 'left' | 'right' | 'up' | 'down'
const ANGLES: AngleTag[] = ['front', 'left', 'right', 'up', 'down']

interface CapturedImage {
  blob: Blob
  quality: number
  timestamp: number
}

interface QualityMetrics {
  sharpness: number
  brightness: number
  faceDetected: boolean
  overallScore: number
}

interface CameraDevice {
  deviceId: string
  label: string
  kind: string
  groupId?: string
}

export default function Register() {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)

  const [studentCode, setStudentCode] = useState('')
  const [fullName, setFullName] = useState('')
  const [courseCode, setCourseCode] = useState('')
  const [section, setSection] = useState('')
  const [courseOptions, setCourseOptions] = useState<Array<{ code: string; name?: string }>>([])
  const [sectionOptions, setSectionOptions] = useState<string[]>([])
  const [sectionsLoading, setSectionsLoading] = useState<boolean>(false)
  const [email, setEmail] = useState('')
  const [captured, setCaptured] = useState<Record<AngleTag, CapturedImage | null>>({
    front: null,
    left: null,
    right: null,
    up: null,
    down: null,
  })
  const [message, setMessage] = useState<string>('')
  const [cameraOn, setCameraOn] = useState(false)
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [qualityMetrics, setQualityMetrics] = useState<QualityMetrics | null>(null)
  const [autoCapture, setAutoCapture] = useState(false)
  const [snackbar, setSnackbar] = useState<{
    open: boolean
    message: string
    type: 'success' | 'error' | 'info'
  }>({
    open: false,
    message: '',
    type: 'info'
  })
  const [cameraSettings, setCameraSettings] = useState({
    resolution: 'hd', // 'hd' or 'fhd'
    facingMode: 'user'
  })
  const [availableCameras, setAvailableCameras] = useState<CameraDevice[]>([])
  const [selectedCameraId, setSelectedCameraId] = useState<string>('')
  const [cameraPermissionGranted, setCameraPermissionGranted] = useState(false)

  // Quality assessment removed - the face model validates quality during registration

  // Camera detection and enumeration
  const detectCameras = async () => {
    try {
      // Request permission first
      await navigator.mediaDevices.getUserMedia({ video: true })
      setCameraPermissionGranted(true)
      
      // Get all media devices
      const devices = await navigator.mediaDevices.enumerateDevices()
      const videoDevices = devices.filter(device => device.kind === 'videoinput')
      
      // Map devices to our interface with better labels
      const cameras: CameraDevice[] = videoDevices.map((device, index) => {
        let label = device.label || `Camera ${index + 1}`
        
        // Enhance labels for better identification
        if (label.toLowerCase().includes('front') || label.toLowerCase().includes('user')) {
          label = `📱 Front Camera (${label})`
        } else if (label.toLowerCase().includes('back') || label.toLowerCase().includes('rear') || label.toLowerCase().includes('environment')) {
          label = `📷 Back Camera (${label})`
        } else if (label.toLowerCase().includes('webcam') || label.toLowerCase().includes('integrated')) {
          label = `💻 Webcam (${label})`
        } else if (label.toLowerCase().includes('usb')) {
          label = `🔌 USB Camera (${label})`
        }
        
        return {
          deviceId: device.deviceId,
          label,
          kind: device.kind,
          groupId: device.groupId
        }
      })
      
      setAvailableCameras(cameras)
      
      // Auto-select first camera if none selected
      if (cameras.length > 0 && !selectedCameraId) {
        setSelectedCameraId(cameras[0].deviceId)
      }
      
      showSnackbar(`Found ${cameras.length} camera(s)`, 'success')
    } catch (error) {
      console.error('Camera detection error:', error)
      showSnackbar('Camera permission denied or no cameras found', 'error')
      setCameraPermissionGranted(false)
    }
  }

  // Enhanced camera start with device selection
  const startCameraWithDevice = async (deviceId?: string) => {
    if (cameraOn) {
      stopCamera()
    }
    
    try {
      const constraints: MediaStreamConstraints = {
        video: deviceId ? {
          deviceId: { exact: deviceId },
          width: cameraSettings.resolution === 'fhd' ? { ideal: 1920, min: 1280 } : { ideal: 1280, min: 640 },
          height: cameraSettings.resolution === 'fhd' ? { ideal: 1080, min: 720 } : { ideal: 720, min: 480 },
          frameRate: { ideal: 30, min: 15 }
        } : {
          facingMode: cameraSettings.facingMode,
          width: cameraSettings.resolution === 'fhd' ? { ideal: 1920, min: 1280 } : { ideal: 1280, min: 640 },
          height: cameraSettings.resolution === 'fhd' ? { ideal: 1080, min: 720 } : { ideal: 720, min: 480 },
          frameRate: { ideal: 30, min: 15 }
        },
        audio: false
      }
      
      const stream = await navigator.mediaDevices.getUserMedia(constraints)
      streamRef.current = stream
      
      if (videoRef.current) {
        videoRef.current.srcObject = stream
        await videoRef.current.play()
      }
      setCameraOn(true)
      
      // Get actual camera label from the stream
      const track = stream.getVideoTracks()[0]
      const settings = track.getSettings()
      const selectedCamera = availableCameras.find(cam => cam.deviceId === deviceId)
      
      setMessage(`Camera started: ${selectedCamera?.label || 'Default camera'}`)
      showSnackbar(`Using: ${selectedCamera?.label || 'Default camera'}`, 'success')
    } catch (error) {
      console.error('Camera error:', error)
      setMessage('Failed to start camera. Please check permissions.')
      showSnackbar('Failed to start camera', 'error')
    }
  }

  // Snackbar helper functions
  const showSnackbar = (message: string, type: 'success' | 'error' | 'info' = 'info') => {
    setSnackbar({ open: true, message, type })
  }

  const hideSnackbar = () => {
    setSnackbar(prev => ({ ...prev, open: false }))
  }

  const startCamera = async () => {
    if (cameraOn) return
    
    // Use selected camera if available, otherwise use default
    const deviceId = selectedCameraId || undefined
    await startCameraWithDevice(deviceId)
  }

  const stopCamera = () => {
    streamRef.current?.getTracks().forEach(t => t.stop())
    streamRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
    setCameraOn(false)
    setQualityMetrics(null)
  }

  const captureAngle = async (angle: AngleTag) => {
    if (!videoRef.current) return
    
    const video = videoRef.current
    const canvas = canvasRef.current || document.createElement('canvas')
    canvasRef.current = canvas
    
    // Use higher resolution for better quality
    canvas.width = video.videoWidth || 1280
    canvas.height = video.videoHeight || 720
    
    const ctx = canvas.getContext('2d')!
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height)
    
    // Create high-quality blob (the face model checks quality during registration)
    const blob: Blob = await new Promise(resolve => 
      canvas.toBlob(b => resolve(b as Blob), 'image/jpeg', 0.95)!
    )
    
    const capturedImage: CapturedImage = {
      blob,
      quality: 1.0, // Always show as good - validated during registration
      timestamp: Date.now()
    }
    
    setCaptured(prev => ({ ...prev, [angle]: capturedImage }))
    setMessage(`${angle} captured successfully! Quality is validated during registration.`)
    showSnackbar(`📸 ${angle.toUpperCase()} angle captured successfully!`, 'success')
  }

  const clearAngle = (angle: AngleTag) => {
    setCaptured(prev => ({ ...prev, [angle]: null }))
    setMessage(`${angle} image cleared`)
  }

  // Auto-capture removed - quality is validated during registration
  useEffect(() => {
    if (!autoCapture || !cameraOn) return
    
    // Show basic metrics for UI feedback only
    const interval = setInterval(() => {
      setQualityMetrics({
        sharpness: 1.0,
        brightness: 0.5,
        faceDetected: true,
        overallScore: 1.0
      })
    }, 500)
    
    return () => clearInterval(interval)
  }, [autoCapture, cameraOn])

  // Deprecated: old flow created student before face registration
  const createStudent = async (): Promise<string | null> => null

  const submitRegistration = async () => {
    setIsSubmitting(true)
    
    try {
      const blobs: Array<{ angle: AngleTag; blob: Blob }> = []
      for (const angle of ANGLES) {
        const capturedImage = captured[angle]
        if (capturedImage) {
          blobs.push({ angle, blob: capturedImage.blob })
        }
      }
      
      if (blobs.length === 0) {
        setMessage('Capture at least one angle before submitting.')
        return
      }
      
      if (blobs.length < 3) {
        setMessage('For better accuracy, capture at least 3 different angles.')
      }
      
      const form = new FormData()
      form.append('full_name', fullName)
      form.append('course_code', courseCode)
      form.append('section', section)
      if (email) form.append('email', email)
      for (const { angle, blob } of blobs) {
        const file = new File([blob], `${angle}.jpg`, { type: 'image/jpeg' })
        form.append('images', file)
        form.append('angle_tags', angle)
      }
      
      const res = await axios.post(`${API}/students/register_new`, form, {
        headers: { 'Content-Type': 'multipart/form-data' },
      })
      
      const response = res.data
      let message = `Successfully registered ${response.num_faces_registered} faces for ${response.student.full_name}`
      
      // Show success snackbar
      showSnackbar(
        `🎉 Registration Complete! ${response.num_faces_registered} faces registered for ${response.student.full_name}`,
        'success'
      )
      
      if (response.validation_errors && response.validation_errors.length > 0) {
        message += `\n\nWarnings:\n${response.validation_errors.join('\n')}`
        setMessage(`Warnings:\n${response.validation_errors.join('\n')}`)
      } else {
        setMessage('')
      }
      
      // Clear captured images after successful submission
      setCaptured({
        front: null,
        left: null,
        right: null,
        up: null,
        down: null,
      })
      
    } catch (error: any) {
      console.error('Registration error:', error)
      let errorMessage = 'Failed to register faces. Please try again.'
      
      if (error.response?.data?.detail) {
        if (typeof error.response.data.detail === 'object') {
          errorMessage = `${error.response.data.detail.message}\n\nErrors:\n${error.response.data.detail.errors?.join('\n') || 'Unknown errors'}`
        } else {
          errorMessage = error.response.data.detail
        }
      }
      
      // Show error snackbar
      showSnackbar(`❌ Registration Failed: ${errorMessage.split('\n')[0]}`, 'error')
      setMessage(errorMessage)
    } finally {
      setIsSubmitting(false)
    }
  }

  const onSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setMessage('')
    const user = authService.getUser()
    if (user && user.is_admin === false) {
      setMessage("This feature is disabled during trial use.")
      return
    }
    await submitRegistration()
  }

  useEffect(() => {
    const init = async () => {
      await detectCameras()
      const first = selectedCameraId || (availableCameras[0]?.deviceId || undefined)
      await startCameraWithDevice(first)
    }
    init()
    const handleDeviceChange = () => {
      detectCameras()
    }
    try {
      navigator.mediaDevices?.addEventListener('devicechange', handleDeviceChange)
    } catch {}
    return () => {
      try {
        navigator.mediaDevices?.removeEventListener('devicechange', handleDeviceChange)
      } catch {}
      stopCamera()
    }
  }, [])

  // Load course and section options (similar to Video Register)
  useEffect(() => {
    const loadCourses = async () => {
      try {
        const res = await axios.get(`${API}/planner/courses`)
        const list = Array.isArray(res.data) ? res.data : []
        setCourseOptions(list.map((c: any) => ({ code: String(c.code), name: c.name ? String(c.name) : undefined })))
      } catch {
        setCourseOptions([])
      }
    }
    loadCourses()
  }, [])

  useEffect(() => {
    const loadSections = async () => {
      if (!courseCode) { setSectionOptions([]); setSection(''); return }
      try {
        setSectionsLoading(true)
        const res = await axios.get(`${API}/courses/${encodeURIComponent(courseCode)}/sections`)
        const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((s: any) => String(s)) : []
        setSectionOptions(arr)
      } catch {
        setSectionOptions([])
      } finally {
        setSectionsLoading(false)
      }
    }
    loadSections()
  }, [courseCode])

  const selectedCourseLabel = useMemo(() => {
    const found = courseOptions.find(c => c.code === courseCode)
    return found ? `${found.code}${found.name ? ` — ${found.name}` : ''}` : ''
  }, [courseOptions, courseCode])

  const formatCourseLabel = (code: string, name?: string) => {
    const full = `${code}${name ? ` — ${name}` : ''}`
    const limit = 36
    return full.length > limit ? `${full.slice(0, limit)}…` : full
  }

  // Handle camera selection change
  const handleCameraChange = async (deviceId: string) => {
    setSelectedCameraId(deviceId)
    if (cameraOn) {
      await startCameraWithDevice(deviceId)
    }
  }

  // Auto-hide snackbar after 5 seconds
  useEffect(() => {
    if (snackbar.open) {
      const timer = setTimeout(() => {
        hideSnackbar()
      }, 5000)
      return () => clearTimeout(timer)
    }
  }, [snackbar.open])

  // Quality helper functions removed - validated during registration

  return (
    <Layout>
      <div className="max-w-4xl mx-auto">
        <div className="mb-6">
          <h1 className="mb-1 text-2xl font-bold tracking-tight">Student Registration</h1>
          <p className="text-gray-600">Register students with faces via Images</p>
        </div>

        {/* Student Information Form */}
        <div className="bg-white rounded-lg shadow-sm border p-6 mb-6">
          {/* <h3 className="text-lg font-semibold text-gray-900 mb-4">Student Information</h3> */}
          <form onSubmit={onSubmit}>
            {/* Details Row (like Video Register): Full Name, Class, Submit */}
            <div className="flex flex-wrap items-end gap-3 mb-6">
              <div className="flex-1 min-w-[220px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Full Name</label>
                <input 
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors" 
                  placeholder="Enter full name" 
                  value={fullName} 
                  onChange={(e) => setFullName(e.target.value)} 
                  required 
                  disabled={isSubmitting}
                />
              </div>
              <div className="flex-1 min-w-[220px] max-w-[260px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Course</label>
                <select
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors truncate"
                  value={courseCode}
                  onChange={(e) => setCourseCode(e.target.value)}
                  required
                  name="courseCode"
                  disabled={isSubmitting}
                  title={selectedCourseLabel}
                >
                  <option value="">Select course</option>
                  {courseOptions.map(c => {
                    const full = `${c.code}${c.name ? ` — ${c.name}` : ''}`
                    const short = formatCourseLabel(c.code, c.name)
                    return (
                      <option key={c.code} value={c.code} title={full}>{short}</option>
                    )
                  })}
                </select>
              </div>
              <div className="flex-1 min-w-[120px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Section</label>
                <select
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                  value={section}
                  onChange={(e) => setSection(e.target.value)}
                  required
                  name="section"
                  disabled={isSubmitting || !courseCode}
                >
                  <option value="">{sectionsLoading ? 'Loading sections…' : 'Select section'}</option>
                  {!sectionsLoading && sectionOptions.map(sec => (<option key={sec} value={sec}>{sec}</option>))}
                </select>
              </div>
              <div className="flex-1 min-w-[220px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Email</label>
                <input 
                  type="email"
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors" 
                  placeholder="Enter email" 
                  value={email} 
                  onChange={(e) => setEmail(e.target.value)} 
                  required
                  disabled={isSubmitting}
                />
              </div>
              <div className="w-full sm:w-auto">
                <button
                  type="submit"
                  className="w-full sm:w-auto px-6 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 font-medium"
                  disabled={isSubmitting || Object.values(captured).every(img => img === null)}
                >
                  {isSubmitting ? 'Registering...' : 'Submit Registration'}
                </button>
              </div>
            </div>

            {/* Camera and Capture Section */}
            <div className="grid gap-6 lg:grid-cols-2">
              {/* Camera Section */}
              <div>
                <h4 className="text-sm font-medium text-gray-700 mb-3">Camera Preview</h4>
                <div className="bg-gray-100 rounded-lg p-4">
                  <video 
                    ref={videoRef} 
                    className="w-full h-64 bg-gray-900 rounded-lg" 
                    muted 
                    playsInline 
                    autoPlay
                  />
                  
                  {/* Camera Selection */}
                  <div className="mt-3">
                    <label className="block text-xs font-medium text-gray-600 mb-1">Select Camera</label>
                    <div className="flex gap-2 items-center">
                      <select
                        value={selectedCameraId}
                        onChange={(e) => handleCameraChange(e.target.value)}
                        className="flex-1 text-xs rounded border border-gray-300 px-2 py-1 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                        disabled={isSubmitting || availableCameras.length === 0}
                      >
                        {availableCameras.length > 0 ? (
                          availableCameras.map((camera) => (
                            <option key={camera.deviceId} value={camera.deviceId}>
                              {camera.label}
                            </option>
                          ))
                        ) : (
                          <option value="">No cameras detected</option>
                        )}
                      </select>
                      <button
                        type="button"
                        className="px-2 py-1 bg-gray-100 text-gray-700 rounded hover:bg-gray-200 text-xs"
                        onClick={detectCameras}
                        disabled={isSubmitting}
                        title="Refresh cameras"
                      >
                        🔄 Refresh
                      </button>
                    </div>
                    {availableCameras.length > 1 && (
                      <div className="mt-2 flex gap-2 flex-wrap">
                        {availableCameras.map((camera) => {
                          const lower = camera.label.toLowerCase()
                          const isFront = lower.includes('front') || lower.includes('user')
                          const isBack = lower.includes('back') || lower.includes('rear') || lower.includes('environment')
                          const isWebcam = lower.includes('webcam') || lower.includes('integrated')
                          let icon = '📷'
                          let shortLabel = 'Camera'
                          if (isFront) {
                            icon = '🤳'
                            shortLabel = 'Front'
                          } else if (isBack) {
                            icon = '📷'
                            shortLabel = 'Back'
                          } else if (isWebcam) {
                            icon = '💻'
                            shortLabel = 'Webcam'
                          }
                          return (
                            <button
                              key={camera.deviceId}
                              type="button"
                              onClick={() => handleCameraChange(camera.deviceId)}
                              className={`px-2 py-1 text-xs rounded border transition-colors ${
                                selectedCameraId === camera.deviceId
                                  ? 'bg-blue-100 border-blue-300 text-blue-700'
                                  : 'bg-gray-50 border-gray-200 text-gray-600 hover:bg-gray-100'
                              }`}
                              disabled={isSubmitting}
                              title={camera.label}
                            >
                              {icon} {shortLabel}
                            </button>
                          )
                        })}
                      </div>
                    )}
                    {!cameraPermissionGranted && availableCameras.length === 0 && (
                      <div className="mt-3 p-2 bg-yellow-50 border border-yellow-200 rounded text-xs">
                        <div className="flex items-center gap-2">
                          <span>⚠️</span>
                          <span className="text-yellow-700">
                            Camera permission required. Click "Refresh cameras" to grant access.
                          </span>
                        </div>
                        <button
                          type="button"
                          onClick={detectCameras}
                          className="mt-2 px-3 py-1 bg-yellow-200 text-yellow-800 rounded text-xs hover:bg-yellow-300"
                        >
                          Refresh cameras
                        </button>
                      </div>
                    )}
                  </div>

                  {/* Quick Camera Switch Buttons for Mobile */}
                  {/* Removed: duplicate quick switch (now embedded above) */}

                  {/* Simple Camera Info */}
                  <div className="mt-4 flex gap-3 flex-wrap">
                    {availableCameras.length > 0 && (
                      <div className="flex items-center text-xs text-gray-500 px-2">
                        📷 {availableCameras.length} camera(s) detected
                      </div>
                    )}
                  </div>
                </div>
              </div>

              {/* Capture Images */}
              <div>
                <h4 className="text-sm font-medium text-gray-700 mb-3">Capture Images</h4>
                <div className="grid gap-4 grid-cols-2 sm:grid-cols-3">
                  {ANGLES.map((angle) => (
                    <div key={angle} className="bg-white border rounded-lg p-3">
                      <div className="flex justify-between items-center mb-2">
                        <span className="text-sm font-medium capitalize">{angle}</span>
                        {captured[angle] && (
                          <span className="text-xs px-2 py-1 bg-green-100 text-green-700 rounded">
                            ✓
                          </span>
                        )}
                      </div>
                      
                      <div className="bg-gray-100 rounded-lg h-24 mb-3 flex items-center justify-center overflow-hidden">
                        {captured[angle] ? (
                          <img
                            alt={angle}
                            className="h-full w-full object-cover rounded-lg"
                            src={URL.createObjectURL(captured[angle]!.blob)}
                          />
                        ) : (
                          <span className="text-xs text-gray-400">No image</span>
                        )}
                      </div>
                      
                      <div className="flex gap-2">
                        <button
                          type="button"
                          className="flex-1 px-3 py-1 bg-blue-600 text-white rounded text-xs hover:bg-blue-700 disabled:opacity-50"
                          onClick={() => captureAngle(angle)}
                          disabled={!cameraOn || isSubmitting}
                        >
                          Capture
                        </button>
                        <button
                          type="button"
                          className="px-3 py-1 bg-gray-400 text-white rounded text-xs hover:bg-gray-500 disabled:opacity-50"
                          onClick={() => clearAngle(angle)}
                          disabled={!captured[angle] || isSubmitting}
                        >
                          Clear
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>

            {/* Student Information removed from below; submit moved to top row */}
          </form>
        </div>

        {/* Status Message */}
        {message && (
          <div className={`mt-4 p-4 rounded-lg border ${message.includes('Success') || message.includes('captured successfully') 
            ? 'bg-green-50 border-green-200 text-green-800' 
            : message.includes('Warning')
            ? 'bg-yellow-50 border-yellow-200 text-yellow-800'
            : 'bg-red-50 border-red-200 text-red-800'
          }`}>
            <pre className="whitespace-pre-wrap text-sm">{message}</pre>
          </div>
        )}

        {/* Hidden canvas for image processing */}
        <canvas ref={canvasRef} style={{ display: 'none' }} />

        {/* Simple Snackbar */}
        {snackbar.open && (
          <div className="fixed top-4 right-4 z-50 max-w-sm">
            <div className={`p-4 rounded-lg shadow-lg ${
              snackbar.type === 'success' 
                ? 'bg-green-500 text-white' 
                : snackbar.type === 'error'
                ? 'bg-red-500 text-white'
                : 'bg-blue-500 text-white'
            }`}>
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium">{snackbar.message}</span>
                <button
                  onClick={hideSnackbar}
                  className="ml-3 text-white hover:text-gray-200"
                  aria-label="Close"
                >
                  ×
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </Layout>
  )
}
