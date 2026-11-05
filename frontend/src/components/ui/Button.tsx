import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { Spinner } from "./Spinner";

type Variant = "primary" | "secondary" | "ghost" | "danger" | "subtle";
type Size = "sm" | "md";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  /** Shows a spinner and blocks clicks; keeps the button width stable. */
  loading?: boolean;
  leftIcon?: ReactNode;
  fullWidth?: boolean;
}

const BASE =
  "inline-flex min-h-11 min-w-11 sm:min-h-0 sm:min-w-0 items-center justify-center gap-2 rounded-lg font-medium transition " +
  "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-offset-1 " +
  "disabled:cursor-not-allowed disabled:opacity-50";

const VARIANTS: Record<Variant, string> = {
  primary: "bg-action text-white hover:bg-action-hover focus-visible:ring-focus",
  secondary: "border border-slate-300 bg-white text-slate-700 hover:bg-slate-50 focus-visible:ring-focus",
  ghost: "text-slate-600 hover:bg-slate-100 focus-visible:ring-focus",
  danger: "bg-rose-600 text-white hover:bg-rose-700 focus-visible:ring-focus",
  subtle: "bg-brand-50 text-brand-700 hover:bg-brand-100 focus-visible:ring-focus",
};

const SIZES: Record<Size, string> = {
  sm: "px-2.5 py-1 text-xs",
  md: "px-4 py-2 text-sm",
};

/**
 * The single button primitive. Every variant is keyboard-focusable with a
 * visible focus ring, and `loading` sets aria-busy + disables interaction so a
 * double-submit is impossible.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "primary", size = "md", loading = false, leftIcon, fullWidth,
    disabled, className = "", children, type = "button", ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      className={`${BASE} ${VARIANTS[variant]} ${SIZES[size]} ${fullWidth ? "w-full" : ""} ${className}`}
      {...rest}
    >
      {loading ? <Spinner size={size === "sm" ? 13 : 15} /> : leftIcon}
      {children}
    </button>
  );
});
