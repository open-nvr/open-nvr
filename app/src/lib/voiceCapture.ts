// Push-to-talk capture for voice search: the microphone, downsampled to
// 16 kHz mono and packed as a PCM WAV in the browser, so the box's own
// speech-to-text adapter gets the one format every STT image decodes
// without ffmpeg, and nothing leaves the box through anyone's cloud
// speech API.
//
// getUserMedia needs a secure context: localhost or the nginx HTTPS
// front. On a plain http LAN address `available()` is false and the
// button says why instead of failing on click.

export const TARGET_RATE = 16000

export function voiceCaptureAvailable(): boolean {
  return typeof navigator !== 'undefined'
    && !!navigator.mediaDevices
    && typeof navigator.mediaDevices.getUserMedia === 'function'
    && typeof window !== 'undefined'
    && (window.isSecureContext ?? false)
}

export class VoiceCapture {
  private ctx: AudioContext | null = null
  private stream: MediaStream | null = null
  private node: ScriptProcessorNode | null = null
  private chunks: Float32Array[] = []
  private sampleRate = 48000

  async start(): Promise<void> {
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
    })
    this.ctx = new AudioContext()
    this.sampleRate = this.ctx.sampleRate
    const source = this.ctx.createMediaStreamSource(this.stream)
    // ScriptProcessorNode is deprecated but universal; an AudioWorklet
    // needs its own served file. 4096 frames a callback is ~85 ms.
    this.node = this.ctx.createScriptProcessor(4096, 1, 1)
    this.chunks = []
    this.node.onaudioprocess = (e) => {
      this.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)))
    }
    source.connect(this.node)
    this.node.connect(this.ctx.destination)
  }

  /** Stops the microphone and answers the clip as a 16 kHz mono WAV. */
  async stop(): Promise<{ wav: Blob; seconds: number }> {
    this.node?.disconnect()
    this.stream?.getTracks().forEach((t) => t.stop())
    const ctx = this.ctx
    this.node = null
    this.stream = null
    this.ctx = null
    if (ctx) await ctx.close().catch(() => undefined)
    const total = this.chunks.reduce((n, c) => n + c.length, 0)
    const pcm = new Float32Array(total)
    let off = 0
    for (const c of this.chunks) { pcm.set(c, off); off += c.length }
    this.chunks = []
    const down = downsample(pcm, this.sampleRate, TARGET_RATE)
    return { wav: toWav(down, TARGET_RATE), seconds: down.length / TARGET_RATE }
  }
}

export function downsample(input: Float32Array, from: number, to: number): Float32Array {
  if (from === to) return input
  const ratio = from / to
  const length = Math.floor(input.length / ratio)
  const out = new Float32Array(length)
  for (let i = 0; i < length; i++) {
    // mean over the source window: a cheap anti-alias that is enough for speech
    const start = Math.floor(i * ratio)
    const end = Math.min(input.length, Math.floor((i + 1) * ratio))
    let sum = 0
    for (let j = start; j < end; j++) sum += input[j]
    out[i] = end > start ? sum / (end - start) : 0
  }
  return out
}

export function toWav(samples: Float32Array, rate: number): Blob {
  const buf = new ArrayBuffer(44 + samples.length * 2)
  const v = new DataView(buf)
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)) }
  str(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); str(8, 'WAVE')
  str(12, 'fmt '); v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true)
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true)
  str(36, 'data'); v.setUint32(40, samples.length * 2, true)
  let o = 44
  for (let i = 0; i < samples.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, samples[i]))
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true)
  }
  return new Blob([buf], { type: 'audio/wav' })
}

export async function blobToBase64(blob: Blob): Promise<string> {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  let bin = ''
  for (let i = 0; i < bytes.length; i += 0x8000) {
    bin += String.fromCharCode.apply(null, Array.from(bytes.subarray(i, i + 0x8000)))
  }
  return btoa(bin)
}
