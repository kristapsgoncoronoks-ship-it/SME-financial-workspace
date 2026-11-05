import { useId, type ReactNode, type InputHTMLAttributes, type SelectHTMLAttributes, type TextareaHTMLAttributes } from "react";
import { cx } from "../../lib/cx";

/** Props the field passes down to whatever control it wraps, for correct wiring. */
export interface FieldControlProps {
  id: string;
  "aria-describedby"?: string;
  "aria-invalid"?: true;
  "aria-required"?: true;
}

export interface FormFieldProps {
  label: ReactNode;
  /** Helper text under the control (guidance, format hints). */
  hint?: ReactNode;
  /** Validation message. When present the control is marked invalid + described by it. */
  error?: ReactNode;
  required?: boolean;
  /** Render the control, spreading the passed props onto it for a11y wiring. */
  children: (control: FieldControlProps) => ReactNode;
  className?: string;
}

const INPUT_BASE =
  "w-full rounded-lg border px-3 py-2 text-sm outline-hidden transition " +
  "focus:ring-2 placeholder:text-text-secondary disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400";

/** Invalid fields keep the error border while sharing the keyboard-focus role. */
export function inputClass(invalid?: boolean, extra = ""): string {
  return cx(
    INPUT_BASE,
    invalid
      ? "border-rose-400 focus:border-rose-500 focus:ring-focus"
      : "border-slate-300 focus:border-focus focus:ring-focus",
    extra,
  );
}

/**
 * The labelled-field wrapper — the single source of truth for form accessibility.
 * It generates the control id, links the label, and wires `aria-describedby` to
 * the hint and error, `aria-invalid` + `aria-required` as needed. Validation
 * messages announce via `role="alert"`, so a screen reader hears them on change.
 *
 * Use it directly for a custom control, or reach for the `TextInput` / `Select` /
 * `Textarea` / `CurrencyInput` / `DateInput` / `TaxRateInput` convenience wrappers
 * that compose it.
 */
export function FormField({ label, hint, error, required, children, className = "" }: FormFieldProps) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errId = `${id}-err`;
  const describedBy = cx(hint ? hintId : "", error ? errId : "").trim() || undefined;

  return (
    <div className={cx("flex flex-col gap-1.5", className)}>
      <label htmlFor={id} className="text-sm font-medium text-slate-700">
        {label}
        {required && (
          <span className="ml-0.5 text-rose-500" aria-hidden="true">
            *
          </span>
        )}
      </label>
      {children({
        id,
        "aria-describedby": describedBy,
        "aria-invalid": error ? true : undefined,
        "aria-required": required ? true : undefined,
      })}
      {hint && !error && (
        <p id={hintId} className="text-xs text-text-secondary">
          {hint}
        </p>
      )}
      {error && (
        <p id={errId} role="alert" className="flex items-center gap-1 text-xs font-medium text-rose-600">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" />
            <path d="M12 7v6M12 16.5v.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          {error}
        </p>
      )}
    </div>
  );
}

type BaseFieldProps = Pick<FormFieldProps, "label" | "hint" | "error" | "required" | "className">;

/** Single-line text input with the full field wiring. */
export function TextInput({
  label, hint, error, required, className, nativeValidation = false,
  ...input
}: BaseFieldProps & {
  /** Opt in to native required validation for an already-required field.
   * Legacy callers retain their existing aria-only required behavior. */
  nativeValidation?: boolean;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "id">) {
  return (
    <FormField label={label} hint={hint} error={error} required={required} className={className}>
      {(f) => <input {...f} {...input} required={nativeValidation && required} className={inputClass(!!error)} />}
    </FormField>
  );
}

/** Labelled select. */
export function Select({
  label, hint, error, required, className, children,
  ...select
}: BaseFieldProps & Omit<SelectHTMLAttributes<HTMLSelectElement>, "id">) {
  return (
    <FormField label={label} hint={hint} error={error} required={required} className={className}>
      {(f) => (
        <select {...f} {...select} className={inputClass(!!error, "bg-white")}>
          {children}
        </select>
      )}
    </FormField>
  );
}

/** Labelled multi-line textarea. */
export function Textarea({
  label, hint, error, required, className,
  ...ta
}: BaseFieldProps & Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "id">) {
  return (
    <FormField label={label} hint={hint} error={error} required={required} className={className}>
      {(f) => <textarea {...f} {...ta} className={inputClass(!!error, "min-h-20")} />}
    </FormField>
  );
}
