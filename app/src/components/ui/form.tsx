// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * Form building blocks. Every form in the app composes these, so a label,
 * a hint, a validation error and a required marker look and behave the same
 * on every screen.
 *
 *     <Field label="Username" required hint="Letters and digits" error={err}>
 *       {(id, describedBy) => <Input id={id} aria-describedby={describedBy} ... />}
 *     </Field>
 *
 * or, for the common case, let Field wire the id itself:
 *
 *     <Field label="Username" required><Input value={..} onChange={..} /></Field>
 *
 * A single-element child gets `id`, `aria-invalid` and `aria-describedby`
 * injected; the label's `htmlFor` points at it, so clicking the label focuses
 * the control and a screen reader announces label, hint and error with it.
 *
 * Controls read the theme tokens only, and forward every native attribute --
 * `placeholder`, `name`, `title`, `aria-label` -- untouched, because tests and
 * browser autofill key off them.
 */

import { clsx } from 'clsx'
import {
  Children,
  cloneElement,
  isValidElement,
  useId,
  type InputHTMLAttributes,
  type ReactElement,
  type ReactNode,
  type Ref,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'

const CONTROL =
  'w-full bg-[var(--panel)] text-[var(--text)] border border-[var(--border)] rounded-[var(--radius-input)] ' +
  'px-2.5 py-1.5 text-sm placeholder:text-[color-mix(in_oklab,var(--text-dim)_55%,transparent)] ' +
  'focus:outline-none focus:border-[var(--accent)] focus:ring-1 focus:ring-[var(--accent)] ' +
  'disabled:opacity-50 disabled:cursor-not-allowed ' +
  'aria-[invalid=true]:border-[var(--danger)] aria-[invalid=true]:focus:ring-[var(--danger)]'

export type InputProps = InputHTMLAttributes<HTMLInputElement> & { ref?: Ref<HTMLInputElement> }
export function Input({ className, ...rest }: InputProps) {
  return <input className={clsx(CONTROL, className)} {...rest} />
}

export type SelectProps = SelectHTMLAttributes<HTMLSelectElement> & { ref?: Ref<HTMLSelectElement> }
export function Select({ className, children, ...rest }: SelectProps) {
  return (
    <select className={clsx(CONTROL, className)} {...rest}>
      {children}
    </select>
  )
}

export type TextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement> & { ref?: Ref<HTMLTextAreaElement> }
export function Textarea({ className, ...rest }: TextareaProps) {
  return <textarea className={clsx(CONTROL, 'min-h-[4.5rem]', className)} {...rest} />
}

/** A checkbox with its label to the right; the whole row is clickable. */
export function Checkbox({
  label,
  hint,
  className,
  ...rest
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> & { label: ReactNode; hint?: ReactNode; ref?: Ref<HTMLInputElement> }) {
  return (
    <label className={clsx('inline-flex items-start gap-2 text-sm text-[var(--text)] cursor-pointer select-none', rest.disabled && 'opacity-50 cursor-not-allowed', className)}>
      <input type="checkbox" className="mt-0.5 accent-[var(--accent)]" {...rest} />
      <span>
        {label}
        {hint && <span className="block text-xs text-[var(--text-dim)]">{hint}</span>}
      </span>
    </label>
  )
}

type FieldChild = ReactNode | ((id: string, describedBy: string | undefined, invalid: boolean) => ReactNode)

export function Field({
  label,
  hint,
  error,
  required,
  htmlFor,
  className,
  children,
  inline,
}: {
  label: ReactNode
  hint?: ReactNode
  /** Shown in the danger colour under the control; also marks it invalid. */
  error?: ReactNode
  required?: boolean
  /** Only needed when the control's id is set elsewhere. */
  htmlFor?: string
  className?: string
  children: FieldChild
  /** Label beside the control instead of above it (short numeric settings). */
  inline?: boolean
}) {
  const autoId = useId()
  const hintId = `${autoId}-hint`
  const errorId = `${autoId}-error`
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(' ') || undefined
  const invalid = Boolean(error)

  let controlId = htmlFor ?? autoId
  let control: ReactNode
  if (typeof children === 'function') {
    control = children(controlId, describedBy, invalid)
  } else {
    const only = Children.count(children) === 1 && isValidElement(children) ? (children as ReactElement<Record<string, unknown>>) : null
    if (only) {
      controlId = (only.props.id as string | undefined) ?? htmlFor ?? autoId
      control = cloneElement(only, {
        id: controlId,
        'aria-invalid': invalid || undefined,
        'aria-describedby': [only.props['aria-describedby'], describedBy].filter(Boolean).join(' ') || undefined,
        required: (only.props.required as boolean | undefined) ?? required,
      })
    } else {
      control = children
    }
  }

  return (
    <div className={clsx(inline ? 'flex items-center justify-between gap-3' : 'flex flex-col gap-1', className)}>
      <label htmlFor={controlId} className="text-xs font-medium text-[var(--text-dim)]">
        {label}
        {required && <span className="ml-0.5 text-[var(--danger)]" aria-hidden="true">*</span>}
      </label>
      <div className={clsx(inline ? 'shrink-0' : 'min-w-0')}>
        {control}
        {hint && !error && <div id={hintId} className="mt-1 text-[11px] text-[var(--text-dim)]">{hint}</div>}
        {error && <div id={errorId} role="alert" className="mt-1 text-[11px] text-[var(--danger)]">{error}</div>}
      </div>
    </div>
  )
}

/** A titled group of fields inside a form, with an optional description. */
export function FormSection({ title, description, children, className }: {
  title: ReactNode
  description?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <fieldset className={clsx('space-y-3', className)}>
      <legend className="mb-2">
        <span className="text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--text)]">{title}</span>
        {description && <span className="block text-xs text-[var(--text-dim)] mt-0.5">{description}</span>}
      </legend>
      {children}
    </fieldset>
  )
}
