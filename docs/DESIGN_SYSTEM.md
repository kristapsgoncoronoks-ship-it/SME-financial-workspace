# InvoiceIQ design system & application shell

The reusable UI foundation for the finance workspace: an accessible component kit,
a responsive application shell, a living component gallery, and visual + end-to-end
tests. Built for non-technical users doing daily financial work on desktop, and for
expense submission / approvals on mobile.

The reusable primitives (`components/ui/`) and the application shell
(`components/shell/`) are shared: `Layout.tsx` mounts the same `AppShell` the
`/design` showcase uses, grouped into the live IA (`src/lib/nav.ts`) instead of the
fixture's nine-item demo nav (board I1.2). The showcase itself stays additive — its
routes, its fixtures (`src/design/fixtures.ts`) and every live *page*'s content are
untouched by this — `/design` remains a fixtures-only surface with **clearly-marked
development data**; no screen is filled with fake analytics.

---

## 1. Component architecture

```
frontend/src/
  components/
    ui/                     ← the reusable primitive kit (one barrel: index.ts)
      Button, Badge, StatusBadge, Card/StatCard
      PageHeader, Breadcrumbs, Tabs/TabPanel
      DataTable (+ useSort), Pagination, SearchInput, FilterBar, Timeline
      Form (FormField, TextInput, Select, Textarea), CurrencyInput, DateInput,
        TaxRateInput, FileUpload
      Modal, Drawer, ConfirmDialog, Portal, useFocusTrap
      EmptyState, ErrorState, QueryState, Spinner, Skeleton
    shell/                  ← the application shell (composes ui/)
      AppShell, nav.ts, Dropdown, UserMenu, EntitySwitcher (ScopeSwitcher)
  design/                   ← the showcase (fixtures-only, mounted at /design)
    fixtures.ts, DevBanner, DesignLayout, Gallery, routes/*
  lib/
    cx.ts                   ← className joiner
    format.ts               ← money/date formatting (pre-existing)
  e2e/                      ← Playwright smoke + visual-regression specs
```

### Principles (continuing the existing kit's conventions)

- **One public surface.** Every primitive is re-exported from
  `components/ui/index.ts`. Pages import from `"../components/ui"` and never
  deep-path, so internals refactor freely.
- **Composition over configuration.** `DataTable` takes a `columns` array whose
  `cell` renders arbitrary JSX (a `StatusBadge`, a `Button`) instead of a prop per
  variation. The shell's two switchers are one `ScopeSwitcher` parameterised by
  `kind`. Overlays share one `useFocusTrap` + `Portal`.
- **Pass intent, not classes.** `tone="danger"`, `variant="primary"`,
  `status="overdue"` — the palette stays central and re-themable.
- **Accessibility is structural, not bolted on** (see §3).
- **Layers.** `ui/` knows nothing about the app; `shell/` composes `ui/`;
  `design/` composes both with fixtures. Dependencies point one way.

### Design tokens

Colour, the `brand` scale, and component classes live in the Tailwind `@theme`
and utilities in `frontend/src/index.css`. Semantic roles reuse the existing
palette; brand marks, disabled states and decorative colors keep their meaning.

| Role | Utility examples | Purpose |
| --- | --- | --- |
| `action` / `action-hover` | `bg-action hover:bg-action-hover` | Enabled primary actions with white text; shared Button and legacy `btn-primary` use the same role. |
| `text-secondary` | `text-text-secondary` | Meaningful instructions, captions, timestamps and placeholders on the light surfaces. |
| `focus` | `focus-visible:ring-focus` | Visible keyboard location for buttons, inputs and related controls, including invalid fields. Preserve separate error borders and messages. |
| `feedback-success` | `bg-feedback-success` | Readable success confirmation with white text. |

State presentation is shared by `lib/statusStyles.ts` and the `state-*` roles
in `index.css`. `Badge` and `StatusBadge` use those presentation roles; legacy
format maps keep their own domain keys and labels. Do not replace a domain state
with a generic label merely because it shares a colour.

