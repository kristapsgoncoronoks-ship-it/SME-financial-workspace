import { test, expect, type Locator } from "@playwright/test";
import * as format from "../src/lib/format";

// Synthetic gallery states exercise the real shared controls and generated CSS.
// Pixel-derived sRGB values measure rendered colors, including Tailwind OKLCH.
async function textContrast(locator: Locator, foregroundProperty = "color"): Promise<number> {
  return locator.evaluate((element, foregroundProperty) => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
    function rgb(color: string): number[] {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, 1, 1);
      return Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3);
    }
    function luminance(channels: number[]): number {
      const linear = channels.map((channel) => {
        const s = channel / 255;
        return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    }
    const parents: Element[] = [];
    for (let node: Element | null = element; node; node = node.parentElement) parents.push(node);
    ctx.fillStyle = "white";
    ctx.fillRect(0, 0, 1, 1);
    for (const node of parents.reverse()) {
      ctx.fillStyle = getComputedStyle(node).backgroundColor;
      ctx.fillRect(0, 0, 1, 1);
    }
    const background = Array.from(ctx.getImageData(0, 0, 1, 1).data).slice(0, 3);
    const foreground = rgb(getComputedStyle(element).getPropertyValue(foregroundProperty));
    const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
    return (values[0] + 0.05) / (values[1] + 0.05);
  }, foregroundProperty);
}

async function focusVisible(locator: Locator): Promise<boolean> {
  return locator.evaluate((element) => {
    if (!element.matches(":focus-visible")) return false;
    const style = getComputedStyle(element);
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const ctx = canvas.getContext("2d", { willReadFrequently: true })!;
    const luminance = (channels: number[]) => {
      const linear = channels.slice(0, 3).map((c) => {
        const s = c / 255;
        return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
      });
      return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
    };
    const colors: string[] = [];
    if (style.outlineStyle !== "none" && Number.parseFloat(style.outlineWidth) >= 2) colors.push(style.outlineColor);
    // Inspect painted shadow colors; a white ring on a white surface must fail.
    colors.push(...(style.boxShadow.match(/rgba?\([^)]+\)/g) ?? []));
    return colors.some((color) => {
      ctx.clearRect(0, 0, 1, 1);
      ctx.fillStyle = color;
      ctx.fillRect(0, 0, 1, 1);
      const rgba = Array.from(ctx.getImageData(0, 0, 1, 1).data);
      if (rgba[3] === 0) return false;
      // Gallery buttons sit on a white canvas; composite translucent indicators.
      const rgb = rgba.slice(0, 3).map((c) => c * rgba[3] / 255 + 255 * (1 - rgba[3] / 255));
      return 1.05 / (luminance(rgb) + 0.05) >= 3;
    });
  });
}

test("shared primary action text meets normal-text contrast before and during hover", async ({ page }) => {
  await page.goto("/design/gallery");
  const primary = page.getByRole("button", { name: "Primary", exact: true });
  await expect(primary).toBeEnabled();
  expect(await textContrast(primary)).toBeGreaterThanOrEqual(4.5);
  await primary.hover();
  await expect.poll(() => textContrast(primary)).toBeGreaterThanOrEqual(4.5);
  await expect(page.getByRole("button", { name: "Disabled", exact: true })).toBeDisabled();
});

test("keyboard focus remains visible on every button variant and invalid field, including forced colors", async ({ page }) => {
  await page.goto("/design/gallery");
  const primary = page.getByRole("button", { name: "Primary", exact: true });
  await primary.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(primary).toBeFocused();
  await expect.poll(() => focusVisible(primary)).toBe(true);
  for (const name of ["Secondary", "Ghost", "Danger", "Subtle"]) {
    const button = page.getByRole("button", { name, exact: true });
    await button.focus();
    await expect.poll(() => focusVisible(button)).toBe(true);
  }
  await page.getByLabel("Full name").fill("Synthetic name");
  await page.keyboard.press("Tab");
  const invalid = page.getByLabel("Email", { exact: true });
  await expect(invalid).toHaveAttribute("aria-invalid", "true");
  await expect(invalid).toBeFocused();
  await expect.poll(() => focusVisible(invalid)).toBe(true);
  await expect(page.getByRole("alert").filter({ hasText: "Example error message" })).toBeVisible();
  await page.emulateMedia({ forcedColors: "active" });
  await primary.focus();
  await expect.poll(() => focusVisible(primary)).toBe(true);
});

test("shared instructions, financial adornments and timeline timestamps remain readable", async ({ page }) => {
  await page.goto("/design/gallery");
  for (const locator of [
    page.getByText("As it appears on the invoice", { exact: true }),
    page.getByText("Amount in EUR", { exact: true }),
    page.locator("#timeline time").first(),
    page.locator("#pagination nav span").filter({ hasText: /^Page / }),
  ]) {
    await expect(locator).toBeVisible();
    expect(await textContrast(locator)).toBeGreaterThanOrEqual(4.5);
  }
});

