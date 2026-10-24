// node tests/ui/kit.shots.mjs — the app frame (app.js/ui.css/store.js) with dummy views at all five viewports:
// dashboard, project, immersive step ③, identity sheet, first run, conflict dialog, offline banner, dock, Rechner chip
// in pupdown + queue sheet, and one run on a non-secure origin (copy/share fallbacks). Exit code 1 on any violation.
import {launch, checkPage, checkSecureFallbacks, trackRequests, stopServer, VIEWPORTS, SHOTS} from "./check.mjs";
import {installMock} from "./mock_api.mjs";

const FULL = "a1b2c3d4e5f6", NEW = "0e1e2e3e4e5e";
const PRIMARY = "#ws-actions .btn.primary";

// Dummy views stand in for dashboard.js / editor.js / auftrag.js / settings.js (owned by other implementers), so the
// frame is tested on its own. They use only the public ui.js/store contracts.
const DUMMY = `
import {h, icon, toggleRow, copyText, shareOrFallback, shareLinks, ratingOf, fmt, statusLabel, img, phoneLayout} from "./ui.js";
export default {mount(el, ctx) {
  const r = ctx.route, s = ctx.store;
  if (!r.pid) {
    el.append(h("h1", {}, "Aufträge"), h("div", {class: "chips scroll"}, ["Alle aktiven", "Anfrage", "Vor-Ort-Termin", "Entwurf", "Beim Kunden", "Freigegeben"].map((t, i) =>
      h("button", {class: "chip" + (i ? "" : " active"), type: "button"}, t, h("span", {class: "count"}, String(9 - i))))),
      h("div", {class: "field", style: "margin-top:12px"}, h("label", {for: "q"}, "Suchen"), h("input", {id: "q", type: "search", placeholder: "Kunde, Projekt oder Nummer suchen"})),
      h("ul", {class: "list"}, [["Esszimmer Sideboard", "Familie Beispiel · Ansbach", "kunde", "variant-1.jpg"], ["Küche Huber", "Huber", "entwurf", "variant-2.jpg"],
        ["Einbauschrank Flur", "Familie Müller", "anfrage", null]].map(([t, c, st, th]) => h("li", {}, h("a", {class: "list-item", href: "#/p/${FULL}"},
          th ? h("img", {class: "thumb", src: img(th), alt: ""}) : h("span", {class: "thumb", html: icon("image")}),
          h("span", {class: "li-main"}, h("span", {class: "li-title"}, t), h("span", {class: "li-sub"}, c)), h("span", {class: "badge info"}, statusLabel(st)))))),
      h("div", {class: "empty"}, h("span", {html: icon("search")}), h("p", {}, "Noch keine Aufträge. Legen Sie den ersten an – am besten direkt beim Kunden mit dem Handy.")));
    ctx.actions([h("button", {class: "btn primary", type: "button"}, "＋ Neuer Auftrag")]);
    return {unmount() {}};
  }
  let off = [];
  (async () => {
    const doc = await s.open(r.pid);
    const p = doc.pieces[0];
    if (r.step === "platzieren" && phoneLayout()) {
      ctx.immersive(true, {title: p.label || "Möbel"});
      el.className = "full";
      el.append(h("div", {class: "stage", style: "border-radius:0;height:calc(100svh - var(--top-h) - var(--footer-h) - 60px)"}, h("img", {src: img(doc.room || "room.png"), alt: "Raumfoto", style: "width:100%;height:100%;object-fit:contain"})),
        h("div", {class: "row", style: "padding:8px;gap:8px;flex-wrap:nowrap"}, ["An die Wand", "↻ 90° drehen", "Rückgängig", "Mehr …"].map(t => h("button", {class: "btn secondary", type: "button", style: "flex:1;padding:6px;font-size:14px"}, t))));
      ctx.actions([h("button", {class: "btn secondary", type: "button"}, "Zurück"), h("button", {class: "btn primary", type: "button"}, "Weiter: Berechnen")]);
      return;
    }
    const w = h("input", {id: "w", type: "number", inputMode: "numeric"});
    const t = h("input", {id: "t", type: "text"});
    const resp = (doc.versions[0] || {}).response;
    el.append(h("div", {class: "stepper"}, ["Raum", "Möbel", "Platzieren", "Berechnen", "Ergebnis & Kunde"].map((n, i) =>
      h("a", {class: "step" + (i === 1 ? " active" : i === 0 ? " done" : ""), href: "#"}, h("span", {class: "num"}, i === 0 ? "✓" : String(i + 1)), n))),
      h("div", {class: "two-col"}, h("div", {},
        h("div", {class: "card"}, h("h2", {}, "Was soll hin?"), h("div", {class: "tiles"}, ["Sideboard", "Esstisch", "Regalwand", "Einbauschrank"].map((n, i) =>
          h("button", {class: "tile", type: "button", "aria-pressed": String(i === 0), html: icon("home")}, n)))),
        h("div", {class: "card"}, h("div", {class: "field"}, h("label", {for: "t"}, "Titel"), t),
          h("div", {class: "fields-3"}, h("div", {class: "field"}, h("label", {for: "w"}, "Breite"), h("div", {class: "unit"}, w, h("span", {}, "cm"))),
            h("div", {class: "field"}, h("label", {}, "Höhe"), h("div", {class: "unit"}, h("input", {type: "number", value: "75"}), h("span", {}, "cm"))),
            h("div", {class: "field"}, h("label", {}, "Tiefe"), h("div", {class: "unit"}, h("input", {type: "number", value: "45"}), h("span", {}, "cm")))),
          h("div", {class: "seg", role: "group", "aria-label": "Qualität"}, h("button", {type: "button", "aria-pressed": "true"}, "Entwurf", h("small", {}, "ca. 2 Min.")),
            h("button", {type: "button", "aria-pressed": "false"}, "Fein", h("small", {}, "ca. 3 Min.")))),
        toggleRow({title: "Rest des Fotos unverändert lassen", text: "Außerhalb der Möbel bleibt das Foto pixelgenau erhalten.", checked: true}),
        h("details", {class: "details"}, h("summary", {}, "Erweiterte Einstellungen"), h("p", {}, "Weitere Optionen."))),
      h("div", {},
        h("div", {class: "card warn"}, h("p", {id: "xss"}, resp ? resp.name + " wünscht eine Änderung: „" + resp.comment + "“" : "")),
        h("div", {class: "card"}, h("h3", {}, "Vorschläge"), h("div", {class: "row"}, doc.variants.map(v => h("span", {class: "badge " + ratingOf(v.judge).tone}, ratingOf(v.judge).text))),
          h("div", {class: "btn-row", style: "margin-top:12px"},
            h("button", {class: "btn secondary", id: "copy", type: "button", on: {click: async () => { window.__copy = await copyText("Sideboard, Eiche, B 180 cm"); }}}, h("span", {html: icon("link")}), "Positionstext kopieren"),
            h("button", {class: "btn secondary", id: "share", type: "button", on: {click: async () => {
              const r2 = await shareOrFallback({title: "Entwürfe", text: "Ihre Entwürfe"});
              window.__share = r2;
              if (r2 === "fallback") { const l = shareLinks({subject: "Entwürfe", text: "Ihre Entwürfe"}); document.getElementById("fb").replaceChildren(
                h("a", {class: "btn ghost", href: l.whatsapp}, "Per WhatsApp"), h("a", {class: "btn ghost", href: l.mail}, "Per E-Mail")); }
            }}}, h("span", {html: icon("share")}), "Teilen …")),
          h("div", {id: "fb", class: "btn-row"})))));
    off.push(s.bind(w, "pieces." + p.id + ".w"), s.bind(t, "title"));
    ctx.actions([h("p", {class: "reason"}, "Erst ein Möbel auswählen (Schritt 2)."), h("button", {class: "btn secondary", type: "button"}, "Zurück"), h("button", {class: "btn primary", type: "button"}, "Weiter: Platzieren")]);
  })();
  return {unmount() { off.forEach(f => f()); }};
}};`;