| Role family | Existing domain meaning retained |
| --- | --- |
| `neutral`, `muted` | Draft/open/no-validation/discarded; written off and cancelled keep the muted background, and cancelled keeps its strike-through. |
| `success` | Paid, confirmed, passed, reimbursed; validation approval keeps its existing success meaning. |
| `warning`, `warning-strong` | Pending, disputed, submitted; partially approved remains the stronger warning text. |
| `danger` | Overdue, failed, rejected, flagged. |
| `info`, `info-subtle` | Viewed/partial/queued/expense-approved; issued Sent keeps its lighter background. |
| `approved`, `credit`, `returned` | Issued approval/reimbursement marking, credits/extraction method, and returned expenses retain their distinct indigo/violet/orange families. |
| `control-on`, `control-off` | Enabled switch track states; native switch semantics and disabled behavior are unchanged. |
| `chart-1` … `chart-10`, `chart-budget`, `chart-grid` | Shared categorical markers, budget line and decorative grid. Labels/legend remain the source of category meaning. |

The incremental migration keeps readable existing status colours. Slate-500
status text moves to Slate-600, and Sent text from Sky-600 to Sky-700, to meet
the sampled 4.5:1 normal-text threshold on their actual light backgrounds.
Chart markers and enabled switch tracks use darker hues for a sampled 3:1
contrast against white; adjacent categories still require labels and do not gain
an automatic colour-difference guarantee. Disabled content and grid lines are
not claimed to meet enabled-information contrast.

Shared Button, legacy `btn`, Switch and overlay-close hit areas are at least
44 CSS pixels below the 640px breakpoint, with nonoverlapping layout boxes.
Desktop sizes and switch track geometry remain dense. The 44px enhancement is
separate from the 24px AA minimum and its spacing exceptions; Chromium rectangle,
keyboard and colour checks do not establish physical-device or full WCAG
acceptance. Named scrolling table regions use a positioned containing block so
visually hidden captions/header labels cannot escape to the document root.

Choose roles by context instead of replacing every palette use mechanically.
The repaired light-theme controls have rendered contrast measurements in the
[priority repair record](design/IMPECCABLE-PRIORITY-FIXES-2026-10-22.md); arbitrary
status combinations and other backgrounds require their own review. Forced-colors
mode uses a system-color outline without disabling user color adjustment.

Keyframe utility classes (`iq-fade-in`, `iq-pop-in`, `iq-slide-right/left`) are
defined in `index.css`, all gated behind
`@media (prefers-reduced-motion: no-preference)`.

---

## 2. Component APIs

Full prop tables live in each file's JSDoc; this is the index. All props are typed
and exported from the barrel.

### Structure & navigation
| Component | Key props | Purpose |
|---|---|---|
| `AppShell` | `navGroups`, `orgs/currentOrgId/onSwitchOrg`, `entities?/currentEntityId?/onSwitchEntity?`, `user/onSignOut`, `search?/onSearch?`, `breadcrumbs`, `banner`, `userMenuExtraItems?`, `accountHref?`, `children` | Sidebar + top bar wrapping routed content. `navGroups` is caller-supplied (no built-in nav opinion); `entities`/`search` are optional — omit when the caller has no legal-entity or search backend behind them (the live app omits both today) |
| `PageHeader` | `title`, `description`, `actions`, `breadcrumbs`, `meta` | The one place page `<h1>` + actions are composed |

