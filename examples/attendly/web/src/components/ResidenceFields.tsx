// The enrolment forms' residence selector: day scholar, or hostler with a
// block. The hostel gates admit hostlers of their block only.
export type Residence = 'day_scholar' | 'hostler'

export default function ResidenceFields({ residence, hostelBlock, onResidence, onHostelBlock, disabled, compact }: {
  residence: Residence
  hostelBlock: string
  onResidence: (r: Residence) => void
  onHostelBlock: (b: string) => void
  disabled?: boolean
  compact?: boolean
}) {
  const input = compact
    ? 'w-full rounded-md border border-gray-300 px-3 py-2 text-sm focus:border-blue-500 focus:ring-1 focus:ring-blue-500'
    : 'w-full rounded-lg border border-gray-300 px-3 py-2 focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors'
  const label = compact ? 'block text-sm font-medium text-gray-700 mb-1' : 'block text-sm font-medium text-gray-700 mb-2'
  return (
    <>
      <div className={compact ? '' : 'flex-1 min-w-[150px]'}>
        <label className={label}>Residence</label>
        <select className={input} value={residence} onChange={e => onResidence(e.target.value as Residence)} disabled={disabled} name="residence">
          <option value="day_scholar">Day scholar</option>
          <option value="hostler">Hostler</option>
        </select>
      </div>
      {residence === 'hostler' && (
        <div className={compact ? '' : 'flex-1 min-w-[150px]'}>
          <label className={label}>Hostel block</label>
          <input className={input} placeholder="e.g., Block B" value={hostelBlock} onChange={e => onHostelBlock(e.target.value)} disabled={disabled} required name="hostelBlock" />
        </div>
      )}
    </>
  )
}