test("success confirmation uses readable text and remains dismissible", async ({ page }) => {
  await page.goto("/design/gallery");
  await page.getByRole("button", { name: "Success toast", exact: true }).click();
  const alert = page.getByRole("alert").filter({ hasText: "Saved successfully" });
  await expect(alert).toBeVisible();
  expect(await textContrast(alert.locator("span").first())).toBeGreaterThanOrEqual(4.5);
  await alert.getByRole("button", { name: "Dismiss", exact: true }).click();
  await expect(alert).toHaveCount(0);
});


test("legacy sign-in actions retain readable text and visible keyboard focus", async ({ page }) => {
  await page.goto("/login");
  const signIn = page.getByRole("button", { name: "Sign in", exact: true });
  await expect(signIn).toBeVisible();
  expect(await textContrast(signIn)).toBeGreaterThanOrEqual(4.5);
  await page.getByLabel("Password", { exact: true }).focus();
  await page.keyboard.press("Tab");
  await expect(signIn).toBeFocused();
  await expect.poll(() => focusVisible(signIn)).toBe(true);
});

test("shell scope labels are legible and search can be cleared with the keyboard", async ({ page }) => {
  await page.goto("/design");
  for (const name of ["Organization", "Legal entity"]) {
    const label = page.getByText(name, { exact: true });
    await expect(label).toBeVisible();
    expect(await label.evaluate((element) => parseFloat(getComputedStyle(element).fontSize))).toBeGreaterThanOrEqual(12);
    expect(await textContrast(label)).toBeGreaterThanOrEqual(4.5);
  }
  const search = page.getByRole("searchbox");
  await search.fill("synthetic");
  await page.keyboard.press("Tab");
  const clear = page.getByRole("button", { name: "Clear search", exact: true });
  await expect(clear).toBeFocused();
  await expect.poll(() => focusVisible(clear)).toBe(true);
  await page.keyboard.press("Enter");
  await expect(search).toHaveValue("");
});

test("financial chart SVG axis text resolves the readable shared color", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-accessibility-token"));
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(/^.*\/api\/v1/, "");
    const org = { id: "org-a11y", name: "Synthetic Workspace", status: "active" };
    let body: unknown = [];
    if (path === "/auth/me") body = {
      user: { id: "user-a11y", email: "owner@example.test", name: "Owner", role: "owner", org_id: org.id },
      organization: org,
    };
    if (path === "/auth/organizations") body = [{ org_id: org.id, name: org.name, role: "owner", current: true }];
    if (path === "/dashboard") body = {
      as_of: "2026-10-22", approvals: null, captures: null, payables: null, receivables: null,
      cash: { currency: "EUR", receivables_outstanding: "100.00", payables_outstanding: "0.00", net_position: "100.00" },
    };
    if (path === "/analytics/spend-over-time") body = {
      currency: "EUR", available_currencies: ["EUR"],
      rows: [{ period: "2026-09", total: "100.00", invoice_count: 1 }, { period: "2026-10", total: "150.00", invoice_count: 2 }],
    };
    if (path === "/analytics/top-vendors") body = {
      currency: "EUR", available_currencies: ["EUR"],
      rows: [{ vendor_id: "vendor-a11y", vendor_name: "Synthetic Supplier", total: "250.00", invoice_count: 3 }],
    };
    if (path === "/analytics/by-category") body = { currency: "EUR", available_currencies: ["EUR"], rows: [] };
    return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Show spend snapshot", exact: true }).click();
  const ticks = page.locator(".recharts-cartesian-axis-tick-value");
  await expect.poll(() => ticks.count()).toBeGreaterThan(0);
  for (const tick of await ticks.all()) expect(await textContrast(tick, "fill")).toBeGreaterThanOrEqual(4.5);
});

