// Screenshot walk for the guided editor, steps ①–③ (spec §12 acceptance), against the Playwright mock.
//   node tests/ui/editor.shots.mjs [phone,phone-land,tablet,desktop]   → SCRATCH/shots-v3/ed-*.png, exit 1 on any violation
import {join} from "node:path";
import {readFileSync} from "node:fs";
import {launch, checkPage, checkSecureFallbacks, trackRequests, VIEWPORTS, stopServer} from "./check.mjs";
import {installMock, FIX} from "./mock_api.mjs";

const NEW = "0e1e2e3e4e5e", FULL = "a1b2c3d4e5f6", PORTRAIT = "9f8e7d6c5b4a";
const PRIMARY = "#ws-actions .btn.primary";
// The mock cannot read XHR file bodies (Playwright exposes only the multipart boundary), so uploaded photos are
// served from the fixture here.
const ROOM_PNG = readFileSync(join(FIX, "room.png"));
const UPLOADS = [["**/outputs/uploads/mock-*", route => route.fulfill({status: 200, contentType: "image/png", body: ROOM_PNG})]];
const only = (process.argv[2] || "").split(",").filter(Boolean);
const problems = [];
const fail = msg => { problems.push(msg); console.log("  ✗", msg); };
const ok = (cond, msg) => { if (!cond) fail(msg); };

const primary = page => page.locator(PRIMARY).first();
const primaryState = page => page.evaluate(sel => {
  const b = document.querySelector(sel), r = document.querySelector("#ws-actions .reason");
  return b ? {text: b.textContent.trim(), disabled: b.disabled, reason: r ? r.textContent.trim() : ""} : null;
}, PRIMARY);
async function until(page, fn, arg, ms = 8000, what = "condition") {
  try { await page.waitForFunction(fn, arg, {timeout: ms}); return true; } catch { fail(`timeout: ${what}`); return false; }
}
const shot = async (page, vp, name, opts = {primary: PRIMARY}) => (await checkPage(page, `ed-${vp.name}-${name}`, opts)).forEach(fail);
const docOf = (mock, pid) => mock.doc(pid);
const btn = (page, text) => page.locator("button", {hasText: text}).filter({visible: true}).first();
// the visible part of the photo canvas (a landscape photo on a portrait phone starts enlarged, partly outside the stage)
async function canvasBox(page, sel = ".pl-canvas canvas") {
  const b = await page.locator(sel).first().boundingBox(), st = await page.locator(".pl-stage").first().boundingBox().catch(() => null);
  if (!b || !st) return b;
  const x = Math.max(b.x, st.x), y = Math.max(b.y, st.y);
  return {x, y, width: Math.min(b.x + b.width, st.x + st.width) - x, height: Math.min(b.y + b.height, st.y + st.height) - y};
}

