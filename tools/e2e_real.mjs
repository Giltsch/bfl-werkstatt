// End-to-end walk of the real Werkstatt UI against a running server (dry run with --fake-gpu, or the live GPU server):
// new order with photo → analysis → 2 pieces → placement → Berechnen → choose → send a version → customer link in a
// fresh phone context → approve → dashboard shows „Freigegeben“ → Kundenblatt PDF → purge. Every non-GET request
// to a project not created by this run is aborted (real orders stay untouched).
//   node tools/e2e_real.mjs --base http://127.0.0.1:7870 [--public http://127.0.0.1:7871] [--vp phone|desktop]
//        [--count 2] [--quality entwurf] [--room path.png] [--keep] [--name Anna]
import {createRequire} from "node:module";
import {mkdirSync, writeFileSync, copyFileSync} from "node:fs";
import {join} from "node:path";

const SCRATCH = process.env.WS_SCRATCH || "/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad";
const CHROME = process.env.WS_CHROME || "/home/peter_behninger/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome";
const {chromium} = createRequire(join(SCRATCH, "pw") + "/")("playwright-core");
const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const flag = k => process.argv.includes(`--${k}`);
const BASE = arg("base", "http://127.0.0.1:7870"), PUBLIC = arg("public", BASE);
const VP = arg("vp", "phone"), COUNT = Number(arg("count", 2)), QUALITY = arg("quality", "entwurf");
const NAME = arg("name", "Anna"), SHOTS = arg("shots", join(SCRATCH, "shots-v3", "e2e"));
const ROOM = arg("room", join(SCRATCH, "ux-audit", "room.png"));
const VPS = {phone: {width: 390, height: 844, touch: true}, desktop: {width: 1440, height: 900, touch: false}};
const PRIMARY = "#ws-actions .btn.primary";
const T = {};       // timings (s)
const problems = [];
const fail = m => { problems.push(m); console.log("  ✗", m); };
const ok = (c, m) => { if (!c) fail(m); else console.log("  ✓", m); };
mkdirSync(SHOTS, {recursive: true});
const room = join(SCRATCH, `e2e-room-${process.pid}.png`);
copyFileSync(ROOM, room);

const browser = await chromium.launch({executablePath: CHROME, headless: true});
const mine = new Set();
async function context(vp, guard = true) {
  const v = VPS[vp];
  const ctx = await browser.newContext({viewport: {width: v.width, height: v.height}, hasTouch: v.touch, isMobile: v.touch,
    deviceScaleFactor: v.touch ? 2 : 1, locale: "de-DE", timezoneId: "Europe/Berlin"});
  if (guard) await ctx.route(/\/api\/projects\/workshop$/, async route => {   // learn the ids this run creates
    if (route.request().method() !== "POST") return route.fallback();
    const res = await route.fetch();
    const body = await res.text();
    try { mine.add(JSON.parse(body).id); } catch { /* error reply */ }
    return route.fulfill({response: res, body});
  });
  if (guard) await ctx.route(/\/api\/(projects\/workshop|workshop)\/([a-z0-9]{12})(\/|$|\?)/, route => {
    const pid = route.request().url().match(/\/api\/(?:projects\/workshop|workshop)\/([a-z0-9]{12})/)[1];
    if (route.request().method() !== "GET" && !mine.has(pid)) { fail(`guard: blocked ${route.request().method()} ${route.request().url()}`); return route.abort(); }
    return route.fallback();
  });
  return ctx;
}
function watch(page, tag) {
  page.on("pageerror", e => fail(`${tag}: page error ${e.message}`));
  page.on("console", m => { if (["error", "warning"].includes(m.type())) console.log(`  · ${tag} console.${m.type()}: ${m.text().slice(0, 300)}`); });
  page.on("request", r => { if (/generate|\/edit/.test(r.url())) console.log(`  · ${tag}: ${r.method()} ${r.url()} ${(r.postData() || "").slice(0, 200)}`); });
  page.on("dialog", d => { fail(`${tag}: native dialog ${d.message()}`); d.dismiss().catch(() => {}); });
  page.on("response", async r => {
    if (r.status() >= 500) fail(`${tag}: HTTP ${r.status()} ${r.request().method()} ${r.url()}`);
    else if (r.status() >= 400 && r.request().method() !== "GET") console.log(`  · ${tag}: HTTP ${r.status()} ${r.request().method()} ${r.url()} ${(await r.text().catch(() => "")).slice(0, 300)}`);
  });
}
async function shot(page, name) {
  const t = await page.evaluate(() => document.body.innerText).catch(() => "");
  const bad = t.match(/(^|\n)\s*(null|undefined)\s*(\n|$)|\bNaN\b|\[object Object\]/);
  if (bad) fail(`${name}: stray „${bad[0].trim()}“ on the page`);
  return page.screenshot({path: join(SHOTS, `${VP}-${name}.png`)});
}
const primaryText = page => page.locator(PRIMARY).first().textContent({timeout: 5000}).then(t => t.trim()).catch(() => "");
async function waitText(page, re, ms, what) {
  try { await page.waitForFunction(([s, f]) => new RegExp(s, f).test(document.body.innerText), [re.source, re.flags], {timeout: ms}); return true; }
  catch { fail(`timeout (${ms / 1000} s): ${what}`); return false; }
}
async function identity(page, name) {
  const sheet = page.locator(".sheet, .dialog").filter({hasText: /Wie heißen Sie|Wer sind Sie|Wer arbeitet/});
  try { await sheet.first().waitFor({timeout: 5000}); } catch { return; }
  const known = sheet.locator("button", {hasText: new RegExp(`^\\s*\\S*\\s*${name}\\s*$`)});
  if (await known.count()) { await known.first().click(); }
  else {
    const add = sheet.locator("button", {hasText: /Neue Person|Ich bin neu|Andere Person/});
    if (await add.count()) await add.first().click();
    await page.locator(".sheet input, .dialog input").first().fill(name);
    await page.locator(".sheet button, .dialog button", {hasText: /^Weiter$|Speichern/}).first().click();
  }
  await page.waitForTimeout(600);
}
const api = (ctx, method, url, data) => ctx.request.fetch(BASE + url, {method, data, headers: data ? {"content-type": "application/json"} : {}})
  .then(async r => ({status: r.status(), body: await r.json().catch(() => null)}));

