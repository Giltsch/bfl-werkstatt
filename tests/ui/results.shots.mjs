// Screenshot walk for steps ④/⑤, Versand, Freigabe and Präsentation at all five viewports (spec §13 acceptance).
//   node tests/ui/results.shots.mjs [phone,desktop,…]   → shots in SCRATCH/shots-v3/res-*.png, exit 1 on any violation
import {launch, checkPage, checkSecureFallbacks, trackRequests, VIEWPORTS, stopServer} from "./check.mjs";
import {installMock} from "./mock_api.mjs";

const PID = "a1b2c3d4e5f6";            // doc-full: 3 proposals (template / 8 steps), version 1 with an XSS change request
const XSS_COMMENT = "<img src=x onerror=alert(1)>";
const PRIMARY = "#ws-actions .btn.primary";
const only = (process.argv[2] || "").split(",").filter(Boolean);
const problems = [];
const fail = msg => { problems.push(msg); console.log("  ✗", msg); };
const ok = (cond, msg) => { if (!cond) fail(msg); };

async function primaryText(page) { return (await page.locator(PRIMARY).first().textContent({timeout: 8000}).catch(() => "")).trim(); }
async function waitPrimary(page, re, ms = 10000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { if (re.test(await primaryText(page))) return true; await page.waitForTimeout(150); }
  fail(`primary did not match ${re}: „${await primaryText(page)}“`);
  return false;
}
const shot = async (page, vp, name, opts = {}) => {
  await page.waitForTimeout(350);  // sheet/dialog entry animations
  const v = await checkPage(page, `res-${vp.name}-${name}`, opts);
  v.forEach(fail);
};
const closeTop = page => page.keyboard.press("Escape");
const sheetBtn = (page, label) => page.locator(".sheet .sheet-actions button, .dialog .dialog-actions button", {hasText: label}).last();

