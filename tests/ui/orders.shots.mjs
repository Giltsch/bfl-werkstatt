// Screenshot walk for Aufträge, Kunde & Auftrag, Einstellungen and the customer page at all five viewports (spec §14).
//   node tests/ui/orders.shots.mjs [phone,desktop,…]   → shots in SCRATCH/shots-v3/ord-*.png, exit 1 on any violation
import {launch, checkPage, checkSecureFallbacks, trackRequests, VIEWPORTS, stopServer} from "./check.mjs";
import {installMock, FIX} from "./mock_api.mjs";
import {join} from "node:path";

const PID = "a1b2c3d4e5f6";            // doc-full: status kunde, version 1 with an XSS change request in Verlauf
const TOKEN = "tokFixture0000000000000000000001";
const XSS = "<img src=x onerror=alert(1)>";
const PRIMARY = "#ws-actions .btn.primary";
const only = (process.argv[2] || "").split(",").filter(Boolean);
const problems = [];
const fail = msg => { problems.push(msg); console.log("  ✗", msg); };
const ok = (cond, msg) => { if (!cond) fail(msg); };

async function open(vp, {scenario = "default", insecure = false, init} = {}) {
  const {context, base, close} = await launch({viewport: vp, insecure});
  const mock = await installMock(context, {scenario});
  // two orders due for deletion (one archived) for the retention notice
  for (const x of mock.state.listExtra) if (["e0e0e0e0e0e4", "a0a0a0a0a0a6"].includes(x.id)) x.retention_due = true;
  if (init) await context.addInitScript(init);
  const page = await context.newPage();
  const dialogs = [];
  page.on("dialog", d => { dialogs.push(d.message()); d.dismiss().catch(() => {}); });
  page.on("pageerror", e => fail(`${vp.name}: page error ${e.message}`));
  const foreign = trackRequests(page, base);
  return {context, base, close, mock, page, dialogs, foreign};
}
const shot = async (page, vp, name, opts = {}) => (await checkPage(page, `ord-${vp.name}-${name}`, opts)).forEach(fail);
const cards = page => page.locator(".order").count();
const settle = page => page.waitForTimeout(350);

async function dashboard(vp) {
  const s = await open(vp);
  const {page, base} = s;
  await page.goto(`${base}/werkstatt#/`);
  await page.waitForSelector(".order", {timeout: 10000});
  await settle(page);
  ok(await cards(page) === 6, `${vp.name}: default list shows 6 active orders (got ${await cards(page)})`);
  ok((await page.locator(".order").first().textContent()).includes("Esszimmer Sideboard"), `${vp.name}: Änderungswunsch first`);
  ok(/2 Aufträge sind zur Löschung fällig/.test(await page.locator(".dash-note:not(.dash-alert)").textContent()), `${vp.name}: retention notice`);
  await shot(page, vp, "dash", {primary: PRIMARY});

  await page.fill(".dash-search input", "huber");
  await settle(page);
  ok(await cards(page) === 1, `${vp.name}: search „huber“ → 1`);
  await shot(page, vp, "dash-search", {primary: PRIMARY});
  await page.fill(".dash-search input", "zzz");
  await settle(page);
  ok((await page.locator(".empty").textContent()).includes("Nichts gefunden für „zzz“."), `${vp.name}: no-hit text`);
  await page.fill(".dash-search input", "");

  await page.locator(".dash-opts .chip", {hasText: "Archiv"}).click();
  await settle(page);
  ok(await cards(page) === 1, `${vp.name}: archive shows 1`);
  await shot(page, vp, "dash-archiv", {primary: PRIMARY});
  await page.locator(".dash-opts .chip", {hasText: "Archiv"}).click();

  await page.locator(".dash-note .btn", {hasText: "Ansehen"}).click();
  await settle(page);
  ok(await cards(page) === 2, `${vp.name}: retention filter shows 2`);
  ok(await page.locator(".chip.active", {hasText: "Zur Löschung fällig"}).count() === 1, `${vp.name}: retention chip active`);
  await shot(page, vp, "dash-loeschung", {primary: PRIMARY});
  await page.locator(".chip.active", {hasText: "Zur Löschung fällig"}).click();

  // new order with photo: the camera picker opens first, then create → upload → PATCH room → step ①
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".new-order");
  await page.waitForTimeout(400);
  await shot(page, vp, "neu");
  await page.locator(".new-order .seg button", {hasText: "Familie"}).click();
  await page.fill(".new-order .field:nth-child(2) input", "Test");
  await page.fill(".new-order input[type=tel]", "0981 4711");
  await page.fill(".new-order input[type=email]", "test@example.org");
  const [chooser] = await Promise.all([page.waitForEvent("filechooser"), page.locator(".new-order .btn.primary").click()]);
  await chooser.setFiles(join(FIX, "room.png"));
  await page.waitForFunction(() => /^#\/p\/[a-z0-9]+\/raum$/.test(location.hash), null, {timeout: 10000}).catch(() => fail(`${vp.name}: did not open step Raum`));
  const pid = (await page.evaluate(() => location.hash)).split("/")[2];
  const d = s.mock.doc(pid);
  ok(d && d.customer === "Test" && d.contact.phone === "0981 4711" && d.contact.email === "test@example.org" && d.contact.salutation === "Familie",
     `${vp.name}: new order data (Anrede, Telefon, E-Mail)`);
  ok(d && /^uploads\/mock-\d+\.png$/.test(d.room || ""), `${vp.name}: room photo PATCHed (${d && d.room})`);
  await s.close();

  for (const scenario of ["busy", "empty"]) {
    const t = await open(vp, {scenario});
    await t.page.goto(`${t.base}/werkstatt#/`);
    await t.page.waitForSelector(scenario === "empty" ? ".empty" : ".order", {timeout: 10000});
    await t.page.waitForTimeout(1200);
    if (scenario === "busy") ok(await t.page.locator(".badge", {hasText: "Berechnung läuft …"}).count() > 0, `${vp.name}: busy marker`);
    else ok((await t.page.locator(".empty").textContent()).includes("Noch keine Aufträge"), `${vp.name}: empty text`);
    await shot(t.page, vp, `dash-${scenario}`, {primary: PRIMARY});
    await t.close();
  }
}

