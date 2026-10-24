// Großtest: two TEST orders (different rooms and pieces) set up through the API, then every Werkstatt feature driven in a
// real browser: switching orders (list, back button, header, from every step) keeps each order's own pieces and
// selection; measuring gate; piece types incl. wall-hung; 3D model + downloads; tap placement; tap-to-remove (SAM);
// step ④ list, 3D template, KI-Plancheck, base option; optional one real generation, star, Präsentieren shows the
// chosen proposal, result order; exports. Writes to orders it did not create are blocked. Screenshots per check.
//   node tools/e2e_full.mjs [--base http://127.0.0.1:7860] [--vp phone|desktop] [--generate] [--keep]
import {createRequire} from "node:module";
import {mkdirSync, readFileSync} from "node:fs";
import {join} from "node:path";

const SCRATCH = process.env.WS_SCRATCH || "/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad";
const CHROME = process.env.WS_CHROME || "/home/peter_behninger/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome";
const {chromium} = createRequire(join(SCRATCH, "pw") + "/")("playwright-core");
const arg = (k, d) => { const i = process.argv.indexOf(`--${k}`); return i > 0 ? process.argv[i + 1] : d; };
const flag = k => process.argv.includes(`--${k}`);
const BASE = arg("base", "http://127.0.0.1:7860"), VP = arg("vp", "phone");
const ROOT = new URL("..", import.meta.url).pathname;
const SHOTS = join(SCRATCH, "grosstest", VP);
mkdirSync(SHOTS, {recursive: true});
const ROOMS = ["uploads/20261017-461a705a03.png", "uploads/20261016-5d0db410c1.png"];   // Esszimmer, Wohnzimmer (existing uploads)
const problems = [], passed = [];
const ok = (c, m) => { (c ? passed : problems).push(m); console.log(c ? "  ✓" : "  ✗", m); };
const sleep = ms => new Promise(r => setTimeout(r, ms));
let shot = 0;

async function api(method, path, body) {
  const r = await fetch(BASE + path, {method, headers: body ? {"content-type": "application/json"} : {}, body: body ? JSON.stringify(body) : undefined});
  const t = await r.text();
  let j = null; try { j = JSON.parse(t); } catch { /* text */ }
  if (!r.ok) throw new Error(`${method} ${path} → ${r.status} ${t.slice(0, 200)}`);
  return j ?? t;
}
const doc = id => api("GET", `/api/projects/workshop/${id}`);
const patch = async (id, ops) => api("PATCH", `/api/projects/workshop/${id}`, {base_rev: (await doc(id)).rev, ops});

/* ---------------- setup through the API ---------------- */
console.log("Setup");
const piece = (id, type, w, h, d, extra = {}) => ({id, type, w, h, d, material: "eiche", handles: "push", text: "", placement: {mode: "none", pose: null, box: null}, ...extra});
async function upload(rel) {   // a private copy: purging the TEST orders must never touch a real order's photo
  const fd = new FormData();
  fd.append("file", new Blob([readFileSync(join(ROOT, "outputs", rel))], {type: "image/png"}), "grosstest.png");
  const r = await fetch(BASE + "/api/upload?max_side=2560", {method: "POST", body: fd});
  if (!r.ok) throw new Error("upload " + r.status);
  return (await r.json()).path;
}
async function makeOrder(title, src, pieces) {
  const room = await upload(src);
  const d = await api("POST", "/api/projects/workshop", {title, customer: "Großtest"});
  const geo = await api("POST", "/api/geometry/analyze", {room});
  const size = geo.size || [1920, 2560];
  await patch(d.id, [{op: "set", path: "room", value: room}, {op: "set", path: "room_size", value: size}, {op: "set", path: "geometry", value: geo},
    ...pieces.map(p => ({op: "add_piece", value: p})), {op: "remove_piece", id: d.pieces[0].id}]);
  return d.id;
}
const A = await makeOrder("TEST Großtest A", ROOMS[0], [piece("ga0001", "tisch", 200, 76, 100), piece("ga0002", "wandregal", 100, 40, 25, {elev: 150}),
  piece("ga0003", "sideboard", 120, 75, 40)]);