const ctx = await context(VP);
const page = await ctx.newPage();
watch(page, VP);
let pid = null;
try {
  await page.goto(BASE + "/werkstatt");
  await identity(page, NAME);
  await page.waitForSelector(".order, .empty", {timeout: 15000});
  await shot(page, "01-dashboard");

  // new order: customer + title, camera first
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".new-order");
  const inputs = page.locator(".new-order input");
  await inputs.nth(0).fill("Familie E2E");
  await inputs.nth(1).fill(`TEST e2e ${VP}`);
  await inputs.nth(2).fill("0981 4711");
  await inputs.nth(3).fill("Ansbach");
  await shot(page, "02-neu");
  const chooser = page.waitForEvent("filechooser");
  await page.locator(".new-order .btn.primary").click();
  await (await chooser).setFiles(room);
  await page.waitForFunction(() => /^#\/p\/[a-z0-9]+\/raum$/.test(location.hash), null, {timeout: 20000});
  pid = (await page.evaluate(() => location.hash)).split("/")[2];
  mine.add(pid);
  console.log("project", pid);
  let t0 = Date.now();
  await waitText(page, /Raum erkannt|konnte nicht vermessen/, 180000, "room analysis");
  T.analysis_s = (Date.now() - t0) / 1000;
  await shot(page, "03-raum");
  if (await page.locator(".rm-cal input").count()) {
    await page.locator(".rm-cal input").fill("250");
    await page.locator("button", {hasText: "Übernehmen"}).first().click();
    await page.waitForTimeout(800);
  }

  // ② two pieces
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".mb .tile");
  await page.locator(".mb .tile", {hasText: "Sideboard"}).first().click();
  await page.waitForTimeout(300);
  await page.locator(".mb .chip", {hasText: "Nussbaum"}).first().click();
  await page.waitForTimeout(500);
  await page.locator("button", {hasText: "＋ Weiteres Möbel"}).click();
  await page.waitForTimeout(600);
  await page.locator(".mb-card.open .tile", {hasText: "Regal"}).first().click();
  await page.waitForTimeout(300);
  await page.locator(".mb-card.open .chip", {hasText: /Eiche/}).first().click();
  await page.waitForTimeout(600);
  await shot(page, "04-moebel");
  let doc = (await api(ctx, "GET", `/api/projects/workshop/${pid}`)).body;
  ok(doc.pieces.length === 2 && doc.pieces.every(p => p.type), `two typed pieces saved (${doc.pieces.map(p => p.type)})`);

  // ③ place both: tap the floor
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".pl-canvas canvas");
  await page.waitForTimeout(800);
  await page.waitForFunction(() => !document.querySelector(".toast"), null, {timeout: 12000}).catch(() => {});
  const cb = await page.locator(".pl-canvas canvas").first().boundingBox();
  const chips = page.locator(".pl-chips .chip");
  for (const [i, x, y] of [[0, 0.35, 0.82], [1, 0.85, 0.95]]) {
    if (await chips.count() > i) await chips.nth(i).click();
    await page.waitForTimeout(300);
    await page.mouse.click(cb.x + cb.width * x, cb.y + cb.height * y);
    await page.waitForTimeout(800);
  }
  await shot(page, "05-platzieren");
  doc = (await api(ctx, "GET", `/api/projects/workshop/${pid}`)).body;
  ok(doc.pieces.every(p => p.placement && p.placement.mode !== "none"), `both pieces placed (${doc.pieces.map(p => p.placement.mode)})`);

  // ④ Berechnen
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".bc");
  await page.locator(".bc-count button").nth(COUNT - 1).click();
  await page.locator(".bc-q button").nth(QUALITY === "fein" ? 1 : 0).click();
  await page.waitForFunction(s => /Bilder berechnen \(ca\. \d+ Min\.\)/.test(document.querySelector(s)?.textContent || ""), PRIMARY, {timeout: 15000}).catch(() => fail("no estimate on the primary"));
  T.estimate_label = await primaryText(page);
  await shot(page, "06-berechnen");
  t0 = Date.now();
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".er", {timeout: 20000});
  await shot(page, "07-ergebnis-laeuft");
  // wait for the images (and verdicts) via the sync payload
  let firstImage = null, lastJob = null;
  for (;;) {
    const s = (await api(ctx, "GET", `/api/sync?project=${pid}&rev=0`)).body;
    const pj = (s.queue && s.queue.project_jobs) || [];
    lastJob = pj[0] || lastJob;
    const d = s.project && s.project.doc;
    if (!firstImage && d && d.variants && d.variants.length) firstImage = (Date.now() - t0) / 1000;
    if (pj.length && pj.every(j => ["done", "error", "cancelled"].includes(j.state)) && pj.every(j => !["pending", "running"].includes(j.judge_state))) break;
    if (Date.now() - t0 > 900000) { fail("job did not finish within 15 min"); break; }
    await page.waitForTimeout(2000);
  }
  T.first_image_s = firstImage;
  T.job_total_s = (Date.now() - t0) / 1000;
  T.job = lastJob && {state: lastJob.state, caption_backend: lastJob.caption_backend, judge_state: lastJob.judge_state, steps: lastJob.steps};
  await page.waitForTimeout(3500);
  await shot(page, "08-ergebnis");
  doc = (await api(ctx, "GET", `/api/projects/workshop/${pid}`)).body;
  ok(doc.variants.length >= COUNT, `${doc.variants.length} proposals recorded`);
  console.log("verdicts", doc.variants.map(v => v.judge && `${v.judge.status}:${v.judge.score ?? ""}`).join(", "));

  // ⑤ choose the first proposal for the customer
  await page.locator(".er-props .er-tile").first().click();
  const star = page.locator(".er-panel button", {hasText: "★ Für den Kunden auswählen"});
  if (await star.count()) await star.first().click();
  await page.waitForFunction(s => !document.querySelector(s)?.disabled, PRIMARY, {timeout: 8000}).catch(() => fail("send stays disabled after ★"));
  await shot(page, "09-ausgewaehlt");
  // Kundenblatt (PDF) draft through the API the button uses
  const sheet = await api(ctx, "POST", `/api/workshop/${pid}/sheet`, {});
  ok(sheet.status === 200 && /\.pdf$/.test(sheet.body.pdf || ""), `draft Kundenblatt ${sheet.body && sheet.body.pdf}`);
  if (sheet.status === 200) writeFileSync(join(SHOTS, `${VP}-kundenblatt-entwurf.pdf`), await (await ctx.request.get(BASE + sheet.body.pdf)).body());

  // send a version
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".sheet .sd-tiles", {timeout: 8000});
  await shot(page, "10-senden");
  await page.locator(".sheet .sheet-actions button, .dialog .dialog-actions button", {hasText: /Version \d+ erstellen/}).last().click();
  const warn = page.locator(".dialog .dialog-actions button", {hasText: "Trotzdem senden"});
  try { await warn.waitFor({timeout: 4000}); await shot(page, "10b-warnung"); await warn.click(); } catch { /* no warning */ }
  await page.waitForSelector(".sheet h2:text-matches('Version \\\\d+ ist bereit')", {timeout: 60000}).catch(() => fail("version result sheet missing"));
  await shot(page, "11-versand");
  doc = (await api(ctx, "GET", `/api/projects/workshop/${pid}`)).body;
  const ver = doc.versions[doc.versions.length - 1];
  ok(ver && ver.token && doc.status === "kunde", `version ${ver && ver.n} created, status ${doc.status}`);
  const pdf = await ctx.request.get(`${BASE}/api/workshop/${pid}/versions/${ver.n}/pdf`);
  ok(pdf.status() === 200 && (await pdf.body()).subarray(0, 4).toString() === "%PDF", "version PDF downloads");
  writeFileSync(join(SHOTS, `${VP}-kundenblatt-v${ver.n}.pdf`), await pdf.body());
  await page.keyboard.press("Escape");

  // the customer opens the link in a fresh phone context (no cookies, no team)
  const kctx = await context("phone", false);
  const k = await kctx.newPage();
  watch(k, "kunde");
  await k.goto(`${PUBLIC}/k/${ver.token}`);
  await k.waitForSelector(".card .ba", {timeout: 15000});
  await k.waitForTimeout(800);
  await k.screenshot({path: join(SHOTS, `${VP}-12-kunde.png`)});
  await k.screenshot({path: join(SHOTS, `${VP}-12b-kunde-full.png`), fullPage: true});
  const sl = await k.locator(".ba").first().boundingBox();
  if (sl) { await k.mouse.click(sl.x + sl.width * 0.25, sl.y + sl.height / 2); await k.waitForTimeout(300); }
  if (await k.locator(".choice").count()) await k.locator(".choice").first().click();
  await k.fill("#kname", "Frau E2E");
  await k.locator(".btn.primary", {hasText: "Entwurf freigeben"}).click();
  await k.locator(".btn", {hasText: "Ja, senden"}).click();
  await k.waitForSelector(".done", {timeout: 10000});
  await k.screenshot({path: join(SHOTS, `${VP}-13-kunde-danke.png`)});
  await kctx.close();
  doc = (await api(ctx, "GET", `/api/projects/workshop/${pid}`)).body;
  ok(doc.status === "freigegeben" && doc.approved && doc.approved.via === "link", `customer approval → status ${doc.status}`);

  // dashboard shows „Freigegeben“
  await page.goto(BASE + "/werkstatt#/");
  await page.waitForSelector(".order", {timeout: 10000});
  await page.locator(".dash-opts .chip", {hasText: "Test-Projekte"}).click().catch(() => {});
  await page.waitForTimeout(1500);
  await shot(page, "14-dashboard-freigegeben");
  const card = page.locator(".order", {hasText: `TEST e2e ${VP}`});
  ok(/Freigegeben/.test(await card.first().textContent().catch(() => "")), "dashboard card shows „Freigegeben“");
  await card.first().click().catch(() => {});
  await page.waitForTimeout(1500);
  await shot(page, "15-projekt-nach-freigabe");
  await page.goto(`${BASE}/werkstatt#/p/${pid}/auftrag`);
  await page.waitForTimeout(2000);
  await shot(page, "16-kunde-auftrag");
} catch (e) {
  fail(`walk crashed: ${e.stack || e}`);
  await shot(page, "zz-crash").catch(() => {});
}
if (pid && !flag("keep")) {
  const doc = (await api(ctx, "GET", `/api/projects/workshop/${pid}`)).body;
  const r = await api(ctx, "POST", `/api/workshop/${pid}/purge`, {confirm: (doc && doc.title) || "LÖSCHEN"});
  ok(r.status === 200, `purged ${pid} (${r.body && r.body.files} files)`);
}
await browser.close();
console.log("TIMINGS " + JSON.stringify(T));
console.log(problems.length ? `${problems.length} problem(s)` : "e2e: all checks passed");
process.exit(problems.length ? 1 : 0);
