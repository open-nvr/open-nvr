import { useEffect, useRef, useState } from 'react'
import jsQR from 'jsqr'

/**
 * Scan a QR code with the device camera and return its text — e.g. the
 * rtsp:// URL shown by the OpenNVR Cam phone app. Decodes locally with jsQR;
 * nothing leaves the browser. Camera access requires a secure context
 * (HTTPS or localhost) — degrades to a clear message otherwise.
 */
export function QrScanner({
  onResult,
  onClose,
  title = 'Scan a QR code',
}: {
  onResult: (text: string) => void
  onClose: () => void
  title?: string
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [error, setError] = useState<string | null>(null)
  // Keep the latest onResult without restarting the camera each render.
  const onResultRef = useRef(onResult)
  onResultRef.current = onResult

  useEffect(() => {
    let cancelled = false
    let raf = 0
    let stream: MediaStream | null = null
    const canvas = document.createElement('canvas')
    const ctx = canvas.getContext('2d', { willReadFrequently: true })

    const tick = () => {
      const v = videoRef.current
      if (v && ctx && v.readyState === v.HAVE_ENOUGH_DATA && v.videoWidth) {
        canvas.width = v.videoWidth
        canvas.height = v.videoHeight
        ctx.drawImage(v, 0, 0, canvas.width, canvas.height)
        const img = ctx.getImageData(0, 0, canvas.width, canvas.height)
        const code = jsQR(img.data, img.width, img.height)
        if (code?.data) {
          onResultRef.current(code.data.trim())
          return // parent closes the scanner
        }
      }
      raf = requestAnimationFrame(tick)
    }

    const start = async () => {
      if (!navigator.mediaDevices?.getUserMedia) {
        setError('Camera scanning needs HTTPS or localhost. Paste the URL instead.')
        return
      }
      try {
        stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: 'environment' },
          audio: false,
        })
        if (cancelled) {
          stream.getTracks().forEach((t) => t.stop())
          return
        }
        const v = videoRef.current
        if (v) {
          v.srcObject = stream
          await v.play()
          raf = requestAnimationFrame(tick)
        }
      } catch {
        setError('Could not access the camera. Check the browser permission.')
      }
    }

    start()
    return () => {
      cancelled = true
      cancelAnimationFrame(raf)
      stream?.getTracks().forEach((t) => t.stop())
    }
  }, [])

  return (
    <div
      className="fixed inset-0 bg-black/70 flex items-center justify-center z-[60]"
      onClick={onClose}
    >
      <div
        className="bg-[var(--panel)] border border-neutral-700 rounded-lg p-4 max-w-sm w-full mx-4"
        onClick={(e) => e.stopPropagation()}
      >
        <h4 className="text-sm font-medium mb-3">{title}</h4>
        {error ? (
          <p className="text-sm text-red-400">{error}</p>
        ) : (
          <div className="relative aspect-square bg-black rounded overflow-hidden">
            <video
              ref={videoRef}
              className="w-full h-full object-cover"
              muted
              playsInline
            />
            <div className="absolute inset-8 border-2 border-[var(--accent)] rounded pointer-events-none" />
          </div>
        )}
        <div className="flex justify-end mt-3">
          <button
            type="button"
            className="px-3 py-1.5 border border-neutral-700 bg-[var(--panel-2)] rounded text-sm"
            onClick={onClose}
          >
            Close
          </button>
        </div>
      </div>
    </div>
  )
}