async function auftrag(vp) {
  const s = await open(vp);
  const {page, base, dialogs} = s;
  await page.goto(`${base}/werkstatt#/p/${PID}/auftrag`);
  await page.waitForSelector(".auftrag", {timeout: 10000});
  await settle(page);
  await shot(page, vp, "auftrag");
  await shot(page, vp, "auftrag-full", {fullPage: true});

  // Verlauf: the XSS strings show literally and create nothing
  const verlauf = await page.locator(".act-list").textContent();
  ok(verlauf.includes(XSS) && verlauf.includes("<b>Herr X</b>"), `${vp.name}: XSS text literal in Verlauf`);
  ok(await page.locator('.act-list img, .act-list b, img[src="x"]').count() === 0, `${vp.name}: no element from XSS text`);

  // contact edits → links follow, PATCH reaches the server
  const tel = page.locator(".field", {hasText: "Telefon"}).locator("input");
  await tel.fill("0981 99 88");
  await page.waitForTimeout(700);
  ok(await page.locator("a.btn", {hasText: "Anrufen"}).getAttribute("href") === "tel:09819988", `${vp.name}: tel link`);
  ok(s.mock.doc(PID).contact.phone === "0981 99 88", `${vp.name}: phone saved`);
  const route = await page.locator("a.btn", {hasText: "Route"}).getAttribute("href");
  ok(route && route.startsWith("https://www.openstreetmap.org/search?query=Musterweg"), `${vp.name}: route link`);
  ok(await page.locator("button", {hasText: "In Kalender (.ics)"}).isVisible(), `${vp.name}: ics button`);
  const [dl] = await Promise.all([page.waitForEvent("download", {timeout: 5000}).catch(() => null), page.locator("button", {hasText: "In Kalender (.ics)"}).click()]);
  ok(dl && /\.ics$/.test(dl.suggestedFilename()), `${vp.name}: .ics download`);

  // backward status asks first
  await page.locator(".steps .chip", {hasText: "Entwurf"}).click();
  await page.waitForSelector(".dialog");
  await page.waitForTimeout(400);
  ok((await page.locator(".dialog").textContent()).includes("Status zurück auf „Entwurf“ setzen?"), `${vp.name}: backward confirm`);
  await shot(page, vp, "auftrag-status");
  await page.locator(".dialog .btn", {hasText: "Abbrechen"}).click();
  ok(s.mock.doc(PID).status === "kunde", `${vp.name}: status unchanged after cancel`);
  await page.locator(".steps .chip", {hasText: "Auftrag"}).last().click();
  await page.waitForTimeout(400);
  ok(s.mock.doc(PID).status === "auftrag", `${vp.name}: forward status without asking`);

  // Positionstext (★ proposal), copy
  await page.locator("button", {hasText: "Positionstext kopieren"}).click();
  await page.waitForTimeout(300);
  await shot(page, vp, "auftrag-kopiert");
  await page.keyboard.press("Escape");

  // purge dialog (mentions the backups), confirm with the title → dashboard
  await page.locator("button", {hasText: "Endgültig löschen …"}).click();
  await page.waitForSelector(".dialog input");
  await page.waitForTimeout(400);   // let the dialog finish its entry animation before measuring
  ok(/In älteren Datensicherungen bleibt er bis zu 14 Tage erhalten/.test(await page.locator(".dialog").textContent()), `${vp.name}: purge text`);
  await shot(page, vp, "auftrag-loeschen");
  await page.fill(".dialog input", "Esszimmer Sideboard");
  await page.locator(".dialog .btn.danger").click();
  await page.waitForFunction(() => location.hash === "#/", null, {timeout: 8000}).catch(() => fail(`${vp.name}: purge did not return to dashboard`));
  ok(!s.mock.doc(PID), `${vp.name}: purged`);
  ok(dialogs.length === 0, `${vp.name}: no native dialog (${dialogs.join(" | ")})`);
  await s.close();
}