const dummyRoutes = ["dashboard", "editor", "auftrag", "settings"].map(n => [`**/static/ws/${n}.js`,
  route => route.fulfill({status: 200, contentType: "text/javascript; charset=utf-8", body: DUMMY})]);

const all = [];
const VPS = [VIEWPORTS.phone, VIEWPORTS.phoneLandscape, VIEWPORTS.tablet, VIEWPORTS.tabletLandscape, VIEWPORTS.desktop];

async function session(vp, {scenario = "default", insecure = false, jobSeconds = 8} = {}) {
  const s = await launch({viewport: vp, insecure});
  const mock = await installMock(s.context, {scenario, routes: dummyRoutes, jobSeconds});
  const page = await s.context.newPage();
  const foreign = trackRequests(page, s.base);
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("dialog", d => { errors.push(`unexpected JS dialog: ${d.message()}`); d.dismiss(); });
  return {...s, mock, page, foreign, errors,
    async go(hash) { await page.goto(`${s.base}/werkstatt${hash}`); await page.waitForSelector(".hdr .rchip", {state: "attached"}); await page.waitForTimeout(350); },
    async done(tag) {
      if (foreign.length) all.push(`${tag}: foreign requests ${foreign.join(", ")}`);
      for (const e of errors) all.push(`${tag}: ${e}`);
      await s.close();
    }};
}

