import { test, expect, type Page } from "@playwright/test";

/**
 * Happy-path e2e for the WO-14 master-data screens (tax codes, currencies, cost
 * objects, documents) over the LIVE app shell, with the API mocked via
 * `page.route` — deterministic, no backend. The mock keeps a tiny in-memory
 * store per test so a create round-trips into the next list fetch, which is
 * exactly what the screens do via query invalidation.
 *
 * Synthetic fixtures only (master context §10): fictional codes and names.
 */

const ORG = { id: "org-1", name: "Test Workspace", status: "active" };
const USER = {
  id: "user-1",
  email: "owner@test.io",
  name: "Owner",
  role: "owner",
  org_id: "org-1",
};

interface Store {
  taxCodes: Record<string, unknown>[];
  currencies: Record<string, unknown>[];
  departments: Record<string, unknown>[];
  costCenters: Record<string, unknown>[];
  projects: Record<string, unknown>[];
  documents: Record<string, unknown>[];
}

function emptyStore(): Store {
  return {
    taxCodes: [],
    currencies: [],
    departments: [],
    costCenters: [],
    projects: [],
    documents: [],
  };
}

let nextId = 1;
function makeId(prefix: string): string {
  return `${prefix}-${nextId++}`;
}

/** Wire the auth/session + master-data API mock. Returns the store so tests can seed it. */
async function mockApi(page: Page, store: Store): Promise<void> {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));

  const json = (body: unknown, status = 200) => ({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^.*\/api\/v1/, "");
    const method = route.request().method();
    const body = () => route.request().postDataJSON() as Record<string, unknown>;

    // --- session bootstrap ---
    if (path === "/auth/me") return route.fulfill(json({ user: USER, organization: ORG }));
    if (path === "/auth/organizations") return route.fulfill(json([{ org_id: ORG.id, name: ORG.name, role: USER.role, current: true }]));
    if (path === "/modules") return route.fulfill(json([]));

    // --- tax codes ---
    if (path === "/tax-codes" && method === "GET") {
      const all = url.searchParams.get("include_inactive") === "true";
      return route.fulfill(json(store.taxCodes.filter((t) => all || t.active)));
    }
    if (path === "/tax-codes" && method === "POST") {
      const row = { id: makeId("tc"), country: null, active: true, version: 1, ...body() };
      store.taxCodes.push(row);
      return route.fulfill(json(row, 201));
    }
    if (path.startsWith("/tax-codes/") && method === "PATCH") {
      const row = store.taxCodes.find((t) => t.id === path.split("/")[2])!;
      row.active = body().active;
      row.version = (row.version as number) + 1;
      return route.fulfill(json(row));
    }

    // --- currencies ---
    if (path === "/currencies" && method === "GET") {
      const all = url.searchParams.get("include_inactive") === "true";
      return route.fulfill(json(store.currencies.filter((c) => all || c.active)));
    }
    if (path === "/currencies" && method === "POST") {
      const row = { id: makeId("cur"), symbol: null, active: true, version: 1, ...body() };
      store.currencies.push(row);
      return route.fulfill(json(row, 201));
    }

    // --- costing masters ---
    const masters: Record<string, Record<string, unknown>[]> = {
      departments: store.departments,
      "cost-centers": store.costCenters,
      projects: store.projects,
    };
    const m = path.match(/^\/masters\/([a-z-]+)(?:\/([^/]+))?$/);
    if (m && masters[m[1]]) {
      const rows = masters[m[1]];
      if (!m[2] && method === "GET") {
        const all = url.searchParams.get("include_archived") === "true";
        return route.fulfill(json(rows.filter((r) => all || r.status !== "archived")));
      }
      if (!m[2] && method === "POST") {
        const row = { id: makeId(m[1]), status: "active", version: 1, ...body() };
        rows.push(row);
        return route.fulfill(json(row, 201));
      }
      if (m[2] && method === "PATCH") {
        const row = rows.find((r) => r.id === m[2])!;
        const b = body();
        if (b.name) row.name = b.name;
        if (b.status) row.status = b.status;
        row.version = (row.version as number) + 1;
        return route.fulfill(json(row));
      }
    }

    // --- document registry ---
    if (path === "/documents/page" && method === "GET") {
      const kind = url.searchParams.get("kind");
      const rows = store.documents.filter((d) => !kind || d.kind === kind);
      const offset = Number(url.searchParams.get("offset") ?? 0), limit = Number(url.searchParams.get("limit") ?? 100);
      return route.fulfill(json({ items: rows.slice(offset, offset + limit), total: rows.length, kinds: Array.from(new Set(store.documents.map((d) => d.kind))).sort() }));
    }

    // Anything else the shell touches: harmless empty list.
    return route.fulfill(json([]));
  });
}

