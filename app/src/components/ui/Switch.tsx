import { clsx } from 'clsx'

/**
 * An on/off setting.
 *
 * A switch, not a "Turn on" / "Turn off" button: a button labelled with
 * the action it performs reads as a call to action, and the loudest thing
 * on the vehicle register was a blue "Turn on" for a feature that was
 * simply off. A switch reads as a STATE.
 *
 * The knob changes colour as well as position, so on and off differ in
 * more than one channel — a knob offset alone is easy to misread.
 */
export function Switch({
  checked, onChange, label, disabled = false,
}: {
  checked: boolean
  onChange: (next: boolean) => void
  /** Accessible name: the setting this switch controls. */
  label: string
  disabled?: boolean
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={clsx(
        'relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors',
        'focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]',
        'disabled:opacity-50',
        checked
          ? 'border-transparent bg-[var(--accent)]'
          : 'border-[var(--border)] bg-[var(--bg-2)]',
      )}
    >
      <span
        aria-hidden
        className={clsx(
          'inline-block h-3.5 w-3.5 rounded-full shadow transition-transform',
          checked ? 'translate-x-[18px] bg-white' : 'translate-x-[2px] bg-[var(--text-dim)]',
        )}
      />
    </button>
  )
}