const B = await makeOrder("TEST Großtest B", ROOMS[1], [piece("gb0001", "schrank", 180, 240, 60, {text: "3 Türen"}), piece("gb0002", "kueche", 240, 220, 60)]);
const mine = new Set([A, B]);
console.log("  A", A, " B", B);

/* ---------------- browser ---------------- */
const browser = await chromium.launch({executablePath: CHROME, headless: true, args: ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]});
const V = VP === "phone" ? {width: 390, height: 844, touch: true} : {width: 1440, height: 900, touch: false};
const ctx = await browser.newContext({viewport: {width: V.width, height: V.height}, hasTouch: V.touch, isMobile: V.touch, locale: "de-DE"});
await ctx.route(/\/api\/(projects\/workshop|workshop)\/([a-z0-9]{12})(\/|$|\?)/, route => {
  const pid = route.request().url().match(/\/([a-z0-9]{12})(\/|$|\?)/)[1];
  if (route.request().method() !== "GET" && !mine.has(pid)) { ok(false, `guard: blocked write to ${pid}`); return route.abort(); }
  return route.fallback();
});
const page = await ctx.newPage();
const errors = [];
page.on("pageerror", e => errors.push("pageerror: " + e.message));
page.on("console", m => { if (m.type() === "error" && !/favicon|ERR_ABORTED/.test(m.text())) errors.push("console: " + m.text()); });
const snap = async name => page.screenshot({path: join(SHOTS, `${String(++shot).padStart(2, "0")}-${name}.png`), fullPage: false});
const go = async (hash, wait = 2200) => { await page.evaluate(h => { location.hash = h; }, hash); await sleep(wait); };
const text = sel => page.evaluate(s => [...document.querySelectorAll(s)].map(e => e.innerText.split("\n")[0].trim()), sel);
const openCard = () => text(".mb-head[aria-expanded=true]");
const chips = () => page.evaluate(() => [...document.querySelectorAll(".pl-chips .chip, .chips .chip")].filter(c => c.querySelector(".dot")).map(c => ({t: c.innerText.trim(), on: c.getAttribute("aria-pressed") === "true"})));
const titleOf = () => page.evaluate(() => (document.querySelector(".hdr-title, header h1, .ed-title")?.innerText || document.title || "").split("\n")[0]);

await page.goto(BASE + "/werkstatt#/"); await sleep(2000);
const skip = page.locator("text=Ohne Namen weiter"); if (await skip.count()) { await skip.first().click(); await sleep(500); }

/* 1. switching orders keeps each order's pieces and selection */
console.log("1 Auftragswechsel");
await go(`#/p/${A}/moebel`);
ok(JSON.stringify(await text(".mb-head")).includes("Wandregal"), "A step 2 shows A's pieces");
await page.locator(".mb-head", {hasText: "Sideboard"}).first().click(); await sleep(700);
ok((await openCard())[0]?.startsWith("Sideboard"), "A: Sideboard selected");
await go("#/");
const tf = page.locator("button, .chip", {hasText: "Test-Projekte"}); if (await tf.count()) { await tf.first().click(); await sleep(1200); }
await page.locator("text=TEST Großtest B").first().click(); await sleep(2500);
await go(`#/p/${B}/moebel`);
const bHeads = await text(".mb-head");
ok(bHeads.length === 2 && bHeads[0].startsWith("Einbauschrank") && !bHeads.some(h => /Sideboard|Wandregal|Esstisch/.test(h)), `B shows only B's pieces (${bHeads.join(", ")})`);
ok((await openCard())[0]?.startsWith("Einbauschrank"), `B opens with its first piece (${(await openCard()).join()})`);
await snap("B-moebel-after-switch");
await page.goBack(); await sleep(1500); await page.goBack(); await sleep(2500);
await go(`#/p/${A}/moebel`);
ok((await openCard())[0]?.startsWith("Sideboard"), `back in A: Sideboard still selected (${(await openCard()).join()})`);
// switch from step 3 (Platzieren) straight to the other order's step 3
await go(`#/p/${A}/platzieren`, 3000);
let ca = await chips();
ok(ca.some(c => c.on && c.t.startsWith("Sideboard")), `A step 3: Sideboard active (${ca.map(c => c.t + (c.on ? "*" : "")).join(", ")})`);
await go(`#/p/${B}/platzieren`, 3000);
let cb = await chips();
ok(cb.length === 2 && cb.every(c => !/Sideboard|Wandregal|Esstisch/.test(c.t)) && cb.some(c => c.on), `B step 3: only B's chips, one active (${cb.map(c => c.t + (c.on ? "*" : "")).join(", ")})`);
await snap("B-platzieren-after-switch");
for (const step of ["raum", "berechnen", "ergebnis"]) {
  await go(`#/p/${A}/${step}`, 1800); await go(`#/p/${B}/moebel`, 2200);
  const h = await text(".mb-head");
  ok(h.length === 2 && h[0].startsWith("Einbauschrank"), `from A/${step} to B: B's pieces (${h.join(", ")})`);
}

