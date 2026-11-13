import { Badge, type BadgeVariant } from './ui'

/** What an app or an adapter needs and whether this box meets it — derived
 *  on the server from the tasks involved (`services/hardware_profile`), never
 *  declared by the app. Advisory: a badge beside "needs <task>", not a
 *  disabled button. */
export type HardwareBar = {
  cores: number | null
  ram_gb: number | null
  gpu: 'none' | 'optional' | 'recommended' | 'required'
  tasks: string[]
  verdict: 'ok' | 'tight' | 'short'
  reasons: string[]
}

const VARIANT: Record<HardwareBar['verdict'], BadgeVariant> = { ok: 'success', tight: 'warning', short: 'destructive' }
const VERDICT_TEXT: Record<HardwareBar['verdict'], string> = {
  ok: 'fits this box',
  tight: 'fits this box, tightly',
  short: 'this box is short',
}
const GPU_TEXT: Record<HardwareBar['gpu'], string | null> = {
  none: null, optional: 'GPU optional', recommended: 'GPU recommended', required: 'GPU required',
}

export function describeBar(bar: HardwareBar): string {
  const parts: string[] = []
  if (bar.cores) parts.push(`${bar.cores} cores`)
  if (bar.ram_gb) parts.push(`${bar.ram_gb} GB`)
  const gpu = GPU_TEXT[bar.gpu]
  if (gpu) parts.push(gpu)
  return parts.join(' · ')
}

export function HardwareBadge({ hardware }: { hardware?: HardwareBar | null }) {
  if (!hardware) return null
  const bar = describeBar(hardware)
  if (!bar) return null
  const title = [
    `Needs ${bar} — ${VERDICT_TEXT[hardware.verdict]}.`,
    ...hardware.reasons,
    'From the tasks it uses (tasks.yml); advisory, nothing is blocked.',
  ].join(' ')
  return (
    <Badge variant={VARIANT[hardware.verdict]} title={title}>
      {bar} — {VERDICT_TEXT[hardware.verdict]}
    </Badge>
  )
}