test.describe("tax codes", () => {
  test("lists, creates and archives a code", async ({ page }) => {
    const store = emptyStore();
    store.taxCodes.push({
      id: "tc-seed",
      code: "STD-21",
      name: "Standard 21%",
      rate: "21.000",
      category: "standard",
      country: "LV",
      active: true,
      version: 1,
    });
    await mockApi(page, store);
    await page.goto("/tax-codes");

    await expect(page.getByRole("heading", { level: 1, name: "Tax codes" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "Standard 21%" })).toBeVisible();

    await page.getByRole("button", { name: "New tax code" }).click();
    const dlg = page.getByRole("dialog", { name: "New tax code" });
    await dlg.getByLabel("Code", { exact: true }).fill("RED-9");
    await dlg.getByLabel("Rate (%)").fill("9");
    await dlg.getByLabel("Name", { exact: true }).fill("Reduced 9%");
    await dlg.getByRole("button", { name: "Create tax code" }).click();
    await expect(page.getByRole("cell", { name: "Reduced 9%" })).toBeVisible();

    // Archive drops the row from the default (active-only) list.
    await page
      .getByRole("row", { name: /Reduced 9%/ })
      .getByRole("button", { name: "Archive" })
      .click();
    await expect(page.getByRole("cell", { name: "Reduced 9%" })).toBeHidden();
  });

  test("shows the empty state when no codes exist", async ({ page }) => {
    await mockApi(page, emptyStore());
    await page.goto("/tax-codes");
    await expect(page.getByText("No tax codes yet")).toBeVisible();
  });
});

test.describe("currencies", () => {
  test("lists and creates a currency", async ({ page }) => {
    const store = emptyStore();
    store.currencies.push({
      id: "cur-seed",
      code: "EUR",
      name: "Euro",
      symbol: "€",
      decimal_places: 2,
      active: true,
      version: 1,
    });
    await mockApi(page, store);
    await page.goto("/currencies");

    await expect(page.getByRole("heading", { level: 1, name: "Currencies" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "Euro" })).toBeVisible();

    await page.getByRole("button", { name: "New currency" }).click();
    const dlg = page.getByRole("dialog", { name: "New currency" });
    // Typed key by key, not `fill()`: FE-022 (reference R4 review) — the focus
    // trap used to re-run on every re-render and move focus to the dialog's
    // Close button after the first character, so a real keyboard lost the
    // rest. `pressSequentially` re-renders per key exactly as a person does.
    const iso = dlg.getByLabel("ISO code");
    await iso.click();
    await iso.pressSequentially("SEK");
    await expect(iso).toHaveValue("SEK");
    await expect(iso).toBeFocused();
    await dlg.getByLabel("Name", { exact: true }).fill("Swedish Krona");
    await dlg.getByRole("button", { name: "Create currency" }).click();
    await expect(page.getByRole("cell", { name: "Swedish Krona" })).toBeVisible();
  });
});

test.describe("cost objects", () => {
  test("creates a department and closes a project", async ({ page }) => {
    const store = emptyStore();
    store.projects.push({
      id: "prj-seed",
      code: "PRJ-1",
      name: "Fleet rollout",
      status: "active",
      version: 1,
      start_date: null,
      end_date: null,
    });
    await mockApi(page, store);
    await page.goto("/cost-objects");

    await expect(page.getByRole("heading", { level: 1, name: "Cost objects" })).toBeVisible();
    // Departments tab is default and empty.
    await expect(page.getByText("No departments yet")).toBeVisible();

    await page.getByRole("button", { name: "New department" }).click();
    const dlg = page.getByRole("dialog", { name: "New department" });
    await dlg.getByLabel("Code", { exact: true }).fill("OPS");
    await dlg.getByLabel("Name", { exact: true }).fill("Operations");
    await dlg.getByRole("button", { name: "Create department" }).click();
    await expect(page.getByRole("cell", { name: "Operations" })).toBeVisible();

    // Switch to projects; the seeded project can be closed (project-only state).
    await page.getByRole("tab", { name: "Projects" }).click();
    await expect(page.getByRole("cell", { name: "Fleet rollout" })).toBeVisible();
    await page.getByRole("button", { name: "Close", exact: true }).click();
    await expect(page.getByRole("row", { name: /Fleet rollout/ }).getByText("closed")).toBeVisible();
  });
});

test.describe("documents", () => {
  test("lists the registry and filters by kind", async ({ page }) => {
    const store = emptyStore();
    store.documents.push(
      {
        id: "doc-1",
        sha256: "a".repeat(64),
        size: 52_431,
        mime: "application/pdf",
        kind: "invoice",
        filename: "fuel-march.pdf",
        uploaded_by: "owner@test.io",
        created_at: "2026-07-01T10:00:00Z",
      },
      {
        id: "doc-2",
        sha256: "b".repeat(64),
        size: 1_204,
        mime: "image/png",
        kind: "receipt",
        filename: "parking.png",
        uploaded_by: "owner@test.io",
        created_at: "2026-07-02T10:00:00Z",
      },
    );
    await mockApi(page, store);
    await page.goto("/documents");

    await expect(page.getByRole("heading", { level: 1, name: "Documents" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "fuel-march.pdf" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "parking.png" })).toBeVisible();

    await page.getByLabel("Kind").selectOption("invoice");
    await expect(page.getByRole("cell", { name: "parking.png" })).toBeHidden();
    await expect(page.getByRole("cell", { name: "fuel-march.pdf" })).toBeVisible();
  });

  test("shows the error state when the registry fails", async ({ page }) => {
    await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "e2e-token"));
    await page.route("**/api/v1/**", async (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path.endsWith("/auth/me")) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({ user: USER, organization: ORG }),
        });
      }
      if (path.endsWith("/auth/organizations")) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([{ org_id: ORG.id, name: ORG.name, role: USER.role, current: true }]),
        });
      }
      if (path.endsWith("/documents/page")) {
        return route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ detail: "boom", code: "internal_error" }),
        });
      }
      return route.fulfill({ status: 200, contentType: "application/json", body: "[]" });
    });
    await page.goto("/documents");
    await expect(page.getByText("Couldn’t load the document registry")).toBeVisible();
  });
});