async function walkNew(vp) {
  const {context, base, close} = await launch({viewport: vp});
  const mock = await installMock(context, {scenario: "default", geoDelay: 1500, routes: UPLOADS});
  const page = await context.newPage();
  page.on("pageerror", e => fail(`${vp.name}: page error ${e.message}`));
  page.on("dialog", d => { fail(`${vp.name}: unexpected dialog ${d.message()}`); d.dismiss().catch(() => {}); });
  const foreign = trackRequests(page, base);
  await page.goto(`${base}/werkstatt#/p/${NEW}`);
  await page.waitForSelector(".rm", {timeout: 10000});
  ok(/#\/p\/0e1e2e3e4e5e\/raum$/.test(page.url()), `${vp.name}: new project opens at Raum (${page.url()})`);

  // ① empty state: camera first, primary disabled with the reason
  let ps = await primaryState(page);
  ok(ps && ps.text === "Weiter: Möbel" && ps.disabled && /Bitte zuerst ein Raumfoto/.test(ps.reason), `${vp.name}: Raum primary gated (${JSON.stringify(ps)})`);
  ok(await page.getByText("Am besten Hoch- oder Querformat", {exact: false}).isVisible(), `${vp.name}: photo hint visible`);
  await shot(page, vp, "raum-leer");

  // camera pick (mock file) → upload → analysis (primary disabled while measuring) → result card
  const chooser = page.waitForEvent("filechooser");
  await btn(page, "📷 Raum fotografieren").click();
  const fc = await chooser;
  ok(fc.element && (await fc.element().getAttribute("capture")) === "environment", `${vp.name}: camera picker uses capture=environment`);
  await fc.setFiles(join(FIX, "room.png"));
  await until(page, () => /Raum wird vermessen/.test(document.body.textContent), null, 8000, "analysis running text");
  ps = await primaryState(page);
  ok(ps.disabled && /Raum wird vermessen/.test(ps.reason), `${vp.name}: primary disabled during analysis (${JSON.stringify(ps)})`);
  await shot(page, vp, "raum-vermessen");
  await until(page, () => /Raum erkannt/.test(document.body.textContent), null, 8000, "analysis card");
  ps = await primaryState(page);
  ok(!ps.disabled, `${vp.name}: primary enabled after analysis`);
  ok(docOf(mock, NEW).geometry && docOf(mock, NEW).geometry.ok && /^uploads\/mock-/.test(docOf(mock, NEW).room), `${vp.name}: room + geometry saved`);
  await shot(page, vp, "raum-erkannt");

  // ceiling height confirm
  await page.locator(".rm-cal input").fill("250");
  await btn(page, "Übernehmen").click();
  await until(page, () => /Deckenhöhe 2,50 m bestätigt\./.test(document.body.textContent), null, 6000, "height toast");
  await until(page, () => /Maße nachgemessen \(Decke 2,50 m\)/.test(document.body.textContent), null, 6000, "nachgemessen label");
  ok(docOf(mock, NEW).calibration && docOf(mock, NEW).calibration.room_height_cm === 250, `${vp.name}: calibration.room_height_cm saved`);

  // tape measure: full-screen line, the length form below it (line stays visible), a contradiction asks first
  await btn(page, "Genauer nachmessen").click();
  await page.waitForSelector(".ed-tape canvas");
  await page.waitForTimeout(300);
  const drawLine = async () => {
    const st = await page.locator(".ed-tape .tp-stage").boundingBox();
    await page.mouse.move(st.x + st.width * 0.3, st.y + st.height * 0.8);
    await page.mouse.down();
    await page.mouse.move(st.x + st.width * 0.6, st.y + st.height * 0.85, {steps: 6});
    await page.mouse.up();
    await page.waitForSelector(".ed-tape .tp-form input[type=number]", {timeout: 5000}).catch(() => fail(`${vp.name}: tape length form missing`));
    await page.waitForTimeout(400);
  };
  await drawLine();
  ok(/Im Foto gemessen: ca\. \d+ cm/.test(await page.locator(".ed-tape .tp-form").textContent()), `${vp.name}: measured length shown`);
  ok(!(await page.locator(".sheet").count()), `${vp.name}: no modal sheet over the line`);
  ok((await page.locator(".ed-tape").textContent()).split("Wie lang ist das in echt?").length === 2, `${vp.name}: the question appears once`);
  await shot(page, vp, "raum-massband", {primary: null});
  await page.locator(".ed-tape .tp-form input[type=number]").fill("130");
  await page.locator(".ed-tape .tp-form button", {hasText: "Maße übernehmen"}).click();
  await page.waitForSelector(".dialog", {timeout: 5000}).catch(() => fail(`${vp.name}: contradiction not asked`));
  ok(/Dann wäre die Decke \d,\d\d m hoch – stimmt das\?/.test(await page.locator(".dialog").textContent()), `${vp.name}: ceiling question`);
  await shot(page, vp, "raum-massband-widerspruch", {primary: null});
  await page.locator(".dialog .btn", {hasText: "Nein"}).click();
  await page.waitForTimeout(400);
  ok(await page.locator(".ed-tape").count() === 1 && docOf(mock, NEW).calibration.room_height_cm === 250, `${vp.name}: kept the confirmed ceiling`);
  await drawLine();
  await page.locator(".ed-tape .tp-form input[type=number]").fill("90");
  await page.locator(".ed-tape .tp-form button", {hasText: "Maße übernehmen"}).click();
  await until(page, () => !document.querySelector(".ed-tape") && !document.querySelector(".sheet"), null, 6000, "tape closed after adopting");
  ok(docOf(mock, NEW).calibration && docOf(mock, NEW).calibration.length_cm === 90, `${vp.name}: tape calibration saved`);
  await shot(page, vp, "raum-nach-messung");

  // ② Möbel: type tile → real default dims, wood
  await primary(page).click();
  await page.waitForSelector(".mb .tile");
  ps = await primaryState(page);
  ok(ps.text === "Weiter: Platzieren" && !ps.disabled && /Bitte wählen Sie aus/.test(ps.reason), `${vp.name}: Möbel primary with warning (${JSON.stringify(ps)})`);
  await shot(page, vp, "moebel-leer");
  await page.locator(".mb .tile", {hasText: "Sideboard"}).click();
  await until(page, () => [...document.querySelectorAll(".mb-dims input")].map(i => i.value).join() === "180,75,45", null, 5000, "dims prefilled 180/75/45");
  await page.locator(".mb .chip", {hasText: "Nussbaum"}).click();
  await page.waitForTimeout(400);
  const piece = docOf(mock, NEW).pieces[0];
  ok(piece.type === "sideboard" && piece.w === 180 && piece.material === "nuss", `${vp.name}: piece saved (${piece.type}, ${piece.w}, ${piece.material})`);
  await page.locator(".details > summary", {hasText: "Skizze oder CAD"}).click();
  await shot(page, vp, "moebel");
  // sketch → photo render with inline progress
  const sk = page.waitForEvent("filechooser");
  await btn(page, "Skizze hochladen").click();
  await (await sk).setFiles(join(FIX, "sketch.png"));
  await page.waitForSelector("button:has-text('Skizze als Foto rendern')", {timeout: 6000}).catch(() => fail(`${vp.name}: render button missing`));
  ok(/^uploads\//.test(docOf(mock, NEW).pieces[0].sketch || ""), `${vp.name}: sketch saved`);
  await btn(page, "Skizze als Foto rendern").click();
  await until(page, () => /Wird gerendert/.test(document.querySelector(".mb")?.textContent || ""), null, 9000, "render progress inline");
  await shot(page, vp, "moebel-rendering");

  // ③ Platzieren: tap on the floor, move, rotate, undo
  await primary(page).click();
  await page.waitForSelector(".pl-canvas canvas");
  await page.waitForTimeout(400);
  const phone = vp.width < 700 || vp.height < 700;
  ok(!phone || await page.evaluate(() => document.body.classList.contains("immersive")), `${vp.name}: immersive on phones`);
  ok(/Tippen Sie auf den Boden/.test(await page.locator(".pl-hint").textContent()), `${vp.name}: tap overlay shown`);
  await shot(page, vp, "platzieren-leer");
  await page.waitForFunction(() => !document.querySelector(".toast"), null, {timeout: 12000}).catch(() => {});   // toasts sit over the stage
  let cb = await canvasBox(page);
  await page.mouse.click(cb.x + cb.width * 0.5, cb.y + cb.height * 0.8);
  await page.waitForTimeout(500);
  let pl = docOf(mock, NEW).pieces[0].placement;
  ok(pl && pl.mode === "3d" && pl.box, `${vp.name}: tap placed the piece (${JSON.stringify(pl)})`);
  const before = pl && pl.pose;
  cb = await canvasBox(page);   // the first tap may pan the view so the whole piece is visible
  // move: grab the box centre and drag a little
  const bx = pl.box, cx = cb.x + cb.width * (bx[1] + bx[3]) / 2000, cy = cb.y + cb.height * (bx[0] * 0.3 + bx[2] * 0.7) / 1000;
  await page.mouse.move(cx, cy); await page.mouse.down();
  await page.mouse.move(cx + cb.width * 0.08, cy - cb.height * 0.03, {steps: 8}); await page.mouse.up();
  await page.waitForTimeout(500);
  pl = docOf(mock, NEW).pieces[0].placement;
  ok(before && pl.pose && (Math.abs(pl.pose.x - before.x) > 1 || Math.abs(pl.pose.z - before.z) > 1), `${vp.name}: drag moved the piece`);
  const yaw0 = pl.pose.yaw;
  await page.locator('.pl-tools button[aria-label="↻ 90° drehen"], .pl-panel button[aria-label="↻ 90° drehen"]').first().click();
  await page.waitForTimeout(400);
  ok(Math.abs(((docOf(mock, NEW).pieces[0].placement.pose.yaw - yaw0 + 540) % 360) - 180) === 90, `${vp.name}: rotated 90°`);
  await btn(page, "Rückgängig").click();
  await page.waitForTimeout(400);
  ok(docOf(mock, NEW).pieces[0].placement.pose.yaw === yaw0, `${vp.name}: undo restored the yaw`);
  await shot(page, vp, "platzieren");

  // „Mehr …“ sheet (phones) / panel (wide): remove frame, then a 2D frame
  if (phone) {
    await btn(page, "Mehr …").click();
    await page.waitForSelector(".sheet");
    await page.waitForTimeout(400);   // sheet animation
    await shot(page, vp, "platzieren-mehr", {primary: null});
    await page.locator(".sheet button", {hasText: "Etwas aus dem Bild entfernen"}).click();
  } else await btn(page, "Etwas aus dem Bild entfernen").click();
  await page.waitForTimeout(200);
  ok(/Rahmen um das, was verschwinden soll/.test(await page.locator(".pl-hint").textContent()), `${vp.name}: remove hint`);
  cb = await canvasBox(page);
  await page.mouse.move(cb.x + cb.width * 0.05, cb.y + cb.height * 0.45); await page.mouse.down();
  await page.mouse.move(cb.x + cb.width * 0.2, cb.y + cb.height * 0.7, {steps: 6}); await page.mouse.up();
  await page.waitForTimeout(400);
  ok((docOf(mock, NEW).removes || []).length === 1, `${vp.name}: remove frame saved`);
  await shot(page, vp, "platzieren-entfernen");
  await page.locator(".pl-hint button", {hasText: "Fertig"}).click();
  if (phone) { await btn(page, "Mehr …").click(); await page.locator(".sheet button", {hasText: "Von Hand einrahmen"}).click(); }
  else await btn(page, "Von Hand einrahmen").click();
  await page.waitForTimeout(400);
  pl = docOf(mock, NEW).pieces[0].placement;
  ok(pl.mode === "2d" && pl.box, `${vp.name}: switched to a 2D frame`);
  await shot(page, vp, "platzieren-2d");
  ok(!foreign.length, `${vp.name}: foreign requests ${foreign.join(", ")}`);
  await close();
}

// Raum gate after a failed analysis and with „Ohne Raumfoto weiter“
async function walkGate(vp) {
  const {context, base, close} = await launch({viewport: vp});
  await installMock(context, {scenario: "geofail", geoDelay: 300, routes: UPLOADS});
  const page = await context.newPage();
  page.on("pageerror", e => fail(`${vp.name}: page error ${e.message}`));
  await page.goto(`${base}/werkstatt#/p/${NEW}/raum`);
  await page.waitForSelector(".rm");
  const chooser = page.waitForEvent("filechooser");
  await btn(page, "Foto auswählen").click();
  await (await chooser).setFiles(join(FIX, "room.png"));
  await until(page, () => /konnte nicht vermessen werden/.test(document.body.textContent), null, 8000, "failure card");
  let ps = await primaryState(page);
  ok(ps && !ps.disabled, `${vp.name}: primary enabled after a failed analysis`);
  await shot(page, vp, "raum-fehlgeschlagen");
  await primary(page).click(); await page.waitForSelector(".mb");
  await primary(page).click(); await page.waitForSelector(".pl-canvas");
  await page.waitForTimeout(300);
  ok(/Raum nicht vermessen/.test(await page.locator(".pl-hint").textContent()), `${vp.name}: 2D fallback hint`);
  const cb = await canvasBox(page, ".pl-canvas");
  await page.mouse.move(cb.x + cb.width * 0.3, cb.y + cb.height * 0.4); await page.mouse.down();
  await page.mouse.move(cb.x + cb.width * 0.6, cb.y + cb.height * 0.8, {steps: 5}); await page.mouse.up();
  await page.waitForTimeout(300);
  await shot(page, vp, "platzieren-ohne-vermessung");
  await close();

  const l2 = await launch({viewport: vp});
  await installMock(l2.context, {});
  const p2 = await l2.context.newPage();
  await p2.goto(`${l2.base}/werkstatt#/p/${NEW}/raum`);
  await p2.waitForSelector(".rm");
  await btn(p2, "Ohne Raumfoto weiter").click();
  await p2.waitForTimeout(200);
  ps = await primaryState(p2);
  ok(ps && !ps.disabled, `${vp.name}: primary enabled after „Ohne Raumfoto weiter“`);
  ok(await p2.getByText("Umgebung (optional)").isVisible(), `${vp.name}: Umgebung field shown`);
  await shot(p2, vp, "raum-ohne-foto");
  await l2.close();
}

// Phone geometry of step ③ (landscape and portrait photos) and the remote-move hold during a drag
async function walkStage(vp) {
  const {context, base, close} = await launch({viewport: vp});
  const mock = await installMock(context, {});
  const page = await context.newPage();
  page.on("pageerror", e => fail(`${vp.name}: page error ${e.message}`));
  const phoneVp = vp.width < 700 || vp.height < 700;
  for (const [pid, kind] of phoneVp ? [[FULL, "quer"], [PORTRAIT, "hoch"]] : []) {
    await page.goto(`${base}/werkstatt#/p/${pid}/platzieren`);
    await page.waitForSelector(".pl-canvas canvas");
    await page.waitForTimeout(600);
    const m = await page.evaluate(() => {
      const st = document.querySelector(".pl-stage").getBoundingClientRect(), cv = document.querySelector(".pl-canvas").getBoundingClientRect();
      const fh = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--footer-h")) || 0;
      const tools = [...document.querySelectorAll(".pl-tools button, .pl-panel button")].filter(b => b.offsetParent).slice(0, 4).map(b => b.getBoundingClientRect());
      return {st: [st.top, st.height, st.width], cv: [cv.left, cv.width, cv.height], free: innerHeight - 44 - fh, iw: innerWidth, ih: innerHeight,
        scroll: document.documentElement.scrollHeight, tools: tools.map(r => r.bottom <= innerHeight - fh + 0.5 && r.top >= 0)};
    });
    if (vp.width < 700) {   // portrait phone
      ok(m.st[1] >= 0.5 * m.free - 1, `${vp.name}/${kind}: stage ${Math.round(m.st[1])} px < 50 % of ${Math.round(m.free)}`);
      ok(Math.abs(m.cv[1] - m.iw) < 1.5 || Math.abs(m.cv[2] - m.st[1]) < 1.5 || (m.cv[1] >= m.iw - 1 && m.cv[2] >= 0.7 * m.st[1]),
         `${vp.name}/${kind}: photo spans the full width or the full stage height, or starts enlarged to ≥ 70 % of the stage (${m.cv} in ${m.st})`);
      ok(Math.abs(m.st[2] - m.iw) < 1, `${vp.name}/${kind}: stage uses the full width`);
    } else {                // landscape phone
      ok(Math.abs(m.cv[2] - m.st[1]) < 1.5, `${vp.name}/${kind}: photo spans the stage height`);
      ok(m.st[1] >= 0.5 * m.free - 1, `${vp.name}/${kind}: stage ≥ 50 % of the free height`);
    }
    ok(m.tools.length === 4 && m.tools.every(Boolean), `${vp.name}/${kind}: four actions visible without scrolling`);
    ok(m.scroll <= m.ih + 1, `${vp.name}/${kind}: no page scroll (${m.scroll} > ${m.ih})`);
    await shot(page, vp, `platzieren-${kind}`);
  }

  // remote move during a drag: held until release, then the local final move wins (placement is last-writer-wins)
  await page.goto(`${base}/werkstatt#/p/${FULL}/platzieren`);
  await page.waitForSelector(".pl-canvas canvas");
  await page.waitForTimeout(600);
  const cb = await canvasBox(page);
  const box = docOf(mock, FULL).pieces[0].placement.box;
  const cx = cb.x + cb.width * (box[1] + box[3]) / 2000, cy = cb.y + cb.height * (box[0] * 0.3 + box[2] * 0.7) / 1000;
  const storeVal = () => page.evaluate(() => import("/static/ws/store.js").then(m => JSON.stringify(m.store.doc.pieces[0].placement.pose)));
  const base0 = await storeVal();
  await page.mouse.move(cx, cy); await page.mouse.down();
  await page.mouse.move(cx + 20, cy, {steps: 4});
  mock.remote(FULL, [{op: "set", path: "pieces.p1aaaa.placement", value: {mode: "3d", pose: {x: -120, z: 300, yaw: 0, wall: null}, box: [500, 100, 700, 300]}}], "ben");
  await page.waitForTimeout(4200);    // ≥ one sync round
  const during = await storeVal();
  ok(during === base0, `${vp.name}: remote placement held back during the drag (${during})`);
  await page.mouse.move(cx + 40, cy, {steps: 4}); await page.mouse.up();
  await page.waitForTimeout(800);
  const after = JSON.parse(await storeVal());
  ok(after.x !== -120, `${vp.name}: local final move wins after release`);
  ok(Math.abs(docOf(mock, FULL).pieces[0].placement.pose.x - after.x) < 0.01, `${vp.name}: server has the final local pose`);
  if (!phoneVp) {   // keyboard: arrows move 5 cm, R turns 90°
    await page.locator("body").click({position: {x: 5, y: 300}}).catch(() => {});
    const x0 = docOf(mock, FULL).pieces[0].placement.pose.x, y0 = docOf(mock, FULL).pieces[0].placement.pose.yaw;
    await page.keyboard.press("ArrowRight"); await page.waitForTimeout(400);
    ok(Math.abs(docOf(mock, FULL).pieces[0].placement.pose.x - (x0 + 5)) < 0.6, `${vp.name}: ArrowRight moved 5 cm`);
    await page.keyboard.press("r"); await page.waitForTimeout(400);
    ok(Math.abs(((docOf(mock, FULL).pieces[0].placement.pose.yaw - y0 + 540) % 360) - 180) === 90, `${vp.name}: R turned 90°`);
  }
  await close();
}

const VPS = [VIEWPORTS.phone, VIEWPORTS.phoneLandscape, VIEWPORTS.tablet, VIEWPORTS.desktop].filter(v => !only.length || only.includes(v.name));
for (const vp of VPS) {
  console.log(`— ${vp.name}`);
  try { await walkNew(vp); } catch (e) { fail(`${vp.name}: walk crashed: ${e.stack || e}`); }
  try { await walkGate(vp); } catch (e) { fail(`${vp.name}: gate walk crashed: ${e.stack || e}`); }
  if (vp.touch && (vp.width < 700 || vp.height < 700)) try { await walkStage(vp); } catch (e) { fail(`${vp.name}: stage walk crashed: ${e.stack || e}`); }
}
// one run on a non-secure origin (http://werkstatt.lan, like a phone in the shop Wi-Fi)
if (!only.length || only.includes("insecure")) try {
  const vp = VIEWPORTS.phone, {context, base, close} = await launch({viewport: vp, insecure: true});
  await installMock(context, {routes: UPLOADS});
  const page = await context.newPage();
  page.on("pageerror", e => fail(`insecure: page error ${e.message}`));
  for (const step of ["raum", "moebel", "platzieren"]) {
    await page.goto(`${base}/werkstatt#/p/${FULL}/${step}`);
    await page.waitForSelector(step === "raum" ? ".rm" : step === "moebel" ? ".mb" : ".pl-canvas");
    await page.waitForTimeout(500);
    await shot(page, {name: "insecure"}, step);
  }
  (await checkSecureFallbacks(page)).forEach(fail);
  await close();
} catch (e) { fail(`insecure run crashed: ${e.stack || e}`); }
if (!only.length || only.includes("desktop")) try { await walkStage(VIEWPORTS.desktop); } catch (e) { fail(`desktop: hold walk crashed: ${e.stack || e}`); }
await stopServer();
console.log(problems.length ? `${problems.length} problem(s)` : "editor shots: all checks passed");
process.exit(problems.length ? 1 : 0);
