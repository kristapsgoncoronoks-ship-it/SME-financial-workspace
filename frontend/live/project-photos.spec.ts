import { execFileSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

// Opt-in real API/PostgreSQL journey. Requires the local synthetic live harness,
// its psql environment and Pillow; it makes no provider or production claim.
test("30 real project photos use bounded JPEG previews and preserve the original bytes", async ({ page }, info) => {
  test.setTimeout(180_000);
  // Share the production auth limiter honestly with the preceding live journey.
  await new Promise(resolve => setTimeout(resolve, 30_500));
  await new Promise(resolve => setTimeout(resolve, 30_500));
  await page.setViewportSize({ width: 390, height: 844 });
  const sourcePath = info.outputPath("synthetic-source.jpg");
  execFileSync("python", ["-c", `
from PIL import Image
import random,sys
image=Image.frombytes('RGB',(1600,1200),random.Random(20261105).randbytes(1600*1200*3))
exif=Image.Exif();exif[306]='2026:11:05 09:00:00';exif[270]='Synthetic photo evidence fixture'
image.save(sys.argv[1],format='JPEG',quality=91,exif=exif)
`, sourcePath]);
  const source = readFileSync(sourcePath);
  const sha = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex");
  const sourceSha = sha(source);
  const email = `photo-live-${randomUUID()}@example.com`, password = "synthetic-photo-live-password";
  const registration = await page.request.post("/api/v1/auth/register", { data: { organization_name: "Synthetic photo live workspace", name: "Synthetic photo owner", email, password } });
  expect(registration.status()).toBe(201);
  const identity = await registration.json();
  const headers = { Authorization: `Bearer ${identity.token.access_token}` };
  const created = await page.request.post("/api/v1/masters/projects", { headers, data: { code: "PHOTO-LIVE", name: "Synthetic bounded photo evidence" } });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const rows: { id: string; filename: string }[] = [];
  for (let i = 0; i < 30; i++) {
    const filename = `Synthetic photo ${i}.jpg`;
    const upload = await page.request.post(`/api/v1/masters/projects/${project.id}/documents?kind=photo`, { headers, multipart: { file: { name: filename, mimeType: "image/jpeg", buffer: source } } });
    expect(upload.status()).toBe(201);
    rows.push(await upload.json());
  }
  const listed = await page.request.get(`/api/v1/masters/projects/${project.id}/documents`, { headers });
  expect(listed.status()).toBe(200);
  const first = (await listed.json())[0] as { id: string; filename: string };
  for (const value of [identity.organization.id, project.id]) expect(value).toMatch(/^[0-9a-f-]{36}$/i);
  const pgRows = JSON.parse(execFileSync("psql", ["-X", "-q", "-v", "ON_ERROR_STOP=1", "-At", "-c", `SET app.current_org = '${identity.organization.id}'; SELECT json_build_object('links',count(*),'source_hashes',json_agg(DISTINCT sha256)) FROM project_documents WHERE org_id='${identity.organization.id}' AND project_id='${project.id}';`], { encoding: "utf8", env: process.env }).trim());
  expect(pgRows.links).toBe(30); expect(pgRows.source_hashes).toEqual([sourceSha]);
  await page.addInitScript(() => {
    const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL), urls = new Set<string>();
    Object.assign(window, { livePhotoUrls: urls, livePhotoCounts: { created: 0, revoked: 0 } });
    URL.createObjectURL = blob => { const url = create(blob); urls.add(url); (window as unknown as { livePhotoCounts: { created: number } }).livePhotoCounts.created++; return url; };
    URL.revokeObjectURL = url => { urls.delete(url); (window as unknown as { livePhotoCounts: { revoked: number } }).livePhotoCounts.revoked++; revoke(url); };
  });
  const requests: { kind: string; path: string }[] = [];
  const bodies: Promise<{ kind: string; status: number; bytes: number; sha: string; contentType: string; cacheControl: string } | null>[] = [];
  let firstPreview: Buffer | undefined;
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (/\/documents\/[^/]+\/(thumbnail|download)$/.test(path)) requests.push({ kind: path.endsWith("/thumbnail") ? "thumbnail" : "download", path });
  });
  page.on("response", response => {
    const path = new URL(response.url()).pathname;
    if (!/\/documents\/[^/]+\/(thumbnail|download)$/.test(path)) return;
    bodies.push((async () => {
      try {
        const body = await response.body(), kind = path.endsWith("/thumbnail") ? "thumbnail" : "download";
        if (kind === "thumbnail" && response.status() === 200) firstPreview ??= body;
        return { kind, status: response.status(), bytes: body.length, sha: sha(body), contentType: response.headers()["content-type"], cacheControl: response.headers()["cache-control"] };
      } catch { return null; }
    })());
  });
  await page.goto(`/login?next=/projects/${project.id}`);
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("heading", { name: "PHOTO-LIVE · Synthetic bounded photo evidence", exact: true })).toBeVisible();
  await page.waitForLoadState("networkidle");
  const initialRequests = requests.slice();
  expect(initialRequests.filter(r => r.kind === "download")).toEqual([]);
  const tile = page.locator(`[data-photo-id="${first.id}"]`);
  await tile.scrollIntoViewIfNeeded();
  await expect(tile.getByRole("img", { name: first.filename, exact: true })).toBeVisible();
  await page.waitForLoadState("networkidle");
  const beforeOpen = requests.slice();
  expect(beforeOpen.filter(r => r.kind === "download")).toEqual([]);
  expect(beforeOpen.filter(r => r.kind === "thumbnail").length).toBeGreaterThan(0);
  expect(beforeOpen.filter(r => r.kind === "thumbnail").length).toBeLessThan(30);
  await tile.getByRole("button", { name: `Open original photo ${first.filename}`, exact: true }).focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: first.filename, exact: true });
  await expect(dialog.getByRole("img", { name: first.filename, exact: true })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(requests.filter(r => r.kind === "download")).toHaveLength(1);
  const download = page.waitForEvent("download");
  await dialog.getByRole("link", { name: "Download original", exact: true }).click();
  const downloaded = await download;
  expect(downloaded.suggestedFilename()).toBe(first.filename);
  expect(sha(readFileSync((await downloaded.path())!))).toBe(sourceSha);
  expect(requests.filter(r => r.kind === "download")).toHaveLength(1);
  await page.keyboard.press("Escape");
  await page.getByRole("link", { name: "All projects", exact: true }).click();
  await expect.poll(() => page.evaluate(() => (window as unknown as { livePhotoUrls: Set<string> }).livePhotoUrls.size)).toBe(0);
  const received = (await Promise.all(bodies)).filter((row): row is NonNullable<typeof row> => !!row);
  const previews = received.filter(row => row.kind === "thumbnail");
  expect(previews.length).toBeGreaterThan(0);
  for (const preview of previews) { expect(preview.status).toBe(200); expect(preview.contentType).toContain("image/jpeg"); expect(preview.bytes).toBeLessThanOrEqual(128 * 1024); expect(preview.cacheControl).toBe("private, no-store"); }
  expect(received.filter(row => row.kind === "download").map(row => row.sha)).toEqual([sourceSha]);
  const previewPath = info.outputPath("actual-thumbnail.jpg"); writeFileSync(previewPath, firstPreview!);
  const dimensions = JSON.parse(execFileSync("python", ["-c", "from PIL import Image;import json,sys;im=Image.open(sys.argv[1]);print(json.dumps({'format':im.format,'width':im.width,'height':im.height,'exif_fields':len(im.getexif())}))", previewPath], { encoding: "utf8" }));
  expect(dimensions.format).toBe("JPEG"); expect(Math.max(dimensions.width, dimensions.height)).toBeLessThanOrEqual(320); expect(dimensions.exif_fields).toBe(0);
  const evidence = { source: { bytes: source.length, sha: sourceSha, width: 1600, height: 1200, syntheticExif: true }, projectId: project.id, orgId: identity.organization.id, pgRows, initialRequests, beforeOpen, requests, received, dimensions, urls: await page.evaluate(() => (window as unknown as { livePhotoCounts: unknown }).livePhotoCounts), scope: "Actual local PostgreSQL/API/storage/Chromium, one synthetic30-photo390px journey; duplicate source bytes deliberately share a storage hash. No provider, physical-device, production, or complete accessibility claim." };
  writeFileSync(info.outputPath("live-photo-evidence.json"), JSON.stringify(evidence, null, 2));
  await info.attach("live-photo-evidence", { body: JSON.stringify(evidence), contentType: "application/json" });
});