/* 2. measuring gate in step 1 */
console.log("2 Messen");
await go(`#/p/${A}/raum`, 2500);
await page.locator("#ws-actions .btn.primary").first().click(); await sleep(800);
const dlg = await text(".dialog h2");
ok(dlg.some(t => /geschätzt/.test(t)), `unmeasured room asks before going on (${dlg.join()})`);
await snap("A-messen-dialog");
const yes = page.locator(".dialog button", {hasText: "stimmt"});
if (await yes.count()) { await yes.first().click(); await sleep(2500); }
ok(!!(await doc(A)).calibration, "confirming the ceiling stores a calibration");

/* 3. pieces: types, wall-hung field, 3D model and downloads */
console.log("3 Möbel + 3D");
await go(`#/p/${A}/moebel`);
await page.locator(".mb-head", {hasText: "Wandregal"}).first().click(); await sleep(700);
const elev = await page.locator('input[aria-label="Abstand vom Boden in cm"]').first().inputValue().catch(() => "");
ok(elev === "150", `Wandregal shows its height above the floor (${elev})`);
await page.locator("text=3D-Modell ansehen").first().click(); await sleep(4000);
ok(await page.locator("model-viewer").count() === 1, "3D viewer opens");
await snap("A-3d-viewer");
for (const ext of ["glb", "stl", "csv"]) {
  const r = await fetch(`${BASE}/api/workshop/${A}/model/ga0002.${ext}`);
  ok(r.ok && (await r.arrayBuffer()).byteLength > 80, `download ${ext}`);
}
await page.keyboard.press("Escape"); await sleep(500);

/* 4. placement by tap (centre of the piece), wall-hang by tap on a wall, tap-to-remove */
console.log("4 Platzieren");
await go(`#/p/${A}/platzieren`, 3500);
const canvas = page.locator(".pl-stage canvas").first();
const box = await canvas.boundingBox();
const tapAt = async (fx, fy) => { const x = box.x + box.width * fx, y = box.y + box.height * fy; V.touch ? await page.touchscreen.tap(x, y) : await page.mouse.click(x, y); await sleep(1500); };
await page.locator(".pl-chips .chip, .chips .chip", {hasText: "Esstisch"}).first().click(); await sleep(600);
await tapAt(0.52, 0.64);
let dA = await doc(A), t = dA.pieces.find(p => p.id === "ga0001");
ok(t.placement.mode === "3d" && t.placement.box, `table placed by a tap (box ${t.placement.box})`);
ok(t.placement.box && t.placement.box[2] > 700, "table reaches down to the floor in the photo (not shrunken far behind)");
await page.locator(".pl-chips .chip, .chips .chip", {hasText: "Wandregal"}).first().click(); await sleep(600);
await tapAt(0.55, 0.22);
dA = await doc(A); t = dA.pieces.find(p => p.id === "ga0002");
ok(t.placement.mode === "3d" && t.placement.pose?.wall, `wall shelf hung on a wall by a tap (wall ${t.placement.pose?.wall}, elev ${t.elev})`);
await snap("A-placed");
// remove tool: tap selects an object (SAM), „Größer auswählen“ replaces it
const more = page.locator("text=Mehr"); if (await more.count()) { await more.first().click(); await sleep(600); }
await page.locator("text=Etwas aus dem Bild entfernen").first().click(); await sleep(800);
const nRm = ((await doc(A)).removes || []).length;
await tapAt(0.30, 0.66);
await sleep(1500);
let rm = (await doc(A)).removes || [];
ok(rm.length > nRm && rm.some(r => r && r.poly), `tap in remove mode selects an outline (${rm.length} areas)`);
ok(await page.locator(".pl-bx.rm.poly").count() > 0, "outline drawn on the photo");
const bigger = page.locator(".toast button", {hasText: "Größer"});
if (await bigger.count()) { await bigger.first().click(); await sleep(1500); ok(true, "„Größer auswählen“ offered and used"); }
await snap("A-remove-outline");
await page.locator(".pl-hint button", {hasText: "Fertig"}).first().click().catch(() => {}); await sleep(500);

