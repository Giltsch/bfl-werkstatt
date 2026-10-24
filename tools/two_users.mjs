// Two people in the real UI at once (phone „Anna“ + desktop „Ben“) against a running server: different fields merge,
// the same field opens the conflict dialog, presence, queue position with who is ahead, cancelling a colleague's job,
// double start → „läuft schon“. TEST projects only (purged), team members created here are removed again.
//   node tools/two_users.mjs --base http://127.0.0.1:7870
import {createRequire} from "node:module";
import {mkdirSync} from "node:fs";
import {join} from "node:path";

const SCRATCH = process.env.WS_SCRATCH || "/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad";
const CHROME = process.env.WS_CHROME || "/home/peter_behninger/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome";
const {chromium} = createRequire(join(SCRATCH, "pw") + "/")("playwright-core");
const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const BASE = arg("base", "http://127.0.0.1:7870"), SHOTS = arg("shots", join(SCRATCH, "shots-v3", "two"));
const PRIMARY = "#ws-actions .btn.primary";
mkdirSync(SHOTS, {recursive: true});
const problems = [];
const ok = (c, m) => { if (!c) { problems.push(m); console.log("  ✗", m); } else console.log("  ✓", m); };
const browser = await chromium.launch({executablePath: CHROME, headless: true});
const REAL = ["0db93df20e67", "bb505aeddc04"];

async function person(name, color, vp) {
  const ctx = await browser.newContext({viewport: vp, hasTouch: vp.width < 700, isMobile: vp.width < 700, deviceScaleFactor: vp.width < 700 ? 2 : 1, locale: "de-DE"});
  await ctx.route("**/*", r => (r.request().method() !== "GET" && REAL.some(p => r.request().url().includes(p))) ? r.abort() : r.fallback());
  const api = async (method, url, data) => {
    const r = await ctx.request.fetch(BASE + url, {method, data, headers: data ? {"content-type": "application/json"} : {}});
    return {status: r.status(), body: await r.json().catch(() => null)};
  };
  const team = (await api("GET", "/api/settings")).body.team || [];
  let m = team.find(t => t.name === name), made = false;
  if (!m) { m = (await api("POST", "/api/team", {name, color})).body; made = true; }
  await api("POST", "/api/me", {id: m.id});
  const page = await ctx.newPage();
  page.on("pageerror", e => ok(false, `${name}: page error ${e.message}`));
  page.on("dialog", d => { ok(false, `${name}: native dialog ${d.message()}`); d.dismiss().catch(() => {}); });
  return {ctx, page, api, member: m, made, name};
}
const shot = (p, n) => p.page.screenshot({path: join(SHOTS, `${n}.png`)});
const dialogText = p => p.page.locator(".dialog, .sheet").last().textContent({timeout: 8000}).catch(() => "");
async function newProject(p, title) {
  const d = (await p.api("POST", "/api/projects/workshop", {title, customer: `Familie ${title.slice(5)}`})).body;
  const pc = d.pieces[0].id;
  await p.api("PATCH", `/api/projects/workshop/${d.id}`, {base_rev: d.rev, ops: [
    {op: "set", path: `pieces.${pc}.type`, value: "sideboard"}, {op: "set", path: `pieces.${pc}.w`, value: 180},
    {op: "set", path: `pieces.${pc}.h`, value: 75}, {op: "set", path: `pieces.${pc}.d`, value: 45},
    {op: "set", path: "setting", value: "heller Raum"}, {op: "set", path: "count", value: 2}]});
  return {pid: d.id, pc};
}

