import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { cx } from "../../lib/cx";

export interface DropdownItem {
  key: string;
  label: ReactNode;
  onSelect?: () => void;
  /** Render as a link instead of a button (still keyboard-activatable). */
  href?: string;
  danger?: boolean;
  disabled?: boolean;
}

export interface DropdownProps {
  /** The trigger. Receives open state so it can rotate a chevron, etc. */
  trigger: (props: { open: boolean }) => ReactNode;
  items: DropdownItem[];
  /** Accessible name for the menu. */
  label: string;
  align?: "left" | "right";
  className?: string;
}

/**
 * Accessible menu-button dropdown (WAI-ARIA menu pattern). The trigger exposes
 * `aria-haspopup="menu"` + `aria-expanded`; the panel is `role="menu"` with
 * `role="menuitem"` children. Keyboard: ArrowDown/Up move between items, Enter/Space
 * activate, Escape closes and returns focus to the trigger, and a click outside
 * dismisses. Used by the user menu and the org/legal-entity switchers so all three
 * behave identically.
 */
export function Dropdown({ trigger, items, label, align = "right", className = "" }: DropdownProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuStyle, setMenuStyle] = useState<CSSProperties>({});
  const itemRefs = useRef<(HTMLElement | null)[]>([]);

  useLayoutEffect(() => {
    if (!open) return;
    const root = rootRef.current, menu = menuRef.current;
    if (!root || !menu) return;
    function position() {
      if (!root || !menu) return;
      const viewport = window.visualViewport;
      let left = viewport?.offsetLeft ?? 0, top = viewport?.offsetTop ?? 0;
      let right = left + (viewport?.width ?? innerWidth), bottom = top + (viewport?.height ?? innerHeight);
      // A drawer's scrolling body also clips its descendants. Keep the menu
      // inside that visible area as well as the browser viewport.
      for (let parent = root.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
        const style = getComputedStyle(parent), rect = parent.getBoundingClientRect();
        if (/auto|scroll|hidden|clip/.test(style.overflowX)) { left = Math.max(left, rect.left + parent.clientLeft); right = Math.min(right, rect.left + parent.clientLeft + parent.clientWidth); }
        if (/auto|scroll|hidden|clip/.test(style.overflowY)) { top = Math.max(top, rect.top + parent.clientTop); bottom = Math.min(bottom, rect.top + parent.clientTop + parent.clientHeight); }
      }
      const anchor = root.getBoundingClientRect(), gap = 4, inset = 8;
      const availableWidth = Math.max(0, right - left - inset * 2);
      const rem = parseFloat(getComputedStyle(document.documentElement).fontSize);
      const maxWidth = Math.min(24 * rem, availableWidth), minWidth = Math.min(12 * rem, maxWidth);
      const width = Math.min(Math.max(menu.offsetWidth, minWidth), maxWidth);
      const x = Math.max(left + inset, Math.min(align === "right" ? anchor.right - width : anchor.left, right - inset - width));
      const below = Math.max(0, bottom - inset - anchor.bottom - gap), above = Math.max(0, anchor.top - top - inset - gap);
      const upwards = menu.scrollHeight > below && above > below;
      const maxHeight = upwards ? above : below;
      const next: CSSProperties = { left: x - anchor.left, top: upwards ? -Math.min(menu.scrollHeight + 2, maxHeight) - gap : anchor.height + gap, minWidth, maxWidth, maxHeight, width: "max-content" };
      setMenuStyle(previous => JSON.stringify(previous) === JSON.stringify(next) ? previous : next);
    }
    position();
    const observer = new ResizeObserver(position);
    observer.observe(root); observer.observe(menu);
    window.addEventListener("resize", position);
    document.addEventListener("scroll", position, true);
    window.visualViewport?.addEventListener("resize", position);
    return () => {
      observer.disconnect();
      window.removeEventListener("resize", position);
      document.removeEventListener("scroll", position, true);
      window.visualViewport?.removeEventListener("resize", position);
    };
  }, [open, align]);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    function onDocKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        setOpen(false);
        triggerRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onDocKey, true);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onDocKey, true);
    };
  }, [open]);

  // Focus the first ENABLED item when the menu opens (the first item may be a
  // non-interactive header, e.g. the account email in the user menu).
  useEffect(() => {
    if (!open) return;
    const firstEnabled = items.findIndex((it) => !it.disabled);
    itemRefs.current[firstEnabled >= 0 ? firstEnabled : 0]?.focus();
  }, [open, items]);

  function close(focusTrigger = true) {
    setOpen(false);
    if (focusTrigger) triggerRef.current?.focus();
  }

  function move(from: number, delta: number) {
    const n = items.length;
    for (let i = 1; i <= n; i++) {
      const idx = (from + delta * i + n) % n;
      if (!items[idx].disabled) {
        itemRefs.current[idx]?.focus();
        return;
      }
    }
  }

  function onItemKeyDown(e: React.KeyboardEvent, i: number) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      move(i, 1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      move(i, -1);
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Home") {
      e.preventDefault();
      move(-1, 1);
    } else if (e.key === "End") {
      e.preventDefault();
      move(0, -1);
    }
  }

  return (
    <div ref={rootRef} className={cx("relative", className)}>
      <button
        ref={triggerRef}
        type="button"
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={(e) => {
          if ((e.key === "ArrowDown" || e.key === "Enter" || e.key === " ") && !open) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className="max-w-full rounded-lg focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
      >
        {trigger({ open })}
      </button>

      {open && (
        <div
          ref={menuRef}
          role="menu"
          aria-label={label}
          style={menuStyle}
          className={cx(
            "iq-pop-in absolute z-40 overflow-x-hidden overflow-y-auto overscroll-contain rounded-xl border border-slate-200 bg-white py-1 shadow-lg",
          )}
        >
          {items.map((item, i) => {
            const cls = cx(
              "flex min-w-0 w-full items-center gap-2 whitespace-normal px-3 py-2 text-left text-sm [overflow-wrap:anywhere] focus-visible:outline-hidden",
              item.disabled
                ? "cursor-not-allowed text-slate-300"
                : item.danger
                  ? "text-rose-600 hover:bg-rose-50 focus:bg-rose-50"
                  : "text-slate-700 hover:bg-slate-50 focus:bg-slate-50",
            );
            const common = {
              role: "menuitem" as const,
              tabIndex: -1,
              ref: (el: HTMLElement | null) => {
                itemRefs.current[i] = el;
              },
              onKeyDown: (e: React.KeyboardEvent) => onItemKeyDown(e, i),
              className: cls,
            };
            if (item.href && !item.disabled) {
              return (
                <a key={item.key} href={item.href} onClick={() => close(false)} {...common}>
                  {item.label}
                </a>
              );
            }
            return (
              <button
                key={item.key}
                type="button"
                disabled={item.disabled}
                onClick={() => {
                  item.onSelect?.();
                  close();
                }}
                {...common}
              >
                {item.label}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
