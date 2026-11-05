import { useEffect, useId, type ReactNode } from "react";
import { Portal } from "./Portal";
import { useFocusTrap } from "./useFocusTrap";
import { cx } from "../../lib/cx";

export type DrawerSide = "right" | "left";
export type DrawerSize = "sm" | "md" | "lg";

const WIDTH: Record<DrawerSize, string> = {
  sm: "max-w-sm",
  md: "max-w-md",
  lg: "max-w-xl",
};

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  description?: ReactNode;
  children?: ReactNode;
  footer?: ReactNode;
  side?: DrawerSide;
  size?: DrawerSize;
}

/**
 * Side panel / drawer. Shares the modal a11y contract (focus trap, Escape,
 * scroll-lock, focus restore, labelled dialog) but slides in from an edge and is
 * full-height — the right surface for record detail, contextual editing, or
 * filters on a wide screen. On mobile it fills the width.
 */
export function Drawer({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  side = "right",
  size = "md",
}: DrawerProps) {
  const titleId = useId();
  const descId = useId();
  const trapRef = useFocusTrap<HTMLDivElement>(open, onClose);

  useEffect(() => {
    if (!open) return;
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = prev;
    };
  }, [open]);

  if (!open) return null;

  const anim = side === "right" ? "iq-slide-right" : "iq-slide-left";

  return (
    <Portal>
      <div className="fixed inset-0 z-50">
        <div
          className="iq-fade-in absolute inset-0 bg-slate-900/40"
          aria-hidden="true"
          onClick={onClose}
        />
        <div
          ref={trapRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby={titleId}
          aria-describedby={description ? descId : undefined}
          tabIndex={-1}
          className={cx(
            "absolute inset-y-0 flex w-full flex-col bg-white shadow-xl outline-hidden",
            WIDTH[size],
            side === "right" ? "right-0 border-l" : "left-0 border-r",
            "border-slate-200",
            anim,
          )}
        >
          <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-4">
            <div>
              <h2 id={titleId} className="text-base font-semibold text-slate-800">
                {title}
              </h2>
              {description && (
                <p id={descId} className="mt-1 text-sm text-slate-500">
                  {description}
                </p>
              )}
            </div>
            <button
              type="button"
              onClick={onClose}
              aria-label="Close panel"
              className="-mr-1 inline-flex min-h-11 min-w-11 sm:min-h-0 sm:min-w-0 items-center justify-center rounded-lg p-1 text-text-secondary hover:bg-slate-100 hover:text-slate-600 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>
          </header>

          <div className="flex-1 overflow-y-auto px-5 py-4 text-sm text-slate-600">{children}</div>

          {footer && (
            <footer className="flex items-center justify-end gap-2 border-t border-slate-100 px-5 py-3">
              {footer}
            </footer>
          )}
        </div>
      </div>
    </Portal>
  );
}
