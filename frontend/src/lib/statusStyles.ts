/** Presentation roles only: each domain keeps its own state keys and labels. */
export const STATE_CLASSES = {
  neutral: "bg-state-neutral-bg text-state-neutral-text",
  muted: "bg-state-muted-bg text-state-muted-text",
  brand: "bg-state-brand-bg text-state-brand-text",
  success: "bg-state-success-bg text-state-success-text",
  warning: "bg-state-warning-bg text-state-warning-text",
  warningStrong: "bg-state-warning-bg text-state-warning-strong-text",
  danger: "bg-state-danger-bg text-state-danger-text",
  info: "bg-state-info-bg text-state-info-text",
  infoSubtle: "bg-state-info-subtle-bg text-state-info-text",
  approved: "bg-state-approved-bg text-state-approved-text",
  credit: "bg-state-credit-bg text-state-credit-text",
  returned: "bg-state-returned-bg text-state-returned-text",
} as const;

export const STATE_DOT_CLASSES = {
  neutral: "bg-state-neutral-text", brand: "bg-state-brand-text",
  success: "bg-state-success-text", warning: "bg-state-warning-text",
  danger: "bg-state-danger-text", info: "bg-state-info-text",
} as const;
