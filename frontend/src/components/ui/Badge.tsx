import { STATE_CLASSES, STATE_DOT_CLASSES } from "../../lib/statusStyles";
import type { ReactNode } from "react";

export type Tone = "neutral" | "brand" | "success" | "warning" | "danger" | "info";

const TONES: Record<Tone, string> = STATE_CLASSES;

export function Badge({ tone = "neutral", children, className = "" }:
  { tone?: Tone; children: ReactNode; className?: string }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${TONES[tone]} ${className}`}>
      {children}
    </span>
  );
}

// Small optional colored dot for status rows (paired with a label).
export function Dot({ tone = "neutral" }: { tone?: Tone }) {
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${STATE_DOT_CLASSES[tone]}`} aria-hidden="true" />;
}