const anna = await person("Anna", "#e879f9", {width: 390, height: 844});
const ben = await person("Ben", "#7cc4a0", {width: 1440, height: 900});
const P1 = await newProject(anna, "TEST zwei Personen 1"), P2 = await newProject(ben, "TEST zwei Personen 2");
const doc = async pid => (await anna.api("GET", `/api/projects/workshop/${pid}`)).body;
try {
  // both open project 1 in step ② Möbel
  for (const p of [anna, ben]) { await p.page.goto(`${BASE}/werkstatt#/p/${P1.pid}/moebel`); await p.page.waitForSelector(".mb-dims input", {timeout: 15000}); }
  await ben.page.waitForTimeout(4500);   // ≥ one sync round for presence
  const pres = await ben.page.locator("body").innerText();
  ok(/Anna \(Handy\) ist gerade in Möbel/.test(pres), "Ben sees „Anna (Handy) ist gerade in Möbel“");
  await shot(ben, "01-ben-presence");

  // different fields: Anna Breite, Ben the title (Kunde & Auftrag) → both persist
  const breiteA = anna.page.locator(".mb-dims input").first();
  await breiteA.fill("200"); await breiteA.blur();
  await ben.page.goto(`${BASE}/werkstatt#/p/${P1.pid}/auftrag`);
  const titleB = ben.page.locator("label.field", {hasText: "Titel"}).locator("input").first();
  await titleB.waitFor({timeout: 10000});
  await titleB.fill("TEST zwei Personen 1 (Ben)"); await titleB.blur();
  await anna.page.waitForTimeout(2500);
  let d = await doc(P1.pid);
  ok(d.pieces[0].w === 200 && d.title === "TEST zwei Personen 1 (Ben)", `different fields both kept (w=${d.pieces[0].w}, title=${d.title})`);

  // same field: Ben's sync is held back, both type Breite → Ben gets the conflict dialog
  await ben.page.goto(`${BASE}/werkstatt#/p/${P1.pid}/moebel`);
  await ben.page.waitForSelector(".mb-dims input");
  await ben.page.waitForTimeout(3500);
  let hold = true;
  await ben.ctx.route("**/api/sync**", async r => { while (hold) await new Promise(res => setTimeout(res, 200)); return r.fallback(); });
  await breiteA.fill("230"); await breiteA.blur();
  await anna.page.waitForTimeout(1500);
  const breiteB = ben.page.locator(".mb-dims input").first();
  await breiteB.fill("250"); await breiteB.blur();
  await ben.page.waitForSelector(".dialog", {timeout: 10000}).catch(() => {});
  const ct = await dialogText(ben);
  ok(/Anna hat .*Breite.* gerade auf 230 cm geändert/.test(ct) && /Sie hatten 250 cm eingetragen/.test(ct), `conflict dialog: „${ct.replace(/\s+/g, " ").slice(0, 160)}“`);
  await shot(ben, "02-ben-conflict");
  await ben.page.locator(".dialog button", {hasText: "Meins übernehmen"}).click().catch(() => ok(false, "no „Meins übernehmen“"));
  hold = false;
  await ben.ctx.unroute("**/api/sync**");
  await ben.page.waitForTimeout(2000);
  d = await doc(P1.pid);
  ok(d.pieces[0].w === 250, `„Meins übernehmen“ saved Ben's value (w=${d.pieces[0].w})`);
  await anna.page.waitForTimeout(4000);
  ok(await breiteA.inputValue() === "250", `Anna's field follows (shows ${await breiteA.inputValue()})`);

  // queue: Anna starts project 1, Ben project 2 → Ben sees his place and who is ahead
  await anna.page.goto(`${BASE}/werkstatt#/p/${P1.pid}/berechnen`);
  await ben.page.goto(`${BASE}/werkstatt#/p/${P2.pid}/berechnen`);
  for (const p of [anna, ben]) await p.page.waitForFunction(s => /Bilder berechnen/.test(document.querySelector(s)?.textContent || ""), PRIMARY, {timeout: 15000});
  await anna.page.locator(PRIMARY).click();
  await anna.page.waitForSelector(".er", {timeout: 15000});
  for (let i = 0; i < 60; i++) {   // until Anna's job renders images (its caption is done)
    const qq = (await ben.api("GET", "/api/queue")).body;
    if ((qq.jobs || []).some(j => j.owner === anna.member.id && j.phase === "image")) break;
    await ben.page.waitForTimeout(500);
  }
  await ben.page.waitForTimeout(3500);
  const ql = await ben.page.locator(".bc-queue").textContent().catch(() => "");
  ok(/Vor Ihnen: 1 Auftrag \(Anna – TEST zwei Personen 1/.test(ql), `Ben's queue line before starting: „${ql}“`);
  await shot(ben, "03-ben-queue-line");
  await ben.page.locator(PRIMARY).click();
  await ben.page.waitForSelector(".er", {timeout: 15000});
  await ben.page.waitForTimeout(3500);
  const prog = await ben.page.locator(".er-prog, .er").first().innerText();
  ok(/Platz 2/.test(prog) && /vor Ihnen: Anna – TEST zwei Personen 1/.test(prog), `Ben's progress: „${prog.split("\n").slice(0, 3).join(" | ")}“`);
  await shot(ben, "04-ben-waiting");
  await shot(anna, "05-anna-running");
  const q = (await ben.api("GET", "/api/queue")).body;
  const mine = q.jobs.find(j => j.owner === ben.member.id) || {};
  ok(mine.position >= 1 && (mine.ahead || []).some(a => a.owner_name === "Anna"), `Ben's job: position ${mine.position}, ahead ${JSON.stringify(mine.ahead)}, „${mine.stage_text}“`);

  // Ben cancels Anna's job from her project → asked first
  await ben.page.goto(`${BASE}/werkstatt#/p/${P1.pid}/berechnen`);
  await ben.page.waitForSelector(".bc-run:not([hidden]) button", {timeout: 15000}).catch(() => {});
  await ben.page.locator(".bc-run button", {hasText: "Abbrechen"}).first().click().catch(() => ok(false, "no Abbrechen button on Anna's running job"));
  const cd = await dialogText(ben);
  ok(/Dieser Auftrag gehört Anna\. Trotzdem abbrechen\?/.test(cd), `cancel dialog: „${cd.replace(/\s+/g, " ").slice(0, 120)}“`);
  await shot(ben, "06-ben-cancel-anna");
  await ben.page.locator(".dialog button", {hasText: /Nicht abbrechen|Abbrechen$|Zurück|Nein/}).first().click().catch(() => ben.page.keyboard.press("Escape"));

  // double start on project 2 within a second → one job, the second device gets „läuft schon“
  for (const p of [anna, ben]) await p.api("GET", "/api/queue");
  const wait = async () => { for (let i = 0; i < 300; i++) { const qq = (await anna.api("GET", "/api/queue")).body; if (!qq.jobs.length) return; await anna.page.waitForTimeout(1000); } };
  await wait();
  for (const p of [anna, ben]) { await p.page.goto(`${BASE}/werkstatt#/p/${P2.pid}/berechnen`); await p.page.waitForFunction(s => /Bilder berechnen/.test(document.querySelector(s)?.textContent || ""), PRIMARY, {timeout: 15000}); }
  await Promise.all([anna.page.locator(PRIMARY).click(), ben.page.locator(PRIMARY).click()]);
  await Promise.all([anna, ben].map(p => p.page.waitForSelector(".dialog, .er", {timeout: 15000}).catch(() => {})));
  const texts = await Promise.all([anna, ben].map(async p => (await p.page.locator(".dialog").count()) ? dialogText(p) : ""));
  const who = texts.findIndex(t => /läuft schon eine Berechnung/.test(t));
  ok(who >= 0, `double start: one device got „${(texts[who] || texts.join(" / ")).replace(/\s+/g, " ").slice(0, 120)}“`);
  if (who >= 0) await shot([anna, ben][who], "07-laeuft-schon");
  const pj = (await anna.api("GET", `/api/sync?project=${P2.pid}&rev=0`)).body.queue.project_jobs.filter(j => !["done", "error", "cancelled"].includes(j.state));
  ok(pj.length === 1, `one active job for project 2 (${pj.length})`);
  await wait();
} catch (e) {
  ok(false, `walk crashed: ${e.stack || e}`);
}
for (const {pid} of [P1, P2]) {
  const d = await doc(pid);
  const r = await anna.api("POST", `/api/workshop/${pid}/purge`, {confirm: d.title || "LÖSCHEN"});
  ok(r.status === 200, `purged ${pid}`);
}
for (const p of [anna, ben]) if (p.made) await p.api("DELETE", `/api/team/${p.member.id}`);
await browser.close();
console.log(problems.length ? `${problems.length} problem(s)` : "two users: all checks passed");
process.exit(problems.length ? 1 : 0);