async function shot(page, name, opts) {
  const v = await checkPage(page, name, opts);
  all.push(...v);
  console.log(`${v.length ? "✗" : "✓"} ${name}${v.length ? "\n   " + v.join("\n   ") : ""}`);
}

function expect(cond, msg) { if (!cond) { all.push(msg); console.log("✗ " + msg); } }

for (const vp of VPS) {
  const s = await session(vp, {scenario: "busy"});
  await s.go("#/");
  await shot(s.page, `kit-${vp.name}-dashboard`, {primary: PRIMARY});
  await s.go(`#/p/${FULL}/moebel`);
  await s.page.waitForSelector("#w");
  await s.page.waitForSelector(".pbar .presence-line:not([hidden])");
  await shot(s.page, `kit-${vp.name}-project`, {primary: PRIMARY});
  const xss = await s.page.evaluate(() => ({text: document.getElementById("xss").textContent, imgs: document.querySelectorAll("#xss img, #xss b").length}));
  expect(xss.text.includes("<img src=x onerror=alert(1)>") && xss.imgs === 0, `${vp.name}: XSS fixture not rendered as text`);
  const pres = await s.page.textContent(".pbar .presence-line");
  expect(pres.includes("Ben (Tablet) ist gerade im Schritt „Platzieren“"), `${vp.name}: presence line „${pres}“`);
  if (vp.width < 700 || vp.name === "phone-land") {
    await s.go(`#/p/${FULL}/platzieren`);
    await s.page.waitForSelector("body.immersive .stage");
    await shot(s.page, `kit-${vp.name}-immersive`, {primary: PRIMARY});
    const m = await s.page.evaluate(() => ({hdr: document.querySelector(".hdr").getBoundingClientRect().height,
      secondaryVisible: [...document.querySelectorAll("#ws-actions > :not(.primary)")].some(e => e.getBoundingClientRect().height > 0),
      step: document.querySelector(".hdr-step").textContent, pbar: document.querySelector(".pbar").getBoundingClientRect().height,
      stage: document.querySelector(".stage").getBoundingClientRect(), vw: innerWidth}));
    expect(m.hdr === 44, `${vp.name}: immersive top row is ${m.hdr}px, not 44`);
    expect(!m.secondaryVisible, `${vp.name}: immersive footer shows more than the primary button`);
    expect(m.step.includes("Schritt 3/5"), `${vp.name}: immersive row lacks „Schritt 3/5“`);
    expect(m.pbar === 0, `${vp.name}: project bar visible in immersive mode`);
    expect(Math.round(m.stage.width) === m.vw, `${vp.name}: stage not full width (${m.stage.width} of ${m.vw})`);
    await s.page.click(".hdr .imm-more");
    await s.page.waitForSelector("text=Zu den Aufträgen");
    await shot(s.page, `kit-${vp.name}-immersive-menu`);
    await s.page.keyboard.press("Escape");
  }
  await s.done(vp.name);
}

// identity: picker, new person form, first run
for (const vp of [VIEWPORTS.phone, VIEWPORTS.desktop]) {
  let s = await session(vp, {scenario: "noident"});
  await s.go("#/");
  await s.page.waitForSelector(".identity-sheet");
  await shot(s.page, `kit-${vp.name}-identity`);
  await s.page.click("text=＋ Ich bin neu hier");
  await s.page.fill("#ws-id-name", "Anna Schreiner");
  await shot(s.page, `kit-${vp.name}-identity-new`);
  await s.page.click(".identity-sheet .btn.primary");
  await s.page.waitForSelector(".identity-sheet", {state: "detached"});
  const av = await s.page.textContent(".avatar-btn .avatar");
  expect(av.trim() === "AS", `${vp.name}: avatar after joining is „${av}“`);
  await s.done(`${vp.name}-identity`);
  s = await session(vp, {scenario: "firstrun"});
  await s.go("#/");
  await s.page.waitForSelector("text=Willkommen! Wie heißen Sie?");
  await shot(s.page, `kit-${vp.name}-firstrun`);
  await s.done(`${vp.name}-firstrun`);
}