**`PageHeader` is the ONLY sanctioned mechanism for a page title** (WO-45 /
UX1). A page renders exactly one `<h1>`, and it always comes from
`PageHeader` — never a hand-rolled `<h1 className="text-2xl ...">`. Where a
page has a loading/error/gated branch as well as its live-data branch,
`PageHeader` is hoisted ABOVE that branch (rendered once, unconditionally)
so the title stays stable while the data loads and no branch can produce a
second `<h1>` — this is what collapsed the duplicate headings
`Expenses.tsx`/`IssuedReports.tsx` used to render.
| `Breadcrumbs` | `items: {label, to?}[]` | Labelled `<nav>`; last crumb `aria-current="page"` |
| `Tabs` / `TabPanel` | `tabs`, `value`, `onChange`, `label`, `idBase` | WAI-ARIA tablist + linked panel; overflowing tabs scroll locally and reveal the selected tab without scrolling the document |
| `Dropdown` | `trigger(fn)`, `items`, `label`, `align` | Menu-button pattern; powers the switchers + user menu |
| `ScopeSwitcher` | `kind`, `options`, `currentId`, `onSwitch` | Org **and** legal-entity switcher; renders as static text when `options.length <= 1` |
| `UserMenu` | `user`, `onSignOut`, `extraItems`, `accountHref?` | Account menu; `accountHref` defaults to `/design/settings`, the live app passes `/settings` |

### Data display
| Component | Key props | Purpose |
|---|---|---|
| `DataTable<T>` | `columns`, `rows`, `rowKey`, `loading`, `empty`, `caption`, `onRowClick`, `sort`, `onSortChange`, `dense` | Column-driven table w/ skeletons, empty state, sorting, keyboard rows |
| `useSort<T>` | `(rows, accessors, initial)` → `{sort, toggle, sorted}` | Client-side sort state for `DataTable` |
| `Pagination` | `page`, `total`, `pageSize`, `onPageChange` | Prev/next + live range summary |
| `SearchInput` | `value`, `onChange`, `label`, `onSubmit` | `searchbox` with clear button |
| `FilterBar` / `FilterSelect` / `FilterChip` | container + labelled select + removable chip | Filter toolbar (`role="search"`) |
| `Timeline` | `events: {title, actor, timestamp, detail, tone}[]` | Audit-history list |
| `StatusBadge` | `status`, `map?` | Maps a status string → tone + label |

### Forms
| Component | Key props | Purpose |
|---|---|---|
| `FormField` | `label`, `hint`, `error`, `required`, `children(control)` | Labelled-field wrapper; the a11y source of truth |
| `TextInput` / `Select` / `Textarea` | `label`, `hint`, `error`, `required` + native attrs | Convenience wrappers over `FormField` |
| `CurrencyInput` | `label`, `value`, `onValueChange`, `currency` | Sanitised money input, symbol adornment, `inputMode="decimal"` |
| `DateInput` | `label`, `value`, `onValueChange`, `min/max` | Native `<input type="date">`, ISO value |
| `TaxRateInput` | `label`, `value`, `onValueChange`, `presets` | VAT % input + preset chips |
| `FileUpload` | `label`, `files`, `onFilesChange`, `accept`, `multiple` | Keyboard + drag-drop dropzone, file list |

### Overlays & feedback
| Component | Key props | Purpose |
|---|---|---|
| `Modal` | `open`, `onClose`, `title`, `description`, `footer`, `size`, `closeOnBackdrop` | Accessible dialog |
| `Drawer` | `open`, `onClose`, `title`, `side`, `size` | Side panel (same a11y contract) |
| `ConfirmDialog` | `open`, `onClose`, `onConfirm`, `title`, `tone`, `loading` | "Are you sure?" over `Modal` |
| `EmptyState` / `ErrorState` | `title`, `description`, `action`/`onRetry` | "Nothing yet" vs "something failed" |
| `QueryState` | `query`, `children(data)`, `loading`, `empty`, `isEmpty`, `errorTitle` | The async triad wrapper around one `useQuery` result |
| `Toast` (`toast.success/error`) | module bridge | `role="alert"` transient notices |
| `Spinner` / `Skeleton` / `SkeletonText` | — | Loading primitives |

