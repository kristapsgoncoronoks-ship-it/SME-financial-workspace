import { FormField, inputClass } from "./Form";
import type { ReactNode } from "react";

export interface DateInputProps {
  label: ReactNode;
  /** ISO date string, "YYYY-MM-DD" (or ""). */
  value: string;
  onValueChange: (value: string) => void;
  hint?: ReactNode;
  error?: ReactNode;
  required?: boolean;
  /** Native required validation only for an already-required field; opt-in
   * so legacy callers keep their existing aria-only validation behavior. */
  nativeValidation?: boolean;
  disabled?: boolean;
  /** ISO min/max bounds, e.g. a filing-deadline cap. */
  min?: string;
  max?: string;
  name?: string;
  className?: string;
}

/**
 * Date input built on the native `<input type="date">` — deliberately, because it
 * gives every platform its own accessible, localised date picker (and a real date
 * keyboard on mobile) for free, which no hand-rolled calendar matches for a11y.
 * The value is always an ISO `YYYY-MM-DD` string, so it round-trips to the API
 * unambiguously regardless of the user's locale display.
 */
export function DateInput({
  label, value, onValueChange, hint, error, required, nativeValidation = false, disabled, min, max, name, className,
}: DateInputProps) {
  return (
    <FormField label={label} hint={hint} error={error} required={required} className={className}>
      {(f) => (
        <input
          {...f}
          name={name}
          type="date"
          required={nativeValidation && required}
          value={value}
          min={min}
          max={max}
          disabled={disabled}
          onChange={(e) => onValueChange(e.target.value)}
          className={inputClass(!!error, "tabular-nums")}
        />
      )}
    </FormField>
  );
}