// conflict dialog with readable values, offline banner, Rechner chip in pupdown, dock
for (const vp of [VIEWPORTS.phone, VIEWPORTS.tablet, VIEWPORTS.desktop]) {
  let s = await session(vp, {scenario: "conflict"});
  await s.go(`#/p/${FULL}/moebel`);
  await s.page.waitForSelector("#w");
  await s.page.fill("#w", "240");
  await s.page.locator("#t").focus();
  await s.page.waitForSelector(".dialog");
  const txt = await s.page.textContent(".dialog");
  expect(txt.includes("Ben hat „Breite (Sideboard)“ gerade auf 220 cm geändert. Sie hatten 240 cm eingetragen."), `${vp.name}: conflict text „${txt}“`);
  await shot(s.page, `kit-${vp.name}-conflict`);
  await s.page.click("text=Meins übernehmen");
  await s.page.waitForTimeout(400);
  expect(s.mock.doc(FULL).pieces[0].w === 240, `${vp.name}: „Meins übernehmen“ did not persist 240`);
  s.mock.setScenario("offline");
  await s.page.waitForSelector(".banner:not([hidden])", {timeout: 8000});
  await shot(s.page, `kit-${vp.name}-offline`, {primary: PRIMARY});
  const chip = await s.page.getAttribute(".rchip", "aria-label");
  expect(chip.startsWith("Keine Verbindung"), `${vp.name}: chip offline label „${chip}“`);
  await s.done(`${vp.name}-conflict`);

  s = await session(vp, {scenario: "busy,pupdown", jobSeconds: 120});
  await s.go(`#/p/${NEW}/moebel`);
  await s.page.waitForSelector("#w");
  await s.page.evaluate(async pid => { await fetch(`/api/workshop/${pid}/generate`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({kind: "room", count: 2})}); }, NEW);
  await s.go("#/");
  await s.page.waitForSelector(".dock-card", {timeout: 12000});
  await shot(s.page, `kit-${vp.name}-dock-pupdown`, {primary: PRIMARY});
  const cl = await s.page.getAttribute(".rchip", "aria-label");
  expect(/Beschreibungs-KI aus – Notbetrieb/.test(cl), `${vp.name}: chip lacks the Notbetrieb line („${cl}“)`);
  await s.page.click(".rchip");
  await s.page.waitForSelector(".sheet");
  await shot(s.page, `kit-${vp.name}-queue-sheet`);
  await s.page.keyboard.press("Escape");
  await s.page.click(".dock-card");
  await s.page.waitForFunction(pid => location.hash === `#/p/${pid}/ergebnis`, NEW);
  await s.done(`${vp.name}-dock`);
}

// dark theme once
{
  const s = await session(VIEWPORTS.phone, {scenario: "busy"});
  await s.page.addInitScript(() => localStorage.setItem("ws.theme", "dark"));
  await s.go(`#/p/${FULL}/moebel`);
  await s.page.waitForSelector("#w");
  await shot(s.page, "kit-phone-dark", {primary: PRIMARY});
  await s.done("dark");
}

// non-secure origin: copy/share fallbacks, never the secure-only APIs
for (const vp of [VIEWPORTS.phone, VIEWPORTS.desktop]) {
  const s = await session(vp, {insecure: true});
  await s.go(`#/p/${FULL}/moebel`);
  await s.page.waitForSelector("#copy");
  await s.page.click("#copy");
  await s.page.waitForFunction(() => window.__copy);
  const copy = await s.page.evaluate(() => window.__copy);
  if (copy === "shown") { await shot(s.page, `kit-${vp.name}-insecure-copysheet`); await s.page.keyboard.press("Escape"); }
  await s.page.click("#share");
  await s.page.waitForSelector("#fb a");
  await shot(s.page, `kit-${vp.name}-insecure-share`, {primary: PRIMARY});
  const v = await checkSecureFallbacks(s.page);
  all.push(...v.map(x => `${vp.name}-insecure: ${x}`));
  console.log(`${v.length ? "✗" : "✓"} kit-${vp.name}-insecure fallbacks (copy → ${copy})`);
  await s.done(`${vp.name}-insecure`);
}

await stopServer();
console.log(`\nshots in ${SHOTS}\n${all.length ? `${all.length} violation(s):\n` + all.join("\n") : "0 violations"}`);
process.exit(all.length ? 1 : 0);