**`QueryState`'s error branch renders `ErrorState` (`role="alert"`), never
`EmptyState`** (WO-45 / UX1 — before this, a failed request rendered
pixel-identical to a genuinely empty dataset, with no toast and no retry).
`EmptyState` means "no data yet, that's fine"; `ErrorState` means "the
request failed" — these are two different FACTS about the world and must
**never share copy** (e.g. "No invoices match these filters" vs "Couldn't
load invoices" — never one generic string doing both jobs). Fail-open vs
fail-closed: `QueryState` introduces no new gate and changes nothing about
what a role/plan is allowed to see — a 403 renders the identical `ErrorState`
a 500 would; the frontend still draws no authorization conclusion (master-
context §6), it only makes an already-failed request visibly failed instead
of silently empty.

---

## 3. Accessibility notes

Accessibility is verified by the smoke suite, which selects controls **by role and
name** — a control with no accessible name simply can't be driven, so the tests fail
if a11y regresses.

- **Overlays (`Modal`, `Drawer`, `ConfirmDialog`).** `role="dialog"` +
  `aria-modal`, labelled by the title (`aria-labelledby`) and described by the
  description. `useFocusTrap` moves focus in on open, **traps Tab / Shift+Tab**
  (wrapping at the ends), and **restores focus** to the trigger on close. Escape is
  handled at the **document capture level**, so it closes the overlay no matter
  where focus sits. Background scroll is locked. Backdrop click closes (opt-out for
  confirms). `Portal` mounts its target synchronously so the focus-trap ref is live
  on first commit (a deferred mount silently defeats the trap — caught by the e2e).
- **Menus (`Dropdown`).** `aria-haspopup="menu"` + `aria-expanded` on the trigger
  (which has an `aria-label`); `role="menu"` / `role="menuitem"`. Arrow Up/Down +
  Home/End move focus (skipping disabled items — the user menu's email header is a
  disabled item, so focus lands on the first *actionable* item), Escape closes and
  restores focus, outside-click dismisses.
- **Tabs.** `role="tablist"`, roving `tabIndex` (only the active tab is tabbable),
  Left/Right/Up/Down + Home/End move selection, panels wired via `aria-controls` /
  `aria-labelledby`.
- **Tables.** scoped `<th>`, sr-only `<caption>`, `aria-busy` while loading,
  **`aria-sort`** on sortable headers (ascending/descending/none), and
  keyboard-activatable rows (Enter/Space) when clickable.
- **Forms.** `FormField` wires `htmlFor`/`id`, `aria-describedby` (hint + error),
  `aria-invalid`, `aria-required`; validation messages use `role="alert"` so they
  announce on change. Native `<input type="date">` gives a platform-accessible
  picker; currency/tax inputs use `inputMode="decimal"` for a numeric mobile keypad.
- **File upload.** The dropzone is a real focusable `role="button"` (Enter/Space
  open the picker), the `<input type="file">` is `sr-only` but label-wired, and each
  file's remove button is individually labelled.
- **Shell.** A **skip link** jumps to `<main id="main">`; the mobile nav button
  exposes `aria-expanded`; breadcrumbs, primary nav, search, and pagination are all
  labelled landmarks/regions. Focus rings are visible on every interactive element.
- **Motion.** All entrance animations sit behind
  `prefers-reduced-motion: no-preference`; skeleton pulses disable under
  `motion-reduce`.

---

## 4. Responsive behavior

- **Shell.** Sidebar is fixed at `16rem` on `lg+`; below `lg` it collapses behind a
  hamburger that opens the **same nav in a `Drawer`**. The top bar's search hides on
  `< md`; the user menu collapses to just the avatar on `< sm`.
- **Page header.** Title and actions share a row on desktop, stack on mobile.
- **Tables.** Wrap in an `overflow-x-auto` container, so wide financial tables scroll
  horizontally inside their card without breaking the page layout.
- **Forms.** Grid form layouts (`sm:grid-cols-2`) collapse to a single column on
  mobile — the **expense-submission** flow (`/design/expenses`) is built this way so
  it's usable one-handed on a phone; approvals are status pills in the list.
- **Overlays.** Modals are width-capped and padded off the viewport edges; drawers go
  full-width on mobile, fixed-width on desktop.
- **Breadcrumbs / filter bar.** Scroll / wrap rather than overflow.

Breakpoints follow Tailwind defaults (`sm 640`, `md 768`, `lg 1024`). A mobile
snapshot at 390px is part of the visual-regression suite.

---

## 5. Stories — the component gallery

There is **no Storybook dependency**. The "story / example page for every reusable
component" is an in-app route so components render in their real Tailwind + runtime
environment:

- **`/design/gallery`** — the living style guide: one live example ("story") per
  primitive (buttons, badges, tables with sorting, forms with validation, currency/
  date/tax inputs, file upload, modal/drawer/confirm, toasts, timeline, empty/error/
  loading states), with an in-page nav.
- **`/design`** — the full **application shell** with the nine initial routes below,
  driven by fixtures, so the shell's real interactions (org/entity switch, search,
  responsive nav) are exercisable.