for (const width of [390, 1280]) {
  test(`shared action, close and switch targets preserve density and native behavior at ${width}px`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("/design/gallery");
    const measured: Record<string, unknown> = {};
    const controls = page.locator("#buttons button");
    await expect(controls).toHaveCount(8);
    const boxes = await controls.evaluateAll(elements => elements.map(e => { const r = e.getBoundingClientRect(); return { label: e.textContent, x: r.x, y: r.y, width: r.width, height: r.height }; }));
    measured.buttons = boxes;
    await page.getByRole("button", { name: "Open modal", exact: true }).click();
    const close = page.getByRole("button", { name: "Close dialog", exact: true });
    measured.close = await close.boundingBox();
    await page.keyboard.press("Escape");
    let checked = false;
    const writes: unknown[] = [];
    await page.addInitScript(() => localStorage.setItem("invoiceiq_token", "synthetic-target-token"));
    await page.route("**/api/v1/**", async route => {
      const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
      const org = { id: "target-org", name: "Synthetic target workspace", status: "active" };
      const user = { id: "target-owner", name: "Owner", email: "owner@example.test", role: "owner", org_id: org.id };
      let body: unknown = [];
      if (path === "/auth/me") body = { user, organization: org };
      if (path === "/auth/organizations") body = [{ org_id: org.id, name: org.name, current: true }];
      if (path === "/team/members") body = [{ id: "target-member", name: "Synthetic member", email: "member@example.test", role: "user", is_active: true, is_expense_approver: checked, created_at: null }];
      if (path === "/team/members/target-member" && route.request().method() === "PATCH") { body = route.request().postDataJSON(); writes.push(body); checked = (body as { is_expense_approver: boolean }).is_expense_approver; }
      return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
    });
    await page.goto("/team");
    const toggle = page.getByRole("switch", { name: "Toggle expense approver for member@example.test", exact: true });
    await expect(toggle).toBeVisible();
    measured.switch = await toggle.boundingBox();
    await testInfo.attach("shared-target-rectangles", { body: JSON.stringify({ viewport: width, measured, interpretation: "24px minimum and any spacing exception must be assessed separately from the enhanced 44px mobile goal. Chromium bounds are not physical-device qualification." }), contentType: "application/json" });
    if (width < 640) {
      for (const box of [...boxes, measured.close, measured.switch] as { width: number; height: number }[]) { expect(box.width).toBeGreaterThanOrEqual(44); expect(box.height).toBeGreaterThanOrEqual(44); }
      for (let a = 0; a < boxes.length; a++) for (let b = a + 1; b < boxes.length; b++) {
        const x = Math.min(boxes[a].x + boxes[a].width, boxes[b].x + boxes[b].width) - Math.max(boxes[a].x, boxes[b].x);
        const y = Math.min(boxes[a].y + boxes[a].height, boxes[b].y + boxes[b].height) - Math.max(boxes[a].y, boxes[b].y);
        expect(x <= 0 || y <= 0, `${boxes[a].label} and ${boxes[b].label} hit areas must not overlap`).toBe(true);
      }
    } else {
      expect((measured.switch as { height: number }).height).toBe(20);
      expect(boxes.find(box => box.label?.includes("Small"))!.height).toBe(24);
      expect((measured.close as { height: number }).height).toBe(26);
    }
    const checkTrack = async () => {
      const color = await toggle.locator(":scope > span").evaluate(e => getComputedStyle(e).backgroundColor);
      await page.evaluate(color => { const probe = document.getElementById("switch-track-probe") ?? document.createElement("span"); probe.id = "switch-track-probe"; probe.style.color = color; probe.style.backgroundColor = "white"; probe.textContent = "Track against white knob and surface"; document.body.append(probe); }, color);
      expect(await textContrast(page.locator("#switch-track-probe"))).toBeGreaterThanOrEqual(3);
    };
    await checkTrack();
    await toggle.focus(); await page.keyboard.press("Space");
    await expect(toggle).toHaveAttribute("aria-checked", "true");
    await expect.poll(() => toggle.locator(":scope > span").evaluate(e => getComputedStyle(e).backgroundColor)).not.toBe("rgb(100, 116, 139)");
    // Wait for the existing color transition to finish before measuring its endpoint.
    await toggle.locator(":scope > span").evaluate(async e => { await Promise.all(e.getAnimations().map(a => a.finished)); });
    await checkTrack();
    expect(writes).toEqual([{ is_expense_approver: true }]);
  });
}


test("domain status presentation and chart markers use readable semantic roles", async ({ page }, testInfo) => {
  await page.goto("/design/gallery");
  await expect(page.getByRole("button", { name: "Primary", exact: true })).toBeVisible();
  const maps = Object.entries(format).filter(([key]) => key.endsWith("_STYLES")) as [string, Record<string, string>][];
  await page.evaluate(maps => {
    const host = document.createElement("section");
    host.id = "semantic-contrast-probe";
    host.style.backgroundColor = "white";
    for (const [map, values] of maps) for (const [status, classes] of Object.entries(values)) {
      const span = document.createElement("span");
      span.className = classes;
      span.dataset.semantic = `${map}:${status}`;
      span.textContent = `${map}: ${status}`;
      host.append(span);
    }
    document.body.append(host);
  }, maps);
  const measurements: { domain: string; ratio: number }[] = [];
  for (const item of await page.locator("[data-semantic]").all()) {
    const ratio = await textContrast(item);
    const domain = (await item.getAttribute("data-semantic"))!;
    measurements.push({ domain, ratio });
    expect(ratio, domain).toBeGreaterThanOrEqual(4.5);
  }
  for (const [index, color] of format.CHART_PALETTE.entries()) {
    await page.evaluate(({ index, color }) => {
      const marker = document.createElement("span");
      marker.dataset.marker = String(index);
      marker.style.color = color;
      marker.textContent = "Chart marker";
      document.getElementById("semantic-contrast-probe")!.append(marker);
    }, { index, color });
    const ratio = await textContrast(page.locator(`[data-marker="${index}"]`));
    measurements.push({ domain: `chart marker ${index + 1} against white`, ratio });
    expect(ratio).toBeGreaterThanOrEqual(3);
  }
  await testInfo.attach("semantic-role-contrast", { body: JSON.stringify({ measurements, scope: "Rendered light-theme samples; domain labels and permissions remain separate. Disabled controls and decorative grid lines are not asserted as enabled information. This is not full WCAG or device qualification." }), contentType: "application/json" });
});