async function settings(vp, migrate) {
  const old = JSON.stringify({name: "Schreinerei Altgerät", contact: "Am Bach 2 · Ansbach", color: "#8a4b2a", logo: null});
  const s = await open(vp, migrate ? {init: `try { localStorage.setItem("werkstatt.company", ${JSON.stringify(old)}); } catch {}`} : {});
  const {page, base} = s;
  if (migrate) s.mock.state.settings.company.name = "";
  await page.goto(`${base}/werkstatt#/einstellungen`);
  await page.waitForSelector(".settings", {timeout: 10000});
  await settle(page);
  await shot(page, vp, "set");
  if (migrate) {
    await page.waitForTimeout(500);
    ok(s.mock.state.settings.company.name === "Schreinerei Altgerät", `${vp.name}: company migrated`);
    ok(await page.locator(".toast", {hasText: "Firmendaten von diesem Gerät übernommen."}).count() === 1, `${vp.name}: migration toast`);
  }
  for (const sec of ["firma", "team", "zugang", "kundenlink", "datenschutz", "darstellung", "system"]) {
    await page.goto(`${base}/werkstatt#/einstellungen/${sec}`);
    await page.waitForTimeout(500);
    await shot(page, vp, `set-${sec}`, sec === "firma" ? {primary: PRIMARY} : {});
  }
  // Datensicherung: Jetzt sichern
  await page.goto(`${base}/werkstatt#/einstellungen/datenschutz`);
  await page.waitForTimeout(400);
  ok(/Letzte Sicherung: .* \(812 MB\)/.test(await page.locator(".settings").textContent()), `${vp.name}: last backup line`);
  ok(/derselben Festplatte/.test(await page.locator(".settings").textContent()), `${vp.name}: same-disk warning`);
  const before = s.mock.state.backup.count;
  await page.locator("button", {hasText: "Jetzt sichern"}).click();
  await page.waitForTimeout(500);
  ok(s.mock.state.backup.count === before + 1, `${vp.name}: backup ran`);
  await shot(page, vp, "set-sicherung-fullpage", {fullPage: true});
  // Team: add a person
  await page.goto(`${base}/werkstatt#/einstellungen/team`);
  await page.locator("button", {hasText: "＋ Person hinzufügen"}).click();
  await page.fill(".sheet input[type=text]", "Anna");
  await page.locator(".sheet .btn.primary").click();
  await page.waitForTimeout(400);
  ok(s.mock.state.settings.team.some(t => t.name === "Anna"), `${vp.name}: team member added`);
  await shot(page, vp, "set-team-neu");
  await s.close();
}