async function walk(vp) {
  console.log(`— ${vp.name}`);
  const {context, base, close} = await launch({viewport: vp});
  const mock = await installMock(context, {scenario: "default", jobSeconds: 6});
  const page = await context.newPage();
  const dialogs = [];
  page.on("dialog", d => { dialogs.push(d.message()); d.dismiss().catch(() => {}); });
  page.on("pageerror", e => fail(`${vp.name}: page error ${e.message}`));
  const foreign = trackRequests(page, base);
  const go = async step => {
    const url = `${base}/werkstatt#/p/${PID}/${step}`;
    if (page.url() === url) await page.reload(); else await page.goto(url);
    await page.waitForSelector(step === "berechnen" ? ".bc" : ".er", {timeout: 10000});
  };

  // ④ Berechnen: summary, live minutes, queue line
  await go("berechnen");
  await waitPrimary(page, /^Bilder berechnen \(ca\. \d+ Min\.\)$/);
  ok(/Rechner frei – Start sofort\./.test(await page.locator(".bc-queue").textContent()), "queue line „Rechner frei“");
  ok((await page.locator(".bc-opt span").first().textContent()).includes("Min."), "quality option shows minutes");
  await shot(page, vp, "berechnen", {primary: PRIMARY});
  await page.locator(".details > summary", {hasText: "Erweiterte Einstellungen"}).click();
  await shot(page, vp, "berechnen-erweitert", {primary: PRIMARY});

  // pup down → notice before starting
  mock.addScenario("pupdown");
  await go("berechnen");
  await page.waitForSelector(".bc-notice:not([hidden])", {timeout: 8000}).catch(() => fail("caption notice missing (pupdown)"));
  ok(/Beschreibungs-KI ist aus/.test(await page.locator(".bc-notice").textContent()), "pupdown notice text");
  await shot(page, vp, "berechnen-pupdown", {primary: PRIMARY});
  mock.dropScenario("pupdown");

  // busy: Ben ahead
  mock.addScenario("busy");
  // the mock seeds Ben's job only at install time (scenario "busy"): add the same job now
  mock.state.jobs.push({id: "bbbbbb0001", kind: "generate", owner: "ben", owner_name: "Ben", project: "b0b0b0b0b0b1",
    project_title: "Küche Huber", label: "2 Vorschläge", count: 2, created: Date.now() / 1000 - 60, fixed: true, dedupe: "design"});
  await go("berechnen");
  await page.waitForFunction(() => /Vor Ihnen: 1 Auftrag \(Ben – Küche Huber\)/.test(document.querySelector(".bc-queue")?.textContent || ""), null, {timeout: 8000})
    .catch(() => fail("queue line with Ben ahead missing"));
  await shot(page, vp, "berechnen-busy", {primary: PRIMARY});

  // running (a colleague started the same) → 409 dialog → Trotzdem starten → ⑤ with placeholders
  mock.addScenario("running");
  await page.locator(PRIMARY).click();
  const force = page.locator(".dialog button, .sheet button", {hasText: "Trotzdem starten"}).first();
  await force.waitFor({timeout: 8000}).catch(() => fail("409 running dialog missing"));
  ok(await page.locator(".dialog, .sheet", {hasText: "Zur laufenden Berechnung"}).count() > 0, "dialog offers „Zur laufenden Berechnung“");
  await shot(page, vp, "running-dialog");
  await force.click().catch(() => {});
  mock.dropScenario("running");
  await page.waitForSelector(".er", {timeout: 10000}).catch(() => fail("did not navigate to ⑤ after start"));
  await page.waitForSelector(".er-ph", {timeout: 8000}).catch(() => fail("no placeholder tiles while running"));
  ok(await page.locator(".er-tile .badge", {hasText: "ohne Beschreibungs-KI"}).count() > 0, "badge „ohne Beschreibungs-KI“");
  ok(await page.locator(".er-tile .badge", {hasText: "Entwurfsqualität"}).count() > 0, "badge „Entwurfsqualität“");
  await shot(page, vp, "ergebnis-running", {primary: PRIMARY});

  // back to ④: running card + Abbrechen
  await go("berechnen");
  await waitPrimary(page, /^Zum Ergebnis$/);
  await page.waitForSelector(".bc-run:not([hidden])", {timeout: 5000}).catch(() => fail("running card missing"));
  await shot(page, vp, "berechnen-laeuft", {primary: PRIMARY});
  const cancel = page.locator(".bc-run button", {hasText: "Abbrechen"});
  const n = await cancel.count();
  for (let i = n - 1; i >= 0; i--) {
    await cancel.nth(i).click().catch(() => {});
    const force2 = page.locator(".dialog button", {hasText: "Trotzdem abbrechen"});
    if (await force2.count()) await force2.click();
  }
  mock.dropScenario("busy");
  await page.waitForFunction(() => document.querySelector(".bc-run")?.hidden, null, {timeout: 8000}).catch(() => fail("cancel did not end the running card"));
  await waitPrimary(page, /^Bilder berechnen/);

  // ⑤ results: slider Vorher left, primary disabled until ★ (doc-full has va★ → unselect first)
  await go("ergebnis");
  await page.waitForFunction(() => (document.querySelector(".rs-cmp")?.getBoundingClientRect().width || 0) > 150, null, {timeout: 5000}).catch(() => fail("slider not sized"));
  const labs = await page.evaluate(() => {
    const l = document.querySelector(".rs-lab.l"), r = document.querySelector(".rs-lab.r");
    return l && r ? {l: l.textContent, r: r.textContent, order: l.getBoundingClientRect().left < r.getBoundingClientRect().left} : null;
  });
  ok(labs && labs.l === "Vorher" && labs.r === "Nachher" && labs.order, `slider shows Vorher left (${JSON.stringify(labs)})`);
  await page.locator(".er-props .er-tile:has(.er-star)").first().click();
  await page.locator(".er-panel button", {hasText: "★ Ausgewählt"}).click();
  await waitPrimary(page, /^An Kunden senden …$/);
  ok(await page.locator(PRIMARY).isDisabled(), "primary disabled without ★");
  ok((await page.locator("#ws-actions").textContent()).includes("Erst einen Entwurf mit ★ für den Kunden auswählen."), "reason line without ★");
  await shot(page, vp, "ergebnis-ohne-auswahl", {primary: PRIMARY});
  // choose the template proposal (vb) and the first one
  await page.locator(".er-props .er-tile", {hasText: "ohne Beschreibungs-KI"}).first().click();
  await page.locator(".er-panel button", {hasText: "★ Für den Kunden auswählen"}).click();
  await page.waitForFunction(sel => !document.querySelector(sel)?.disabled, PRIMARY, {timeout: 5000}).catch(() => fail("primary stays disabled after ★"));
  await shot(page, vp, "ergebnis", {primary: PRIMARY});
  await page.evaluate(() => scrollTo(0, document.body.scrollHeight));
  await shot(page, vp, "ergebnis-unten", {primary: PRIMARY});
  await page.evaluate(() => scrollTo(0, 0));

  // wood swap sheet
  await page.locator(".er-panel button", {hasText: "Holz tauschen"}).click();
  await page.waitForSelector(".sheet", {timeout: 5000});
  await page.waitForFunction(() => /Holz tauschen \(ca\. \d+ Min\.\)/.test(document.querySelector(".sheet .sheet-actions")?.textContent || ""), null, {timeout: 5000}).catch(() => fail("wood sheet minutes"));
  await shot(page, vp, "holz-sheet");
  await closeTop(page);

  // change sheet prefilled from the hostile customer change request
  await page.locator(".er-banner button", {hasText: "Als Änderung übernehmen"}).click();
  await page.waitForSelector(".sheet textarea", {timeout: 5000});
  ok(await page.locator(".sheet textarea").inputValue() === XSS_COMMENT, "change text prefilled literally");
  ok(await page.locator("img[src='x']").count() === 0, "no <img> created from the customer comment");
  ok((await page.locator(".er-banner").textContent()).includes("<b>Herr X</b> wünscht eine Änderung: „<img src=x onerror=alert(1)>“"), "banner shows the raw text");
  await shot(page, vp, "aenderung-sheet");
  await closeTop(page);

  // Versand (LAN only): warning dialog (template proposal) → result sheet: LAN link in the texts + setup sentence
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".sheet .sd-tiles", {timeout: 5000});
  await shot(page, vp, "senden-sheet");
  await sheetBtn(page, "Für Kunden bereitstellen").click();
  await page.waitForSelector(".dialog .sd-warn", {timeout: 5000}).catch(() => fail("warning dialog missing"));
  ok((await page.locator(".dialog").textContent()).includes("wurde ohne Beschreibungs-KI berechnet – trotzdem senden?"), "template warning text");
  await shot(page, vp, "senden-warnung");
  await sheetBtn(page, "Trotzdem senden").click();
  await page.waitForSelector(".sheet h2:text('Version 2 ist bereit')", {timeout: 8000}).catch(() => fail("result sheet v2 missing"));
  const hrefs = await page.locator(".sheet a.btn").evaluateAll(as => as.map(a => a.getAttribute("href")));
  const mailHref = hrefs.find(h => h.startsWith("mailto:")), waHref = hrefs.find(h => h.startsWith("https://wa.me/"));
  ok(mailHref && waHref, "mail + WhatsApp buttons");
  ok(/%2Fk%2Ftok/.test(mailHref) && /%2Fk%2Ftok/.test(waHref), "customer link in WhatsApp/E-Mail");
  ok(/^https:\/\/wa\.me\/49\d+\?/.test(waHref) || await page.locator(".sheet input[type=tel]").count() === 1, "WhatsApp number from the order, else asked");
  ok(await page.locator(".sheet .sd-note a.btn", {hasText: "Kundenlink einrichten"}).count() === 1, "one way to set up the public link");
  ok(await page.locator(".sheet", {hasText: "Teilen geht hier nicht direkt"}).count() === 0, "no jargon about sharing");
  ok(await page.locator(".sheet a.btn", {hasText: "Kundenblatt (PDF) herunterladen"}).isVisible(), "download visible (no share on http)");
  await shot(page, vp, "versand-lan");
  // approval capture from the result sheet
  await sheetBtn(page, "Freigabe erfassen").click();
  await page.waitForSelector(".sheet h2:text('Freigabe erfassen')", {timeout: 5000});
  await page.locator(".sheet .sd-tile").first().click();
  await shot(page, vp, "freigabe-sheet");
  await sheetBtn(page, "Freigabe speichern").click();
  await page.waitForFunction(() => /freigegeben ✓/.test(document.querySelector(".er-banner")?.textContent || ""), null, {timeout: 8000}).catch(() => fail("approved banner missing"));
  await shot(page, vp, "ergebnis-freigegeben", {primary: PRIMARY});

  // public address → version 3 with link + QR
  ok(await primaryText(page) === "Weiter: Angebot & Auftrag", "after the approval the primary is the next step");
  mock.state.settings.public_base_url = "https://kunde.example.de";
  await go("ergebnis");
  await page.locator(".er-panel button", {hasText: "Neue Version senden"}).click();
  await page.waitForSelector(".sheet .sd-tiles", {timeout: 5000});
  await sheetBtn(page, "Für Kunden bereitstellen").click();
  const warn = sheetBtn(page, "Trotzdem senden");
  if (await warn.isVisible().catch(() => false)) await warn.click();
  await page.waitForSelector(".sheet h2:text('Version 3 ist bereit')", {timeout: 8000}).catch(() => fail("result sheet v3 missing"));
  ok(await page.locator(".sheet .sd-qr").count() === 1, "public: QR available");
  ok(await page.locator(".sheet .sd-note").count() === 0, "public: no setup sentence");
  ok((await page.locator(".sheet .sd-link").inputValue()).startsWith("https://kunde.example.de/k/"), "public link shown");
  const wa = await page.locator(".sheet a.btn", {hasText: "Per WhatsApp"}).getAttribute("href");
  ok(decodeURIComponent(wa).includes("https://kunde.example.de/k/"), "public link in WhatsApp text");
  await shot(page, vp, "versand-public");
  await closeTop(page);

  await page.locator(".er-props .er-tile").last().click();
  const star = page.locator(".er-panel button", {hasText: "★ Für den Kunden auswählen"});
  if (await star.count()) await star.click();
  // presentation → Dieser gefällt → approval preselected „vor Ort“
  await page.locator(".er-panel button", {hasText: "Präsentieren"}).click();
  await page.waitForSelector(".pr-ov", {timeout: 5000});
  await page.waitForTimeout(300);
  await shot(page, vp, "praesentation");
  const total = Number(((await page.locator(".pr-bot .n").textContent()).match(/von (\d+)/) || [])[1] || 0);
  ok(total >= 1, "presentation counter");
  if (total > 1) {
    await page.keyboard.press("ArrowRight");
    ok((await page.locator(".pr-bot .n").textContent()).startsWith("Entwurf 2 von"), "arrow key → next proposal");
  }
  await page.locator(".pr-ov button", {hasText: "Dieser gefällt"}).click();
  await page.waitForSelector(".sheet h2:text('Freigabe erfassen'), .dialog", {timeout: 5000}).catch(() => fail("approval after „Dieser gefällt“ missing"));
  const viaOk = await page.locator(".sheet .seg button[aria-checked=true]", {hasText: "vor Ort"}).count();
  const needsVersion = await page.locator(".dialog", {hasText: "noch keine Version"}).count();
  ok(viaOk === 1 || needsVersion === 1, "approval preselected „vor Ort“ (or version offer)");
  await shot(page, vp, "praesentation-gefaellt");
  await closeTop(page);

  ok(!dialogs.length, `no browser dialog (${dialogs.join(" | ")})`);
  ok(!foreign.length, `no foreign requests (${foreign.slice(0, 3).join(", ")})`);
  await close();
}

