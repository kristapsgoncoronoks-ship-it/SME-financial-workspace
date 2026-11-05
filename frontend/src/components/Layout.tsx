import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../auth/AuthContext";
import { isOwner } from "../lib/roles";
import { useModules } from "../lib/useModules";
import { useOrgSwitcher } from "../lib/useOrgSwitcher";
import { LIVE_NAV, LIVE_GROUP_TITLES, matchNavItem, matchNavParent, type LiveNavGroup, type LiveNavItem } from "../lib/nav";
import { ErrorBoundary } from "./ErrorBoundary";
import { AppShell } from "./shell/AppShell";
import type { NavGroup } from "./shell/nav";
import { icon } from "./shell/nav";
import type { Crumb } from "./ui/Breadcrumbs";

const DEFAULT_EXPANDED_NAVIGATION_GROUPS = [LIVE_GROUP_TITLES.overview];

/**
 * The application shell for the live app (board I1.2) — mounts the same `AppShell`
 * the `/design` showcase uses (`docs/DESIGN_SYSTEM.md`), with task-focused
 * sections and a quiet default: Overview and the current section are open.
 * Filtering is unchanged in spirit from the old flat `NAV` array:
 * a `module` flag hides an item until that add-on is enabled and `perm` hides it
 * unless the caller's SERVED permissions include the one the destination's router
 * requires (PROD-003 — this replaced the `admin`/`owner` ladder flags, which
 * disagreed with the 8-role matrix). This filtering is cosmetic UX only — the
 * server is the real permission boundary (master-context §6), unchanged by this.
 *
 * Entity choice remains in the relevant invoice workflows. Go to page searches
 * the filtered page list; it does not imply a workspace-record search endpoint.
 */
function filterNav(
  groups: LiveNavGroup[],
  opts: {
    isEnabled: (m: string) => boolean;
    /** PROD-003: the caller's SERVED permissions decide what is listed. */
    hasPerm: (p: string) => boolean;
  },
): NavGroup[] {
  return groups
    .map((g) => ({
      title: g.title,
      items: g.items.filter(
        (i: LiveNavItem) => (!i.module || opts.isEnabled(i.module)) && opts.hasPerm(i.perm),
      ),
    }))
    .filter((g) => g.items.length > 0);
}

export function Layout() {
  const { user, org, logout, hasPerm } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { isEnabled } = useModules();
  const orgSwitcher = useOrgSwitcher();

  const owner = isOwner(user);
  const navGroups = filterNav(LIVE_NAV, { isEnabled, hasPerm });
  if (user?.is_platform_admin) {
    const workspace = navGroups.find((g) => g.title === LIVE_GROUP_TITLES.settings);
    const platformItem = { to: "/platform", label: "Platform", icon: icon("M4 4h16v16H4V4zm4 4h8v8H8V8z") };
    if (workspace) workspace.items.push(platformItem);
    else navGroups.push({ title: LIVE_GROUP_TITLES.settings, items: [platformItem] });
  }

  const suspended = Boolean(org?.status && org.status !== "active");

  // PROD-001 (audit 2026-09-05): a suspended workspace renders ONE destination.
  // Every data route answers 401 while the org is suspended, so the normal nav
  // would be a wall of dead links and silent empty tables. The owner (the only
  // role holding BILLING_MANAGE) is routed to Plan & billing, which the server
  // still serves for a suspended org; everyone else gets the reason and who can
  // act on it. Note the banner below used to be unreachable: the identity read
  // itself 401'd, so the shell never mounted for a suspended tenant.
  const billingItem = LIVE_NAV.flatMap((g) => g.items).find((i) => i.to === "/billing");
  const suspendedNav: NavGroup[] =
    suspended && owner && billingItem ? [{ title: LIVE_GROUP_TITLES.settings, items: [billingItem] }] : [];
  const visibleNavGroups = suspended ? suspendedNav : navGroups;
  const parent = matchNavParent(pathname, visibleNavGroups);
  const current = parent?.item ?? matchNavItem(pathname, visibleNavGroups);
  const crumbs: Crumb[] = parent
    ? [{ label: parent.item.label, to: parent.to }, { label: parent.currentLabel }]
    : current ? [{ label: current.label }] : [];

  return (
    <AppShell
      navGroups={visibleNavGroups}
      activeNavigationTo={parent?.item.to}
      orgs={orgSwitcher.options}
      currentOrgId={orgSwitcher.currentId}
      onSwitchOrg={orgSwitcher.onSwitch}
      orgReadState={orgSwitcher.readState}
      orgWriteState={orgSwitcher.writeState}
      user={{ name: user?.name ?? user?.email ?? "—", email: user?.email ?? "", role: user?.role ?? "" }}
      onSignOut={async () => {
        const revocation = logout();
        navigate("/login");
        await revocation;
      }}
      breadcrumbs={crumbs}
      quickNavigation={!suspended}
      collapsibleNavigation
      navigationScope={`${user?.id ?? ""}:${org?.id ?? ""}`}
      defaultExpandedNavigationGroups={DEFAULT_EXPANDED_NAVIGATION_GROUPS}
      userMenuExtraItems={[{ key: "sessions", label: "Sessions", href: "/sessions" }]}
      accountHref="/settings"
      banner={
        suspended ? (
          <div role="alert" className="bg-rose-600 px-4 py-2 text-center text-sm font-medium text-white">
            This workspace is {org?.status}.{" "}
            {owner
              ? "Update the plan or payment details on Plan & billing to restore access, or contact support if you believe this is a mistake."
              : "Only the workspace owner can restore access — ask them to check Plan & billing."}
          </div>
        ) : undefined
      }
    >
      {/* FE-003: a render throw in a page takes out that page, not the shell. */}
      {!suspended ? (
        <ErrorBoundary resetKey={pathname}>
          <Outlet />
        </ErrorBoundary>
      ) : owner ? (
        pathname === "/billing" ? (
          <ErrorBoundary resetKey={pathname}>
            <Outlet />
          </ErrorBoundary>
        ) : (
          <Navigate to="/billing" replace />
        )
      ) : (
        <div className="mx-auto max-w-xl">
          <div className="card space-y-2">
            <h1 className="text-lg font-semibold">This workspace is suspended</h1>
            <p className="text-sm text-slate-600">
              Your account is fine; the workspace&apos;s subscription is not. Nothing here is lost —
              access returns as soon as the workspace owner updates the plan or payment details.
            </p>
            <p className="text-sm text-slate-500">
              Owner: {orgSwitcher.options.find((o) => o.id === orgSwitcher.currentId)?.name ?? org?.name}
              {" "}— reach them directly; support cannot change a workspace&apos;s billing on a member&apos;s request.
            </p>
          </div>
        </div>
      )}
    </AppShell>
  );
}
