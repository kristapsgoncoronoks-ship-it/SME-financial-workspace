import { Dropdown, type DropdownItem } from "./Dropdown";
import { cx } from "../../lib/cx";
import { Button } from "../ui/Button";

export interface SwitcherOption {
  id: string;
  name: string;
  /** Optional second line, e.g. a VAT number or plan. */
  sub?: string;
}

/** Optional live-read feedback. Fixture and legal-entity callers keep their
 * established picker behavior when they omit this state. */
export interface ScopeSwitcherReadState {
  phase: "loading" | "error" | "empty" | "mismatch" | "ready";
  currentName: string;
  message: string;
  onRetry?: () => void;
  isFetching: boolean;
  retryDisabled: boolean;
}

export interface ScopeSwitcherWriteState {
  phase: "idle" | "pending" | "failed" | "uncertain" | "checking";
  message: string;
  onCheck: () => void;
}

/**
 * A generic top-bar switcher used for BOTH the organization and the legal-entity
 * pickers — the two are structurally identical (choose one of N scopes) but
 * semantically distinct, so this is parameterised by `kind`. The current scope is
 * shown in the trigger; the menu lists the alternatives with the active one ticked.
 * When only one option exists it renders as static text (nothing to switch to).
 */
export function ScopeSwitcher({
  kind,
  options,
  currentId,
  onSwitch,
  readState,
  writeState,
}: {
  kind: "organization" | "legal entity";
  options: SwitcherOption[];
  currentId: string;
  onSwitch: (id: string) => void;
  readState?: ScopeSwitcherReadState;
  writeState?: ScopeSwitcherWriteState;
}) {
  const current = options.find((o) => o.id === currentId) ?? (readState ? undefined : options[0]);
  const currentName = readState?.currentName ?? current?.name;
  const eyebrow = kind === "organization" ? "Organization" : "Legal entity";

  if (writeState && writeState.phase !== "idle" && writeState.phase !== "failed") {
    const busy = writeState.phase === "pending" || writeState.phase === "checking";
    return <section aria-label="Organization switch" className="min-w-0 space-y-2 px-2">
      <div><span className="block text-xs font-medium uppercase tracking-wide text-text-secondary">{eyebrow}</span>
        <span dir="auto" className="block text-sm font-medium text-slate-700 [overflow-wrap:anywhere]">{currentName}</span></div>
      <p role={busy ? "status" : "alert"} className="text-sm text-text-secondary [overflow-wrap:anywhere]">{writeState.message}</p>
      {writeState.phase !== "pending" && <Button variant="secondary" className="max-w-full whitespace-normal" disabled={busy} onClick={writeState.onCheck}>Check current organization</Button>}
    </section>;
  }

  if (readState && readState.phase !== "ready") {
    return (
      <section aria-label="Organization choices" className="min-w-0 space-y-2 px-2">
        <div>
          <span className="block text-xs font-medium uppercase tracking-wide text-text-secondary">{eyebrow}</span>
          <span dir="auto" className="block text-sm font-medium text-slate-700 [overflow-wrap:anywhere]">{currentName}</span>
        </div>
        <p role={readState.phase === "loading" ? "status" : "alert"} className={cx("text-sm [overflow-wrap:anywhere]", readState.phase === "loading" ? "text-text-secondary" : "text-rose-700")}>
          {readState.message}
        </p>
        {readState.phase === "mismatch" ? (
          <Button variant="secondary" className="max-w-full whitespace-normal" onClick={() => location.reload()}>Reload workspace</Button>
        ) : readState.onRetry && (
          <Button
            variant="secondary"
            className="max-w-full whitespace-normal"
            aria-label="Retry organizations"
            aria-busy={readState.isFetching || undefined}
            disabled={readState.retryDisabled}
            onClick={readState.onRetry}
          >
            {readState.isFetching ? "Retrying…" : "Retry organizations"}
          </Button>
        )}
      </section>
    );
  }

  if (options.length <= 1) {
    return (
      <div className="px-2">
        <span className="block text-xs font-medium uppercase tracking-wide text-text-secondary">{eyebrow}</span>
        <span dir={readState ? "auto" : undefined} className={cx("block text-sm font-medium text-slate-700", readState && "[overflow-wrap:anywhere]")}>{currentName ?? "—"}</span>
      </div>
    );
  }

  const items: DropdownItem[] = options.map((o) => ({
    key: o.id,
    onSelect: () => onSwitch(o.id),
    label: (
      <span className="flex w-full items-center justify-between gap-3">
        <span className="min-w-0">
          <span className="block font-medium [overflow-wrap:anywhere]">{o.name}</span>
          {o.sub && <span className="block text-xs text-text-secondary [overflow-wrap:anywhere]">{o.sub}</span>}
        </span>
        {o.id === currentId && (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true" className="shrink-0 text-brand-600">
            <path d="M5 12l5 5 9-11" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        )}
      </span>
    ),
  }));

  return (
    <div className="min-w-0">
    <Dropdown
      label={`Switch ${kind}`}
      align="left"
      trigger={({ open }) => (
        <span className={cx("flex max-w-full items-center gap-2 rounded-lg px-2 py-1 hover:bg-slate-100")}>
          <span className="min-w-0 text-left">
            <span className="block text-xs font-medium uppercase tracking-wide text-text-secondary">{eyebrow}</span>
            <span dir={readState ? "auto" : undefined} className={cx("block max-w-40 text-sm font-medium text-slate-700", readState ? "[overflow-wrap:anywhere]" : "truncate")}>{currentName}</span>
          </span>
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
            className={`text-slate-400 transition-transform ${open ? "rotate-180" : ""}`}
          >
            <path d="M6 9l6 6 6-6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      )}
      items={items}
    />
    {writeState?.phase === "failed" && <p role="alert" className="mt-2 px-2 text-sm text-rose-700 [overflow-wrap:anywhere]">{writeState.message}</p>}
    </div>
  );
}