async function kunde(vp) {
  // online: approve (a choice, not an order)
  let s = await open(vp);
  let reqs = [];
  s.page.on("request", r => reqs.push(new URL(r.url()).pathname));
  await s.page.goto(`${s.base}/k/${TOKEN}`);
  await s.page.waitForSelector(".card .ba", {timeout: 10000});
  await s.page.waitForTimeout(500);
  await shot(s.page, vp, "kunde");
  await shot(s.page, vp, "kunde-full", {fullPage: true});
  ok((await s.page.locator(".intro").textContent()).includes("Version 1 vom 16.10.2026"), `${vp.name}: version line`);
  await s.page.locator(".choice", {hasText: "Entwurf 2"}).click();
  await s.page.locator(".btn.primary", {hasText: "Entwurf freigeben"}).click();
  ok((await s.page.locator(".err").textContent()).includes("Bitte Ihren Namen angeben."), `${vp.name}: name required`);
  await s.page.fill("#kname", "Herr Beispiel");
  await s.page.locator(".btn.primary", {hasText: "Entwurf freigeben"}).click();
  ok((await s.page.locator(".respond").textContent()).includes("Entwurf 2 freigeben? Das ist noch keine Bestellung – wir melden uns mit dem Angebot."), `${vp.name}: confirm text`);
  await shot(s.page, vp, "kunde-confirm");
  await s.page.locator(".btn", {hasText: "Ja, Entwurf 2 freigeben"}).click();
  await s.page.waitForSelector(".done");
  ok((await s.page.locator(".done").textContent()).includes("Vielen Dank! Ihre Freigabe von Entwurf 2 ist bei"), `${vp.name}: thanks`);
  ok(s.mock.doc(PID).approved && s.mock.doc(PID).approved.variant_id === "vb222222", `${vp.name}: approval stored`);
  await shot(s.page, vp, "kunde-danke");
  await s.page.reload();
  await s.page.waitForSelector(".done");
  ok(/Entwurf 2 wurde am \d\d\.\d\d\.\d{4} freigegeben\./.test(await s.page.locator(".done").textContent()), `${vp.name}: answered state`);
  const own = new RegExp(`^/k/${TOKEN}(/(data|respond|pdf|zip|img/[^/]+))?$`);
  const bad = reqs.filter(p => !own.test(p));
  ok(!bad.length && !s.foreign.length, `${vp.name}: customer page requests only its own /k/ URLs (${[...bad, ...s.foreign].join(", ")})`);
  ok(s.dialogs.length === 0, `${vp.name}: no native dialog`);
  await s.close();

  // change request
  s = await open(vp);
  await s.page.goto(`${s.base}/k/${TOKEN}`);
  await s.page.waitForSelector(".respond");
  await s.page.fill("#kname", "Frau Beispiel");
  await s.page.fill("#kmsg", "Bitte die Türen in Salbeigrün.");
  await s.page.locator(".btn", {hasText: "Änderung wünschen"}).click();
  ok((await s.page.locator(".respond").textContent()).includes("Änderungswunsch senden?"), `${vp.name}: change confirm`);
  await s.page.locator(".btn", {hasText: "Ja, senden"}).click();
  await s.page.waitForSelector(".done");
  ok((await s.page.locator(".done").textContent()).includes("Wir melden uns mit einem überarbeiteten Entwurf."), `${vp.name}: change thanks`);
  ok(s.mock.doc(PID).versions[0].response.comment === "Bitte die Türen in Salbeigrün.", `${vp.name}: change stored`);
  await shot(s.page, vp, "kunde-aenderung");
  // expired / unknown link
  await s.page.goto(`${s.base}/k/tokExpired000000000000000000000001`);
  await s.page.waitForSelector(".center h1");
  ok((await s.page.textContent("body")).includes("Dieser Link ist nicht mehr gültig."), `${vp.name}: expired text`);
  await shot(s.page, vp, "kunde-abgelaufen");
  await s.close();
}

async function insecure() {
  const vp = VIEWPORTS.phone;
  const s = await open(vp, {insecure: true});
  await s.page.goto(`${s.base}/werkstatt#/p/${PID}/auftrag`);
  await s.page.waitForSelector(".auftrag", {timeout: 10000});
  await s.page.locator("button", {hasText: "Positionstext kopieren"}).click();
  await s.page.waitForTimeout(400);
  const copied = await s.page.locator(".toast", {hasText: "Text kopiert"}).count();
  const shown = await s.page.locator(".sheet .copy-area").count();
  ok(copied + shown > 0, "insecure: copy fallback (execCommand or text sheet)");
  if (shown) ok((await s.page.locator(".sheet .copy-area").inputValue()).includes("Visualisierung Vorgang 2026-002"), "insecure: positions text in sheet");
  await shot(s.page, vp, "insecure-kopieren");
  (await checkSecureFallbacks(s.page)).forEach(fail);
  await s.close();
}

for (const [key, vp] of Object.entries(VIEWPORTS)) {
  if (only.length && !only.includes(key) && !only.includes(vp.name)) continue;
  console.log(`— ${vp.name}`);
  for (const [name, fn] of [["dashboard", dashboard], ["auftrag", auftrag], ["settings", v => settings(v, key === "desktop")], ["kunde", kunde]]) {
    try { await fn(vp); } catch (e) { fail(`${vp.name} ${name}: ${e.message.split("\n")[0]}`); }
  }
}
if (!only.length || only.includes("insecure")) { console.log("— insecure origin"); try { await insecure(); } catch (e) { fail(`insecure: ${e.message.split("\n")[0]}`); } }
await stopServer();
console.log(problems.length ? `\n${problems.length} problem(s)` : "\nall checks passed");
process.exit(problems.length ? 1 : 0);
