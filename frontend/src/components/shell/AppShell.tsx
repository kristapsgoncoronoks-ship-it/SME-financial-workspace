import { useEffect, useId, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import type { NavGroup } from "./nav";
import { ScopeSwitcher, type ScopeSwitcherReadState, type ScopeSwitcherWriteState, type SwitcherOption } from "./EntitySwitcher";
import { UserMenu, type ShellUser } from "./UserMenu";
import type { DropdownItem } from "./Dropdown";
import { Drawer } from "../ui/Drawer";
import { SearchInput } from "../ui/SearchInput";
import { Breadcrumbs, type Crumb } from "../ui/Breadcrumbs";
import { CommandPalette } from "./CommandPalette";
import { cx } from "../../lib/cx";

export interface AppShellProps {
  /** The grouped nav to render. Caller-supplied so the shell has no built-in
   * opinion about destinations — `/design` passes its fixture IA, the live app
   * passes the real one (`frontend/src/lib/nav.ts`), already permission/module
   * filtered by the caller (the shell renders whatever it is given). */
  navGroups: NavGroup[];
  /** A caller-resolved parent for a detail URL outside its navigation prefix.
   * The destination must already be present in the filtered navGroups. */
  activeNavigationTo?: string;
  orgs: SwitcherOption[];
  currentOrgId: string;
  onSwitchOrg: (id: string) => void;
  /** Live organization-list recovery; omitted by fixture/showcase callers. */
  orgReadState?: ScopeSwitcherReadState;
  orgWriteState?: ScopeSwitcherWriteState;
  /** Legal-entity switcher — pass only when the caller supports changing this
   * scope. Invoice-level entity selection does not imply a global switcher. */
  entities?: SwitcherOption[];
  currentEntityId?: string;
  onSwitchEntity?: (id: string) => void;
  user: ShellUser;
  onSignOut: () => void;
  /** Global search box — omit when no search backend exists for the caller. */
  search?: string;
  onSearch?: (v: string) => void;
  breadcrumbs?: Crumb[];
  /** Full-width notice under the top bar (e.g. the dev-fixtures banner, or a
   * suspended-workspace notice). */
  banner?: ReactNode;
  /** Extra items appended to the user menu, before Sign out (e.g. "Sessions"). */
  userMenuExtraItems?: DropdownItem[];
  /** Where "Account settings" in the user menu points. */
  accountHref?: string;
  /** Quick navigation (Ctrl/⌘ K) over the SAME `navGroups` the sidebar renders —
   * no API call, no second authorization model (TW-P2-01). Off by default so
   * the design showcase is unaffected. */
  quickNavigation?: boolean;
  /** Live navigation may fold sections; fixture/showcase navigation is unchanged. */
  collapsibleNavigation?: boolean;
  /** Keep explicit expansion choices inside the current account/workspace. */
  navigationScope?: string;
  defaultExpandedNavigationGroups?: readonly string[];
  children: ReactNode;
}

function NavList({ navGroups, activeNavigationTo, onNavigate, collapsible, isExpanded, onToggleGroup }: {
  navGroups: NavGroup[];
  activeNavigationTo?: string;
  onNavigate?: () => void;
  collapsible: boolean;
  isExpanded: (title: string) => boolean;
  onToggleGroup: (title: string) => void;
}) {
  // Desktop and mobile render separate lists, so their controlled regions must
  // have different IDs even though their expansion choices are shared.
  const listId = useId();
  const linkClass = (active: boolean) => cx(
    "flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition",
    "focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus",
    active ? "bg-brand-50 text-brand-700" : "text-slate-600 hover:bg-slate-100 hover:text-slate-800",
  );
  return (
    <nav aria-label="Primary" className="flex flex-col gap-5 px-3 py-4">
      {navGroups.map((group, index) => (
        <div key={group.title}>
          {collapsible ? (
            <button
              type="button"
              aria-expanded={isExpanded(group.title)}
              aria-controls={`${listId}-${index}`}
              onClick={() => onToggleGroup(group.title)}
              className="flex w-full items-center justify-between gap-2 rounded-lg px-3 py-2 text-left text-xs font-semibold text-slate-600 transition hover:bg-slate-100 hover:text-slate-800 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus"
            >
              <span>{group.title}</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true" className={cx("shrink-0 transition-transform", isExpanded(group.title) && "rotate-90")}>
                <path d="m9 5 7 7-7 7" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          ) : (
            <p className="px-3 pb-1 text-xs font-semibold uppercase tracking-wide text-text-secondary">{group.title}</p>
          )}
          <ul id={`${listId}-${index}`} hidden={collapsible && !isExpanded(group.title)} className={cx("flex-col gap-0.5", collapsible && !isExpanded(group.title) ? "hidden" : "flex")}>
            {group.items.map((item) => {
              const content = <><span className="shrink-0 text-current opacity-80">{item.icon}</span>{item.label}</>;
              return (
                <li key={item.to}>
                  {item.to === activeNavigationTo ? (
                    <Link to={item.to} onClick={onNavigate} aria-current="page" className={linkClass(true)}>
                      {content}
                    </Link>
                  ) : (
                    <NavLink
                      to={item.to}
                      end={item.end ?? false}
                      onClick={onNavigate}
                      className={({ isActive }) => linkClass(isActive)}
                    >
                      {content}
                    </NavLink>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </nav>
  );
}

const NO_DEFAULT_EXPANSION: readonly string[] = [];

function currentNavigationGroup(navGroups: NavGroup[], pathname: string): string | undefined {
  let current: { title: string; length: number } | undefined;
  for (const group of navGroups) {
    for (const item of group.items) {
      // `end` controls link highlighting, not the containing section: an expense
      // detail still belongs to Expenses even though its list link is exact-only.
      const matches = item.to === pathname || (item.to !== "/" && pathname.startsWith(`${item.to}/`));
      if (matches && (!current || item.to.length > current.length)) current = { title: group.title, length: item.to.length };
    }
  }
  return current?.title;
}

function Brand() {
  return (
    <div className="flex items-center gap-2 px-4 py-4">
      <div className="grid h-8 w-8 place-items-center rounded-lg bg-brand-500 text-sm font-bold text-white">iQ</div>
      <span className="text-lg font-semibold tracking-tight text-slate-800">InvoiceIQ</span>
    </div>
  );
}

/**
 * The application shell: a persistent grouped sidebar + a sticky top bar wrapping
 * the routed page content. Responsive — on desktop the sidebar is fixed at 16rem;
 * below `lg` it collapses behind a hamburger that opens the same nav in a `Drawer`.
 * The top bar carries the org switcher (+ an optional legal-entity switcher and an
 * optional global search box, when the caller has something real to back them
 * with), and the user menu. A skip link jumps keyboard users straight to `<main>`.
 *
 * `navGroups` is caller-supplied and pre-filtered — this component has no opinion
 * about destinations, permissions or module gating; both the `/design` showcase
 * (`DesignLayout`) and the live app (`Layout`) mount it with their own nav data.
 */
export function AppShell({
  navGroups, activeNavigationTo,
  orgs, currentOrgId, onSwitchOrg, orgReadState, orgWriteState,
  entities, currentEntityId, onSwitchEntity,
  user, onSignOut, search, onSearch, breadcrumbs, banner,
  userMenuExtraItems, accountHref, quickNavigation = false,
  collapsibleNavigation = false, navigationScope = "",
  defaultExpandedNavigationGroups = NO_DEFAULT_EXPANSION,
  children,
}: AppShellProps) {
  const [mobileNav, setMobileNav] = useState(false);
  const { pathname } = useLocation();
  const activeGroup = currentNavigationGroup(navGroups, activeNavigationTo ?? pathname);
  const [expansion, setExpansion] = useState<{ scope: string; overrides: Record<string, boolean> }>(
    () => ({ scope: navigationScope, overrides: {} }),
  );

  useEffect(() => {
    if (!collapsibleNavigation) return;
    setExpansion((previous) => {
      if (previous.scope !== navigationScope) return { scope: navigationScope, overrides: {} };
      // Landing through a link, a deep URL or quick navigation exposes the
      // current section, including one the person had previously folded.
      // Automatic expansion is derived below rather than stored, so visiting
      // many sections does not gradually unfold the entire menu again.
      if (activeGroup && previous.overrides[activeGroup] === false) {
        const overrides = { ...previous.overrides };
        delete overrides[activeGroup];
        return { scope: navigationScope, overrides };
      }
      return previous;
    });
  }, [activeGroup, collapsibleNavigation, navigationScope, pathname]);

  const defaultExpanded = (title: string) => title === activeGroup || defaultExpandedNavigationGroups.includes(title);
  const isExpanded = (title: string) =>
    expansion.scope === navigationScope ? expansion.overrides[title] ?? defaultExpanded(title) : defaultExpanded(title);
  const toggleGroup = (title: string) => setExpansion((previous) => {
    const overrides = previous.scope === navigationScope ? previous.overrides : {};
    return { scope: navigationScope, overrides: { ...overrides, [title]: !(overrides[title] ?? defaultExpanded(title)) } };
  });

  const switchers = (
    <div className="flex flex-col gap-2 border-y border-slate-100 px-3 py-3">
      <ScopeSwitcher kind="organization" options={orgs} currentId={currentOrgId} onSwitch={onSwitchOrg} readState={orgReadState} writeState={orgWriteState} />
      {entities && entities.length > 0 && (
        <ScopeSwitcher
          kind="legal entity"
          options={entities}
          currentId={currentEntityId ?? entities[0].id}
          onSwitch={onSwitchEntity ?? (() => {})}
        />
      )}
    </div>
  );

  return (
    <div className="min-h-screen bg-slate-50 text-slate-800">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-3 focus:top-3 focus:z-50 focus:rounded-lg focus:bg-brand-600 focus:px-3 focus:py-2 focus:text-sm focus:text-white"
      >
        Skip to content
      </a>

      {/* Desktop sidebar */}
      <aside className="fixed inset-y-0 left-0 hidden w-64 flex-col border-r border-slate-200 bg-white lg:flex">
        <Brand />
        {switchers}
        <div className="flex-1 overflow-y-auto">
          <NavList navGroups={navGroups} activeNavigationTo={activeNavigationTo} collapsible={collapsibleNavigation} isExpanded={isExpanded} onToggleGroup={toggleGroup} />
        </div>
      </aside>

      {/* Mobile nav drawer */}
      <Drawer open={mobileNav} onClose={() => setMobileNav(false)} title="Menu" side="left" size="sm">
        {switchers}
        <NavList navGroups={navGroups} activeNavigationTo={activeNavigationTo} onNavigate={() => setMobileNav(false)} collapsible={collapsibleNavigation} isExpanded={isExpanded} onToggleGroup={toggleGroup} />
      </Drawer>

      <div className="lg:pl-64">
        {/* Top bar */}
        <header className="sticky top-0 z-30 border-b border-slate-200 bg-white/90 backdrop-blur-sm">
          <div className="flex items-center gap-3 px-4 py-2.5">
            <button
              type="button"
              className="rounded-lg p-2 text-slate-500 hover:bg-slate-100 focus-visible:outline-hidden focus-visible:ring-2 focus-visible:ring-focus lg:hidden"
              aria-label="Open navigation menu"
              aria-expanded={mobileNav}
              onClick={() => setMobileNav(true)}
            >
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M4 6h16M4 12h16M4 18h16" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
              </svg>
            </button>

            <div className="min-w-0 flex-1">
              {breadcrumbs && breadcrumbs.length > 0 && <Breadcrumbs items={breadcrumbs} />}
            </div>

            {onSearch && (
              <SearchInput
                value={search ?? ""}
                onChange={onSearch}
                label="Search the workspace"
                placeholder="Search…"
                className="hidden w-64 md:block"
              />
            )}
            {quickNavigation && <CommandPalette navGroups={navGroups} />}
            <UserMenu user={user} onSignOut={onSignOut} extraItems={userMenuExtraItems} accountHref={accountHref} />
          </div>
        </header>

        {banner}

        <main id="main" tabIndex={-1} className="mx-auto max-w-6xl px-4 py-6 focus:outline-hidden sm:px-6">
          {children}
        </main>
      </div>
    </div>
  );
}
