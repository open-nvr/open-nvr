import { useEffect, useRef, useState, useCallback, useMemo } from 'react'
import axios from 'axios'
import { authService } from '../services/auth'
import Layout from '../components/Layout'
import ResidenceFields, { type Residence } from '../components/ResidenceFields'
import { config } from '../config/environment'

const API = config.API_BASE_URL

interface VideoRegistrationState {
  isRecording: boolean
  isCapturing: boolean
  progress: number
  message: string
  framesCollected: number
  targetFrames: number
  hasFace: boolean
  isComplete: boolean
}

interface WebSocketMessage {
  type: string
  progress?: number
  message?: string
  has_face?: boolean
  frames_collected?: number
  target_frames?: number
  frame_captured?: boolean
  duration?: number
  registered_faces?: number
  failed_faces?: number
  total_frames?: number
  student_code?: string
  duplicate_student?: {
    student_code: string
    full_name: string
    class_name: string
    email?: string | null
    course_code?: string | null
    section?: string | null
    similarity: number
  }
}

const CAPTURE_INSTRUCTIONS = [
  "Position yourself so your face is clearly visible to the camera",
  "Adjust your distance: move closer or farther until your face fits well",
  "Ensure good lighting — avoid shadows or bright glare",
  "Keep your face fully visible (remove masks, caps, or obstructions)",
  "Move naturally — the system will automatically capture the best frames"
]

interface CameraDevice {
  deviceId: string
  label: string
  kind: string
  groupId?: string
}

type DialogVariant = 'success' | 'error' | 'warning' | 'info'
interface DialogState {
  open: boolean
  title: string
  content: React.ReactNode
  variant: DialogVariant
  onClose?: () => void
}