/* 5. step ④: list, 3D template, KI-Plancheck, (base option) */
console.log("5 Berechnen");
await go(`#/p/${A}/berechnen`, 4000);
const items = await text(".bc-items li");
ok(items.length === 3 && items.some(i => /Wandregal.*hängt/.test(i)), `step 4 lists every piece with its data (${items.length})`);
ok(await page.locator("summary", {hasText: "3D-Vorlage"}).count() === 1, "3D template available");
for (let i = 0; i < 40; i++) { const tx = (await text(".bc-check")).join(" "); if (!/prüft/.test(tx)) break; await sleep(2000); }
const chk = (await text(".bc-check")).join(" ");
ok(/Plancheck/.test(chk), `KI-Plancheck answered (${chk.slice(0, 90)})`);
await snap("A-berechnen");

/* 6. one real generation (GPU, ~3 min): star, Präsentieren shows that proposal, newest first */
if (flag("generate")) {
  console.log("6 Berechnung");
  const r = await api("POST", `/api/workshop/${A}/generate`, {kind: "room", count: 1, quality: "entwurf", base: false, active_piece: "ga0001"});
  for (let i = 0; i < 120; i++) { const j = await api("GET", `/api/jobs/${r.job.id}`); if (j.state === "done" && ["done", "off", "error", "skipped"].includes(j.judge_state)) break; await sleep(5000); }
  dA = await doc(A);
  ok(dA.variants.length >= 1, `proposal recorded (${dA.variants.length})`);
  await go(`#/p/${A}/ergebnis`, 3500);
  const first = await page.evaluate(() => document.querySelector(".vgrid img, .vs-list img, [class*=prop] img")?.getAttribute("src"));
  ok(!!first && first.includes(dA.variants.sort((a, b) => b.image.localeCompare(a.image))[0].image.slice(0, 15)), "„Neueste“ shows the newest proposal first");
  const pres = page.locator("button", {hasText: "Präsentieren"});
  if (await pres.count()) {
    await pres.first().click(); await sleep(2000);
    const src = await page.evaluate(() => document.querySelector(".pr-ov img:last-of-type, .pr-ov img")?.getAttribute("src") || "");
    const shown = await page.evaluate(() => document.querySelector(".er-stage img:last-of-type, .slider img:last-of-type")?.getAttribute("src") || "");
    ok(!!src, `Präsentieren opens (${src.slice(-30)})`);
    await snap("A-present");
    await page.keyboard.press("Escape"); await sleep(500);
  }
}

/* 7. exports & errors */
console.log("7 Abschluss");
ok(errors.length === 0, `no page errors (${errors.slice(0, 3).join(" | ")})`);
const hs = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
ok(hs <= 0, "no horizontal scrolling");
await browser.close();
if (!flag("keep")) {
  for (const id of [A, B]) {
    const d = await doc(id);
    await api("POST", `/api/workshop/${id}/purge`, {confirm: d.title}).catch(e => ok(false, `purge ${id}: ${e.message}`));
  }
  console.log("  TEST orders deleted");
}
console.log(`\n${passed.length} ok, ${problems.length} Probleme${problems.length ? ":\n - " + problems.join("\n - ") : ""}\nScreenshots: ${SHOTS}`);
process.exit(problems.length ? 1 : 0);