async function insecureRun() {
  console.log("— phone, non-secure origin");
  const vp = VIEWPORTS.phone;
  const {context, base, close} = await launch({viewport: vp, insecure: true});
  await installMock(context, {scenario: "default"});
  const page = await context.newPage();
  page.on("pageerror", e => fail(`insecure: page error ${e.message}`));
  await page.goto(`${base}/werkstatt#/p/${PID}/ergebnis`);
  await page.waitForSelector(".er", {timeout: 10000});
  await waitPrimary(page, /^An Kunden senden …$/);
  await page.locator(PRIMARY).click();
  await page.waitForSelector(".sheet .sd-tiles", {timeout: 5000});
  await sheetBtn(page, "Für Kunden bereitstellen").click();
  const w2 = sheetBtn(page, "Trotzdem senden");
  if (await w2.isVisible({timeout: 1500}).catch(() => false)) await w2.click();
  await page.waitForSelector(".sheet h2:text('Version 2 ist bereit')", {timeout: 8000}).catch(() => fail("insecure: result sheet missing"));
  ok(await page.locator(".sheet a.btn", {hasText: "Bilder und PDF (ZIP)"}).isVisible(), "insecure: ZIP download visible");
  ok(await page.locator(".sheet button", {hasText: "PDF teilen"}).count() === 0, "insecure: no share button");
  await page.locator(".sheet button", {hasText: "Link kopieren"}).click();
  await page.waitForTimeout(300);
  await shot(page, vp, "insecure-versand");
  (await checkSecureFallbacks(page)).forEach(fail);
  await close();
}

const vps = Object.values(VIEWPORTS).filter(v => !only.length || only.includes(v.name));
for (const vp of vps) {
  try { await walk(vp); } catch (e) { fail(`${vp.name}: ${e.message.split("\n").slice(0, 3).join(" | ")}`); }
}
if (!only.length || only.includes("insecure")) try { await insecureRun(); } catch (e) { fail(`insecure: ${e.message.split("\n")[0]}`); }
await stopServer();
console.log(problems.length ? `\n${problems.length} problem(s)` : "\nall checks passed");
process.exit(problems.length ? 1 : 0);