export default function VideoRegister() {
  const videoRef = useRef<HTMLVideoElement | null>(null)
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const frameIntervalRef = useRef<number | null>(null)
  const sessionFinalizedRef = useRef<boolean>(false)

  const [studentCode, setStudentCode] = useState('')
  const [fullName, setFullName] = useState('')
  const [courseCode, setCourseCode] = useState('')
  const [section, setSection] = useState('')
  const [courseOptions, setCourseOptions] = useState<Array<{ code: string; name?: string }>>([])
  const [sectionOptions, setSectionOptions] = useState<string[]>([])
  const [sectionsLoading, setSectionsLoading] = useState<boolean>(false)
  const [email, setEmail] = useState('')
  const [residence, setResidence] = useState<Residence>('day_scholar')
  const [hostelBlock, setHostelBlock] = useState('')
  const [sessionId, setSessionId] = useState('')
  const [registrationState, setRegistrationState] = useState<VideoRegistrationState>({
    isRecording: false,
    isCapturing: false,
    progress: 0,
    message: 'Ready to start registration',
    framesCollected: 0,
    targetFrames: 30,
    hasFace: false,
    isComplete: false
  })
  const [error, setError] = useState<string>('')
  const [cameraOn, setCameraOn] = useState(false)
  const [availableCameras, setAvailableCameras] = useState<CameraDevice[]>([])
  const [selectedCameraId, setSelectedCameraId] = useState<string>('')
  const [cameraPermissionGranted, setCameraPermissionGranted] = useState(false)
  const [dialog, setDialog] = useState<DialogState>({ open: false, title: '', content: null, variant: 'info' })

  const showDialog = (title: string, content: React.ReactNode, variant: DialogVariant = 'info', onClose?: () => void) => {
    setDialog({ open: true, title, content, variant, onClose })
  }

  const handleCloseDialog = () => {
    const onClose = dialog.onClose
    setDialog(prev => ({ ...prev, open: false }))
    if (onClose) {
      try { onClose() } catch {}
    }
  }

  const detectCameras = async (): Promise<CameraDevice[]> => {
    try {
      await navigator.mediaDevices.getUserMedia({ video: true })
      setCameraPermissionGranted(true)
      const devices = await navigator.mediaDevices.enumerateDevices()
      const videoDevices = devices.filter(d => d.kind === 'videoinput')
      const cameras: CameraDevice[] = videoDevices.map((device, index) => {
        let label = device.label || `Camera ${index + 1}`
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
          groupId: (device as any).groupId
        }
      })
      setAvailableCameras(cameras)
      if (cameras.length > 0 && !selectedCameraId) {
        setSelectedCameraId(cameras[0].deviceId)
      }
      return cameras
    } catch (err) {
      console.error('Camera detection error:', err)
      setCameraPermissionGranted(false)
      setError('Failed to access camera. Please allow camera permissions.')
      setAvailableCameras([])
      return []
    }
  }

  const startCameraWithDevice = async (deviceId?: string) => {
    if (cameraOn) {
      stopCamera()
    }
    try {
      setError('')
      const constraints: MediaStreamConstraints = {
        video: deviceId ? {
          deviceId: { exact: deviceId },
          width: { ideal: 1280, min: 640 },
          height: { ideal: 720, min: 480 },
          frameRate: { ideal: 30, min: 20 }
        } : {
          facingMode: 'user',
          width: { ideal: 1280, min: 640 },
          height: { ideal: 720, min: 480 },
          frameRate: { ideal: 30, min: 20 }
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
    } catch (err: any) {
      console.error('Camera error:', err)
      let errorMessage = 'Failed to access camera. '
      if (err.name === 'NotFoundError') {
        errorMessage += 'No camera found.'
      } else if (err.name === 'NotAllowedError') {
        errorMessage += 'Camera access denied. Please allow camera permissions.'
      } else if (err.name === 'NotReadableError') {
        errorMessage += 'Camera is being used by another application.'
      } else {
        errorMessage += 'Please ensure camera permissions are granted and try again.'
      }
      setError(errorMessage)
    }
  }

  const stopCamera = () => {
    streamRef.current?.getTracks().forEach(track => track.stop())
    streamRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
    setCameraOn(false)
  }

  const captureFrame = useCallback(() => {
    if (!videoRef.current || !canvasRef.current || !wsRef.current) return

    const video = videoRef.current
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')!

    // Higher resolution helps the face model
    canvas.width = 640
    canvas.height = 480
    ctx.drawImage(video, 0, 0, canvas.width, canvas.height)

    try {
      // Convert to base64 at good quality
      const dataURL = canvas.toDataURL('image/jpeg', 0.85)
      
      if (wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'frame',
          image: dataURL
        }))
      }
    } catch (error) {
      console.error('Error capturing frame:', error)
    }
  }, [])

  const startVideoRegistration = async () => {
    try {
      setError('')
      if (residence === 'hostler' && !hostelBlock.trim()) {
        setError('A hostler needs a hostel block.')
        return
      }
      sessionFinalizedRef.current = false
      // const user = authService.getUser()
      // if (user && user.is_admin === false) {
      //   setError("You don't have access. Ask admin for permissions.")
      //   return
      // }
      console.log('Starting video registration for:', fullName)
      
      // Start registration session
      const response = await axios.post(`${API}/video_registration/start_video_registration`, 
        new URLSearchParams({
          full_name: fullName,
          course_code: courseCode || '',
          section: section || '',
          email: email || ''
        }),
        {
          headers: { 'Content-Type': 'application/x-www-form-urlencoded' }
        }
      )
      
      console.log('Registration session started:', response.data)
      const sid = response.data?.session_id
      if (!sid) {
        setError('Failed to get session id')
        return
      }
      setSessionId(sid)

      // Connect WebSocket
      const wsUrl = `${config.WS_BASE_URL}/video_registration/ws/${sid}?token=${encodeURIComponent(localStorage.getItem('access_token') || '')}`
      console.log('Connecting to WebSocket:', wsUrl)
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        console.log('WebSocket connected successfully')
        setRegistrationState(prev => ({ 
          ...prev, 
          isRecording: true, 
          message: 'Starting registration...' 
        }))

        // Send init payload with details so server can allocate code later
        try {
          wsRef.current?.send(JSON.stringify({
            type: 'init',
            full_name: fullName,
            class_name: '',
            course_code: courseCode || '',
            section: section || '',
            email: email || '',
            residence,
            hostel_block: residence === 'hostler' ? hostelBlock.trim() : ''
          }))
        } catch {}
        
        // Start sending frames at moderate frequency for quality
        frameIntervalRef.current = window.setInterval(captureFrame, 400) // ~2.5 FPS for quality
      }

      ws.onmessage = (event) => {
        try {
          const data: WebSocketMessage = JSON.parse(event.data)
          console.log('WebSocket message:', data)

          switch (data.type) {
            case 'student_code_assigned':
              if (data.student_code) {
                setStudentCode(data.student_code as any)
              }
              break
            case 'start_capture':
              setRegistrationState(prev => ({
                ...prev,
                progress: data.progress || 0,
                message: data.message || 'Get ready to capture frames...',
                targetFrames: data.target_frames || 30
              }))
              // Send start capture signal after a brief delay
              setTimeout(() => {
                if (wsRef.current?.readyState === WebSocket.OPEN) {
                  wsRef.current.send(JSON.stringify({ type: 'start_capture' }))
                }
              }, 2000)
              break

            case 'capture_started':
              setRegistrationState(prev => ({
                ...prev,
                isCapturing: true,
                message: data.message || 'Capturing frames...',
                progress: data.progress || 0
              }))
              break

            case 'feedback':
              setRegistrationState(prev => ({
                ...prev,
                hasFace: data.has_face || false,
                framesCollected: data.frames_collected || 0,
                progress: data.progress || prev.progress,
                message: data.message || prev.message
              }))
              break

            case 'capture_complete':
              setRegistrationState(prev => ({
                ...prev,
                isCapturing: false,
                message: data.message || 'Processing frames...',
                progress: 100
              }))
              break

            case 'processing':
              setRegistrationState(prev => ({
                ...prev,
                message: data.message || 'Processing frames...',
                progress: data.progress || prev.progress
              }))
              break

            case 'duplicate_detected':
              setRegistrationState(prev => ({
                ...prev,
                isComplete: false,
                isRecording: false,
                message: data.message || 'Duplicate face detected'
              }))
              setError('')
              stopRecording()
              sessionFinalizedRef.current = true
              
              // Show duplicate detection dialog
              if (data.duplicate_student) {
                const duplicate = data.duplicate_student
                setTimeout(() => {
                  showDialog(
                    'Duplicate Registration Detected',
                    (
                      <div className="space-y-2 text-sm">
                        <p className="text-gray-700">This face is already registered in the system:</p>
                        <div className="rounded border bg-gray-50 p-3 text-gray-800">
                          <div><span className="font-medium">Name:</span> {duplicate.full_name}</div>
                          {duplicate.course_code && (
                            <div>
                              <span className="font-medium">Course:</span>{' '}
                              {(() => {
                                const match = courseOptions.find(c => c.code === (duplicate.course_code as string))
                                const full = `${duplicate.course_code}${match?.name ? ` — ${match.name}` : ''}`
                                const short = formatCourseLabel(duplicate.course_code as string, match?.name)
                                return <span title={full}>{short}</span>
                              })()}
                            </div>
                          )}
                          {duplicate.section && (
                            <div><span className="font-medium">Section:</span> {duplicate.section}</div>
                          )}
                          {duplicate.email && (
                            <div><span className="font-medium">Email:</span> {duplicate.email}</div>
                          )}
                          {duplicate.class_name && (
                            <div><span className="font-medium">Class:</span> {duplicate.class_name}</div>
                          )}
                          <div><span className="font-medium">Match Similarity:</span> {duplicate.similarity.toFixed(1)}%</div>
                        </div>
                        <p className="text-gray-600">The current registration has been cancelled to prevent duplicate entries.
                        If this is a different individual, please retry with clearer face visibility.</p>
                      </div>
                    ),
                    'warning',
                    () => {
                      resetRegistration()
                      setFullName('')
                      setCourseCode('')
                      setSection('')
                      setStudentCode('')
                      setSessionId('')
                      setError('')
                    }
                  )
                }, 200)
              }
              break

            case 'registration_complete':
              setRegistrationState(prev => ({
                ...prev,
                isComplete: true,
                isRecording: false,
                message: (data.registered_faces || 0) > 0 
                  ? 'Student registered successfully'
                  : 'Registration failed'
              }))
              if ((data as any).student_code) {
                setStudentCode((data as any).student_code)
              }
              stopRecording()
              sessionFinalizedRef.current = true
              setError('')
              
              // Show completion notification
              if ((data.registered_faces || 0) > 0) {
                setTimeout(() => {
                  showDialog(
                    'Registration Successful',
                    (
                      <div className="text-sm text-gray-800 space-y-2">
                        <div className="flex items-center gap-2 text-green-700"><span>✅</span><span>Student registered successfully.</span></div>
                        <div className="grid grid-cols-2 gap-2 bg-green-50 border border-green-200 rounded p-3">
                          <div className="font-medium">Images Captured</div>
                          <div>{data.registered_faces || 0}</div>
                          <div className="font-medium">Images Processed</div>
                          <div>{data.total_frames || 0}</div>
                        </div>
                        <p>Your face has been securely enrolled and can now be recognized by the attendance system.</p>
                      </div>
                    ),
                    'success',
                    () => {
                      // After successful registration, reset form and state for a fresh entry
                      resetRegistration()
                      setFullName('')
                      setCourseCode('')
                      setSection('')
                      setStudentCode('')
                    }
                  )
                }, 200)
              } else {
                setTimeout(() => {
                  showDialog(
                    'Registration Failed',
                    (
                      <div className="text-sm text-gray-800 space-y-2">
                        <div className="flex items-center gap-2 text-red-700"><span>❌</span><span>No face profiles could be saved.</span></div>
                        <div className="bg-red-50 border border-red-200 rounded p-3">
                          <div className="font-medium">Frames rejected</div>
                          <div>{data.failed_faces || 0}</div>
                        </div>
                        <p>Try again with better lighting and clearer face visibility.</p>
                      </div>
                    ),
                    'error'
                  )
                }, 200)
              }
              break

            case 'error':
              setError(data.message || 'An error occurred during registration')
              stopRecording()
              sessionFinalizedRef.current = true
              break
              
            default:
              console.warn('Unknown WebSocket message type:', data.type)
          }
        } catch (parseError) {
          console.error('Error parsing WebSocket message:', parseError)
          setError('Error processing server response')
        }
      }

      ws.onclose = (event) => {
        console.log('WebSocket disconnected:', event.code, event.reason)
        if (!sessionFinalizedRef.current) {
          setError('Connection lost during registration')
        }
        stopRecording()
      }

      ws.onerror = (error) => {
        console.error('WebSocket error:', error)
        setError('Connection error during registration')
        stopRecording()
      }

    } catch (err: any) {
      console.error('Registration error:', err)
      const status = err?.response?.status
      const msg = err?.response?.data?.message || err?.response?.data?.detail || err?.message
      if (status === 409 && typeof msg === 'string' && msg.toLowerCase().includes('email')) {
        setError('Email already registered')
        showDialog(
          'Duplicate Email',
          (
            <div className="text-sm text-gray-800 space-y-2">
              <div className="flex items-center gap-2 text-yellow-700"><span>⚠️</span><span>This email is already registered.</span></div>
              <p>Please use a different email address or look up the existing student in Student Data.</p>
            </div>
          ),
          'warning',
          () => {
            // Reset everything on dialog OK
            resetRegistration()
            // setFullName('')
            // setCourseCode('')
            // setSection('')
            // setEmail('')
            // setStudentCode('')
            // setSessionId('')
            setError('')
          }
        )
        return
      }
      setError(typeof msg === 'string' ? msg : 'Failed to start registration')
    }
  }

  const stopRecording = () => {
    if (frameIntervalRef.current) {
      clearInterval(frameIntervalRef.current)
      frameIntervalRef.current = null
    }
    
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
    
    setRegistrationState(prev => ({
      ...prev,
      isRecording: false
    }))
  }

  // Submit handler for HTML5 validation bubble
  const handleSubmitDetails = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    const form = e.currentTarget
    if (!form.checkValidity()) {
      form.reportValidity()
      return
    }
    // Reset state for a fresh session
    setError('')
    setStudentCode('')
    setRegistrationState({
      isRecording: false,
      isCapturing: false,
      progress: 0,
      message: 'Starting registration...',
      framesCollected: 0,
      targetFrames: 30,
      hasFace: false,
      isComplete: false
    })
    await startVideoRegistration()
  }

  // Load course and section options
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

  const selectedCourseLabel = useMemo(() => {
    const found = courseOptions.find(c => c.code === courseCode)
    return found ? `${found.code}${found.name ? ` — ${found.name}` : ''}` : ''
  }, [courseOptions, courseCode])

  const formatCourseLabel = (code: string, name?: string) => {
    const full = `${code}${name ? ` — ${name}` : ''}`
    const limit = 36
    return full.length > limit ? `${full.slice(0, limit)}…` : full
  }

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

  const handleCourseSelectChange = async (newCode: string) => {
    setCourseCode(newCode)
    setSection('')
    if (!newCode) { setSectionOptions([]); return }
    try {
      setSectionsLoading(true)
      const res = await axios.get(`${API}/courses/${encodeURIComponent(newCode)}/sections`)
      const arr = Array.isArray(res.data?.sections) ? res.data.sections.map((s: any) => String(s)) : []
      setSectionOptions(arr)
    } catch {
      setSectionOptions([])
    } finally {
      setSectionsLoading(false)
    }
  }

  const resetRegistration = () => {
    stopRecording()
    setRegistrationState({
      isRecording: false,
      isCapturing: false,
      progress: 0,
      message: 'Ready to start registration',
      framesCollected: 0,
      targetFrames: 30,
      hasFace: false,
      isComplete: false
    })
    setError('')
  }

  const verifyRegistration = async () => {
    try {
      setError('')
      // const user = authService.getUser()
      // if (user && user.is_admin === false) {
      //   setError("You don't have access. Ask admin for permissions.")
      //   return
      // }
      const response = await axios.get(`${API}/video_registration/verify_registration/${encodeURIComponent(studentCode)}`)
      const data = response.data
      
      if (data.registered) {
        showDialog(
          'Registration Verified',
          (
            <div className="text-sm text-gray-800 space-y-2">
              <div className="flex items-center gap-2 text-green-700"><span>✅</span><span>Successfully registered.</span></div>
              <div className="rounded border bg-green-50 border-green-200 p-3">
                <div><span className="font-medium">Student:</span> {studentCode}</div>
                <div><span className="font-medium">Face embeddings:</span> {data.face_count}</div>
              </div>
              <p>You can now use the attendance system.</p>
            </div>
          ),
          'success'
        )
      } else {
        showDialog(
          'Registration Not Found',
          (
            <div className="text-sm text-gray-800 space-y-2">
              <div className="flex items-center gap-2 text-red-700"><span>❌</span><span>No enrolled face samples found.</span></div>
              <div className="rounded border bg-red-50 border-red-200 p-3">
                <div><span className="font-medium">Student:</span> {studentCode}</div>
              </div>
              <p>Please try registering again with better lighting and face visibility.</p>
            </div>
          ),
          'error'
        )
      }
    } catch (err: any) {
      console.error('Verification error:', err)
      setError('Failed to verify registration. Please try again.')
    }
  }

  useEffect(() => {
    ;(async () => {
      const cameras = await detectCameras()
      const first = cameras[0]?.deviceId || selectedCameraId || undefined
      if (first) {
        await startCameraWithDevice(first)
      }
    })()
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
      stopRecording()
    }
  }, [])

  const handleCameraChange = async (deviceId: string) => {
    setSelectedCameraId(deviceId)
    await startCameraWithDevice(deviceId)
  }

  // No hard-coded height: rely on flexbox sizing from Layout to avoid scroll

  const handleCancelRegistration = () => {
    // Mark session finalized to suppress "connection lost" message on ws close
    sessionFinalizedRef.current = true
    stopRecording()
    resetRegistration()
    setFullName('')
    setCourseCode('')
    setSection('')
    setEmail('')
    setStudentCode('')
    setSessionId('')
    setError('')
  }


  return (
    <Layout>
      <div className="max-w-6xl mx-auto flex-1 min-h-0 flex flex-col">
        <div className="mb-3 flex-shrink-0">
          <h1 className="mb-1 text-2xl font-bold tracking-tight">Student Registration via Video</h1>
        </div>

        {/* Student details and Start/Cancel controls (inline, with native validation) */}
        <div className="bg-white rounded-lg shadow-sm border p-4 mb-4 flex-shrink-0">
          <form onSubmit={handleSubmitDetails} noValidate>
            <div className="flex flex-wrap items-end gap-3">
              <div className="flex-1 min-w-[220px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Full Name</label>
                <input
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                  placeholder="Enter full name"
                  value={fullName}
                  onChange={(e) => setFullName(e.target.value)}
                  required
                  name="fullName"
                  disabled={registrationState.isRecording}
                />
              </div>
              <div className="flex-1 min-w-[220px] max-w-[260px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Course</label>
                <select
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors truncate"
                  value={courseCode}
                  onChange={(e) => { handleCourseSelectChange(e.target.value) }}
                  onClick={(e) => {
                    const val = (e.target as HTMLSelectElement).value
                    if (val && sectionOptions.length === 0 && !sectionsLoading) {
                      handleCourseSelectChange(val)
                    }
                  }}
                  required
                  name="courseCode"
                  disabled={registrationState.isRecording}
                  title={selectedCourseLabel}
                >
                  <option value="">Select course</option>
                  {courseOptions.map(c => {
                    const full = `${c.code}${c.name ? ` — ${c.name}` : ''}`
                    const short = formatCourseLabel(c.code, c.name)
                    return (
                      <option key={c.code} value={c.code} title={full}>
                        {short}
                      </option>
                    )
                  })}
                </select>
                {/* Removed inline helper line under course select per request */}
              </div>
              <div className="flex-1 min-w-[120px]">
                <label className="block text-sm font-medium text-gray-700 mb-2">Section</label>
                <select
                  className="w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                  value={section}
                  onChange={(e) => setSection(e.target.value)}
                  onFocus={() => { if (courseCode && sectionOptions.length === 0 && !sectionsLoading) handleCourseSelectChange(courseCode) }}
                  required
                  name="section"
                  disabled={registrationState.isRecording || !courseCode}
                >
                  <option value="">{sectionsLoading ? 'Loading sections…' : 'Select section'}</option>
                  {!sectionsLoading && sectionOptions.map(sec => (<option key={sec} value={sec}>{sec}</option>))}
                </select>
                {courseCode && !sectionsLoading && sectionOptions.length === 0 && (
                  <div className="mt-1 text-[11px] text-gray-500">No sections found for selected course.</div>
                )}
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
                  name="email"
                  disabled={registrationState.isRecording}
                />
              </div>
              <ResidenceFields residence={residence} hostelBlock={hostelBlock} onResidence={setResidence} onHostelBlock={setHostelBlock} disabled={registrationState.isRecording} />
              {!registrationState.isRecording && (
                <div className="sm:ml-auto self-end">
                  <button
                    type="submit"
                    className="inline-flex items-center gap-2 px-5 py-2.5 bg-green-600 text-white rounded-md hover:bg-green-700 disabled:opacity-50 font-medium shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-green-600"
                  >
                    ▶️ Start Registration
                  </button>
                </div>
              )}
              {registrationState.isRecording && (
                <div className="sm:ml-auto self-end">
                  <button
                    type="button"
                    className="inline-flex items-center gap-2 px-5 py-2.5 bg-red-600 text-white rounded-md hover:bg-red-700 font-medium shadow-sm focus:outline-none focus:ring-2 focus:ring-offset-2 focus:ring-red-600"
                    onClick={handleCancelRegistration}
                  >
                    ✖️ Cancel Registration
                  </button>
                </div>
              )}
            </div>
            
            <div className="grid gap-4 lg:grid-cols-2 mt-4 flex-1 min-h-0">
          {/* Video and Controls */}
          <div className="space-y-4">
            {/* Heading removed to reclaim space */}
            <div className="bg-gray-100 rounded-lg p-4 h-full flex flex-col min-h-0 overflow-hidden">
              <div className="relative flex-1 min-h-[280px]">
                <video
                  ref={videoRef}
                  className="w-full h-full bg-gray-900 rounded-lg object-cover"
                  muted
                  playsInline
                  autoPlay
                />

                {/* Overlay instructions (confined to video area) */}
                {cameraOn && registrationState.isRecording && (
                  <div className="absolute top-2 left-2 right-2 bg-black bg-opacity-75 text-white p-2 rounded">
                    <div className="text-sm font-semibold">
                      Natural Face Capture
                    </div>
                    <div className="text-xs mt-1">{registrationState.message}</div>
                    {registrationState.isCapturing && (
                      <div className="flex items-center mt-1">
                        <span className="text-[11px] mr-2">Frames:</span>
                        <span className="text-xs">
                          {registrationState.framesCollected}/{registrationState.targetFrames}
                        </span>
                        {registrationState.hasFace ? (
                          <span className="ml-2 text-green-400 text-xs">✓ Face</span>
                        ) : (
                          <span className="ml-2 text-yellow-400 text-xs">⚠ Looking for face</span>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {/* Progress bar (confined to video area) */}
                {registrationState.isRecording && (
                  <div className="absolute bottom-2 left-2 right-2 bg-black bg-opacity-75 p-2 rounded">
                    <div className="flex justify-between text-white text-xs mb-1">
                      <span>Progress</span>
                      <span>{Math.round(registrationState.progress)}%</span>
                    </div>
                    <div className="w-full bg-gray-300 rounded-full h-1.5">
                      <div
                        className="bg-blue-600 h-1.5 rounded-full transition-all duration-300"
                        style={{ width: `${registrationState.progress}%` }}
                      />
                    </div>
                  </div>
                )}
              </div>

              {/* Camera Selection */}
              <div className="mt-2">
                <label className="block text-xs font-medium text-gray-600 mb-1">Select Camera</label>
                <div className="flex gap-2 items-center">
                  <select
                    value={selectedCameraId}
                    onChange={(e) => handleCameraChange(e.target.value)}
                    className="flex-1 text-xs rounded border border-gray-300 px-2 py-1 focus:border-blue-500 focus:ring-1 focus:ring-blue-500"
                    disabled={registrationState.isRecording || availableCameras.length === 0}
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
                    onClick={async () => {
                      const cams = await detectCameras()
                      const firstCam = cams[0]?.deviceId
                      if (firstCam) {
                        await startCameraWithDevice(firstCam)
                      }
                    }}
                    disabled={registrationState.isRecording}
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
                          disabled={registrationState.isRecording}
                          title={camera.label}
                        >
                          {icon} {shortLabel}
                        </button>
                      )
                    })}
                  </div>
                )}
                {!cameraPermissionGranted && availableCameras.length === 0 && (
                  <div className="mt-2 text-xs text-yellow-700 bg-yellow-50 border border-yellow-200 rounded px-2 py-1">
                    Allow camera permission to list devices.
                  </div>
                )}
              </div>

            </div>

            {/* Camera controls */}
            <div className="mt-4 flex gap-3 flex-wrap">
              {/* Stop Camera button removed per requirement */}

              {/* <button
                type="button"
                className="px-3 py-2 bg-gray-100 text-gray-700 rounded-lg hover:bg-gray-200 text-sm"
                onClick={async () => {
                  const cams = await detectCameras()
                  const first = cams[0]?.deviceId
                  if (first) {
                    await startCameraWithDevice(first)
                  }
                }}
                title="Refresh available cameras"
                disabled={registrationState.isRecording}
              >
                🔄 Refresh
              </button> */}

              {/* Cancel button moved to details row above */}

              {(registrationState.isComplete || error) && registrationState.isComplete && (
                <div className="flex gap-2">
                  <button
                    type="button"
                    className="px-4 py-2 bg-purple-600 text-white rounded-lg hover:bg-purple-700 font-medium"
                    onClick={verifyRegistration}
                  >
                    Verify Registration
                  </button>
                </div>
              )}
            </div>
            {/* Student Information removed from here (moved above) */}
          </div>

          {/* Status Panel */}
          <div className="relative flex flex-col gap-4 h-full min-h-0 overflow-auto">
            <div className="bg-white rounded-lg shadow-sm border p-4 flex-none">
              <h3 className="text-base font-semibold text-gray-900 mb-2">Registration Status</h3>
              
              <div className="space-y-3">
                <div>
                  <span className="text-sm font-medium text-gray-600">Progress:</span>
                  <div className="mt-1">
                    <div className="flex justify-between text-sm">
                      <span>Overall Progress</span>
                      <span>{Math.round(registrationState.progress)}%</span>
                    </div>
                    <div className="w-full bg-gray-200 rounded-full h-2 mt-1">
                      <div
                        className="bg-green-600 h-2 rounded-full transition-all duration-300"
                        style={{ width: `${registrationState.progress}%` }}
                      />
                    </div>
                  </div>
                </div>

                {registrationState.isRecording && (
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="px-2 py-1 rounded-full text-[11px] bg-blue-50 text-blue-700 border border-blue-200">
                      {registrationState.isCapturing ? 'Capturing' : 'Ready'}
                    </span>
                    <span className="px-2 py-1 rounded-full text-[11px] bg-gray-50 text-gray-700 border border-gray-200">
                      Frames {registrationState.framesCollected}/{registrationState.targetFrames}
                    </span>
                    <span
                      className={`px-2 py-1 rounded-full text-[11px] border ${registrationState.hasFace ? 'bg-green-50 text-green-700 border-green-200' : 'bg-red-50 text-red-700 border-red-200'}`}
                    >
                      {registrationState.hasFace ? 'Face detected' : 'No face'}
                    </span>
                  </div>
                )}

                <div>
                  <span className="text-sm font-medium text-gray-600">Status:</span>
                  <div
                    className={`text-sm mt-1 bg-transparent p-0 border-0 rounded-none shadow-none ${
                      error ? 'text-red-600' : registrationState.isComplete ? 'text-green-600' : 'text-gray-800'
                    }`}
                  >
                    {error || registrationState.message}
                  </div>
                </div>

                {/* Completion Results */}
                {/* {registrationState.isComplete && !error && (
                  <div className="mt-4 p-3 bg-green-50 border border-green-200 rounded">
                    <h4 className="font-semibold text-green-800 mb-2">Registration Results</h4>
                    <div className="text-sm text-green-700 space-y-1">
                      <div>✅ Face samples enrolled with the OpenNVR face model</div>
                      <div>🔍 Your face can now be recognized in the system</div>
                      <div>📱 You can now use the attendance system</div>
                    </div>
                  </div>
                )} */}

                {/* Duplicate Detection Warning moved to absolute overlay below */}
              </div>
            </div>

            {/* Instructions */}
            <div className="bg-white rounded-lg shadow-sm border p-4 flex-1 min-h-0">
              <h3 className="text-base font-semibold text-gray-900 mb-2">Instructions</h3>
              <ul className="text-sm text-gray-700 space-y-1">
                {CAPTURE_INSTRUCTIONS.map((instruction, index) => (
                  <li key={index}>• {instruction}</li>
                ))}
              </ul>
            </div>

            {/* Absolute overlay for duplicate detection to avoid resizing container */}
            {registrationState.message.includes('already registered') && (
              <div className="absolute top-2 left-2 right-2 z-20">
                <div className="p-3 bg-yellow-50 border border-yellow-200 rounded shadow-md">
                  <h4 className="font-semibold text-yellow-800 mb-1">⚠️ Duplicate Detected</h4>
                  <div className="text-sm text-yellow-700">
                    This face is already registered in the system. Registration was cancelled to prevent duplicates.
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
        </form>
        </div>

        {/* Hidden canvas for frame capture */}
        <canvas ref={canvasRef} style={{ display: 'none' }} />

        {/* Reusable modal dialog */}
        {dialog.open && (
          <div className="fixed inset-0 z-[100] flex items-end sm:items-center justify-center bg-black/40 p-0 sm:p-4">
            <div className="w-full sm:max-w-lg bg-white rounded-t-xl sm:rounded-xl shadow-lg border">
              <div className={`px-4 py-3 border-b rounded-t-xl ${
                dialog.variant === 'success' ? 'bg-green-50 border-green-200 text-green-800' :
                dialog.variant === 'error' ? 'bg-red-50 border-red-200 text-red-800' :
                dialog.variant === 'warning' ? 'bg-yellow-50 border-yellow-200 text-yellow-800' :
                'bg-gray-50 border-gray-200 text-gray-800'
              }`}>
                <div className="flex items-center justify-between">
                  <h3 className="font-semibold text-base">{dialog.title}</h3>
                  <button onClick={handleCloseDialog} className="text-gray-500 hover:text-gray-700">✖</button>
                </div>
              </div>
              <div className="p-4">
                {dialog.content}
              </div>
              <div className="px-4 py-3 border-t bg-gray-50 flex justify-end">
                <button
                  onClick={handleCloseDialog}
                  className="px-4 py-2 rounded-md bg-blue-600 text-white hover:bg-blue-700"
                >
                  OK
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </Layout>
  )
}