### Initial routes (fixtures-only)
`Dashboard` · `Supplier invoices` · `Expenses` · `Customer invoices` · `Payments` ·
`Reports` · `Contacts` · `Settings` · `Administration`

`Supplier invoices` is the reference list page (filter + sort + pagination + detail
drawer + confirm); `Expenses` is the reference form (currency/date/tax/upload in a
modal); `Payments` shows tabbed filtering; `Reports` deliberately shows an **empty
state** instead of fake charts.

Every `/design` screen carries a persistent amber **"Development fixtures"** banner.

---

## 6. Visual-regression test setup

Playwright screenshots the deterministic `/design` surface. Config:
`frontend/playwright.config.ts` — fixed 1280×900 viewport, `deviceScaleFactor: 1`,
animations frozen, caret hidden, `maxDiffPixelRatio: 0.02`. The browser is the
environment's **pre-installed Chromium** (pinned via `executablePath`), so no
`playwright install` download is needed.

```bash
cd frontend
npm run test:vr           # assert against committed baselines
npm run test:vr:update    # regenerate baselines after an intended visual change
```

Baselines live in `frontend/e2e/visual.spec.ts-snapshots/` (13 shots: the gallery,
all nine routes, modal + drawer open, and a 390px mobile view). Review the diff
before updating — an unexpected change is a regression.

---

## 7. End-to-end smoke tests

```bash
cd frontend
npm run test:e2e          # smoke.spec.ts + dashboard.spec.ts + masters.spec.ts + nav.spec.ts
npm run test:ui           # every e2e spec, incl. visual, together
```

`e2e/smoke.spec.ts` drives real user journeys through **role + accessible-name**
selectors (so it doubles as an a11y check) over the `/design` showcase: navigating
the IA, switching legal entity, keyboard-operating the user menu (Escape closes),
filtering/sorting the supplier list and opening a detail drawer → confirm → toast,
validating then submitting the expense form, tab-filtering payments, and trapping
focus in a modal.

Three further specs exercise the **live app shell** (mocked API via `page.route`,
no backend, same role/accessible-name discipline): `e2e/dashboard.spec.ts` (the
composed home dashboard, WO-16), `e2e/masters.spec.ts` (the AR/master-data
screens, WO-14) and `e2e/nav.spec.ts` (the grouped nav IA, WO-17/I1.2 — group
headings, permission/module filtering, the org switcher, the mobile drawer, the
breadcrumb).

These tests already earned their keep: `smoke.spec.ts` caught two real defects
during build-out — a `Portal` that mounted a render late (silently defeating the
modal focus trap) and a dropdown that focused a disabled item and ignored Escape.

**In CI.** The `frontend-e2e` job (`.github/workflows/ci.yml`) runs `npm run
test:e2e` — all four functional specs above — in the version-matched Playwright
container (`mcr.microsoft.com/playwright:v1.61.1-jammy`) — browsers are
preinstalled, and the config falls back to the bundled Chromium when the sandbox's
pinned browser isn't present. Visual regression (`visual.spec.ts`) is intentionally
a **local** gate, excluded from `test:e2e`: pixel baselines are captured against a
specific browser build, so running them on a different CI browser would false-diff
on font rendering. To gate VR in CI, regenerate the baselines inside that same
container and commit them.

---

## Running the showcase locally

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173/design  (and /design/gallery)
npm run build          # tsc --noEmit && vite build
```

No backend or login is required for `/design` — it's fixtures all the way down.
