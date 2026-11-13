const DURATION_UNITS_NS: Record<string, bigint> = {
  ns: 1n, us: 1_000n, '\u00b5s': 1_000n, ms: 1_000_000n,
  s: 1_000_000_000n, m: 60_000_000_000n, h: 3_600_000_000_000n,
}
const MAX_DURATION_NS = (1n << 63n) - 1n
const MIN_DURATION_NS = -(1n << 63n)
// Bound BigInt and Python integer conversion consistently. Normal fractions
// still truncate to nanoseconds using MediaMTX's duration semantics.
const MAX_DURATION_FRACTION_DIGITS = 1_000

export function durationFieldError(field: string, value: unknown): string | undefined {
  if (value === null || value === undefined || value === '') return undefined
  if (typeof value !== 'string') return `Invalid ${field}: must be a duration string.`

  const day = /^(-?[0-9]+)d/.exec(value)
  let days = 0n
  let negativeDays = false
  let rest = value
  if (day) {
    days = BigInt(day[1])
    negativeDays = days < 0n
    if (negativeDays) days = -days
    rest = value.slice(day[0].length)
  }

  let sign = 1n
  let hadSign = false
  if (rest.startsWith('+') || rest.startsWith('-')) {
    hadSign = true
    if (rest[0] === '-') sign = -1n
    rest = rest.slice(1)
  }

  if (hadSign && rest === '') return `Invalid ${field}: use a MediaMTX duration such as 10s or 1h0m0s.`
  let durationNs = 0n
  if (rest !== '' && rest !== '0') {
    const part = /([0-9]+(?:\.[0-9]*)?|\.[0-9]+)(ns|us|\u00b5s|ms|s|m|h)/gy
    let position = 0
    let match: RegExpExecArray | null
    while ((match = part.exec(rest)) !== null) {
      const [whole = '0', fraction = ''] = match[1].split('.')
      if (fraction.length > MAX_DURATION_FRACTION_DIGITS) {
        return `Invalid ${field}: fraction exceeds ${MAX_DURATION_FRACTION_DIGITS} digits.`
      }
      const unit = DURATION_UNITS_NS[match[2]]
      const scale = 10n ** BigInt(fraction.length)
      durationNs += BigInt(whole || '0') * unit + (fraction ? BigInt(fraction) * unit / scale : 0n)
      if (durationNs > MAX_DURATION_NS + (sign < 0n ? 1n : 0n)) return `Invalid ${field}: duration exceeds the supported range.`
      position = part.lastIndex
    }
    if (position !== rest.length) return `Invalid ${field}: use a MediaMTX duration such as 10s or 1h0m0s.`
    durationNs *= sign
  }

  if (days * 86_400_000_000_000n > MAX_DURATION_NS) return `Invalid ${field}: duration exceeds the supported range.`
  durationNs = negativeDays ? -(days * 86_400_000_000_000n + durationNs) : durationNs + days * 86_400_000_000_000n
  if (durationNs > MAX_DURATION_NS || durationNs < MIN_DURATION_NS) return `Invalid ${field}: duration exceeds the supported range.`
  if (durationNs > 86_400_000_000_000n && field === 'recordSegmentDuration') {
    return 'Invalid recordSegmentDuration: maximum segment duration is 1 day.'
  }
  return undefined
}
