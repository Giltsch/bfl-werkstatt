// Step ④ Berechnen: what will be computed, how many, which quality (live minutes), queue, caption notice, start/cancel.
import { h, toast, debounce, toggleRow as uiToggle, confirmDialog, jobProgress, jobLine, fmt, img, api } from "./ui.js";
import { ensureStyle, aheadLine } from "./present.js";
import { wideList } from "./editor.js";

const CSS = `
.bc{display:flex;flex-direction:column;gap:var(--gap);max-width:760px;margin:0 auto;padding:4px 0 24px}
.bc h2{margin:0;font-size:20px}.bc .card,.bc .details{margin:0}.bc h3{margin:0 0 8px;font-size:17px}
.bc .card{display:flex;flex-direction:column;gap:6px}
.bc .muted{color:var(--muted);font-size:14px;margin:0}
.bc .bc-warn{margin:4px 0 0;padding-left:20px;color:var(--warn)}
.bc .bc-items{margin:0;padding:0;list-style:none;display:flex;flex-direction:column;gap:6px}
.bc .bc-items li{font-size:16px;line-height:1.4}.bc .bc-items b{font-size:17px}
.bc .bc-ok{color:var(--ok);font-weight:600}.bc .bc-check{gap:8px}.bc .bc-base{gap:10px}.bc .bc-base-img{width:100%;max-height:220px;object-fit:contain;border-radius:10px;background:var(--bg2,#0001)}
.bc .bc-q{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.bc .bc-opt{display:flex;flex-direction:column;align-items:flex-start;gap:2px;min-height:72px;padding:12px;border:2px solid var(--line);border-radius:var(--r);background:var(--panel);color:var(--text);font:inherit;text-align:left;cursor:pointer}
.bc .bc-opt b{font-size:16px}.bc .bc-opt span{color:var(--muted);font-size:14px}
.bc .bc-opt[aria-checked=true]{border-color:var(--accent);background:var(--accent-soft)}
.bc .bc-count{display:grid;grid-template-columns:repeat(4,1fr);gap:8px}
.bc .bc-count button{min-height:52px;font-size:18px}
.bc .bc-count button[aria-checked=true]{border-color:var(--accent);background:var(--accent);color:var(--accent-ink)}
.bc .bc-notice{border-left:4px solid var(--warn)}
.bc .bc-run{border-left:4px solid var(--accent)}
.bc .bc-job{display:flex;flex-direction:column;gap:6px;padding:6px 0}
.bc .bc-job+.bc-job{border-top:1px solid var(--line)}
.bc .bc-bar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden}.bc .bc-bar i{display:block;height:100%;background:var(--accent);transition:width .4s}
.bc .bc-adv{display:flex;flex-direction:column;gap:8px;padding-top:8px}
.bc textarea{width:100%;box-sizing:border-box}
@media (max-width:420px){.bc .bc-q{grid-template-columns:1fr}}
`;
const ACTIVE = new Set(["queued", "running"]);
export const mins = s => Math.max(1, Math.round((+s || 0) / 60));
export const activeJobs = jobs => (jobs || []).filter(j => ACTIVE.has(j.state));
export const designPieces = doc => (doc?.pieces || []).filter(p => p.type || (p.text || "").trim());
const placed = p => ["2d", "3d"].includes(p.placement?.mode) && !!p.placement.box;
const liveVariants = doc => { const del = new Set(doc?.deleted_variants || []); return (doc?.variants || []).filter(v => !del.has(v.id)); };
const uniq = a => [...new Set(a.filter(Boolean))];

/** {line: "2 Möbel · Eiche natur geölt · grifflos (Push)", scope: "Rest des Fotos bleibt unverändert" …} */
export function summary(doc, catalog = {}) {
  const ps = designPieces(doc), mats = catalog.materials || {}, hds = catalog.handles || {};
  const woods = uniq(ps.map(p => p.material === "custom" ? (p.material_custom || "").trim() : mats[p.material]?.de || p.material_label));
  const grips = uniq(ps.map(p => hds[p.handles]?.de || p.handles_label));
  const line = [`${ps.length} Möbel`, woods.join(", "), grips.join(", ")].filter(Boolean).join(" · ");
  const scope = !doc?.room ? "Freie Visualisierung (ohne Raumfoto)"
    : protectState(doc).effective ? "Rest des Fotos bleibt unverändert" : "Ganzes Bild wird neu gezeichnet";
  const types = catalog.pieces || {};
  const items = ps.map(p => {
    const dims = [p.w, p.h, p.d].some(Boolean) ? `${p.w || "–"} × ${p.h || "–"} × ${p.d || "–"} cm` : "Maße fehlen";
    const wood = p.material === "custom" ? (p.material_custom || "").trim() : mats[p.material]?.de || p.material_label;
    const hang = +p.elev > 0 ? `hängt ${p.elev} cm über dem Boden` : "";
    return { name: types[p.type]?.de || p.label || "Möbel", rest: [dims, wood, hang].filter(Boolean).join(" · "), placed: placed(p) };
  });
  return { line, scope, count: ps.length, items };
}
/** Plain-language state of „Rest des Fotos unverändert lassen“ (mirrors workshop.build_room_job's keep rule). */
export function protectState(doc) {
  const on = doc?.protect !== false;
  if (!on) return { effective: false, text: "Aus: das ganze Bild darf neu gezeichnet werden." };
  if ((doc.room_changes || "").trim()) return { effective: false, text: "Wirkt nicht, solange „Raum selbst ändern“ ausgefüllt ist." };
  if (designPieces(doc).some(p => !placed(p))) return { effective: false, text: "Wirkt erst, wenn alle Möbel platziert sind." };
  return { effective: true, text: "Außerhalb der Möbel bleibt das Foto pixelgenau erhalten." };
}
/** Queue line before starting (§16.8): own active jobs count too, the round-robin wait comes from the plan. */
export function queueLine(plan, queue = {}, meId = null) {
  const jobs = activeJobs(queue.jobs);
  const wait = queue.wait_s ?? plan?.wait_s ?? 0;   // the live queue (sync, every 3 s) beats the dry run made when the step opened
  if (!jobs.length || wait < 15) return "Rechner frei – Start sofort.";
  const who = uniq(jobs.slice(0, 2).map(j => `${j.owner && j.owner === meId ? "Sie" : j.owner_name || "Jemand"} – ${j.project_title || j.label || "Auftrag"}`)).join(", ");
  return `Vor Ihnen: ${jobs.length} ${jobs.length === 1 ? "Auftrag" : "Aufträge"} (${who}${jobs.length > 2 ? ", …" : ""}) – Start in ca. ${mins(wait)} Min.`;
}
/** Beschreibungs-KI notice shown above the primary — only when the job needs a caption and the service is not up. */
export function captionNotice(plan, pup) {
  const c = plan?.caption;
  if (c && (c.backend === "cache" || c.backend === "reuse")) return null;
  if (c && c.backend === "template" && c.state === "up") return null;  // template by design (no caption needed)
  const state = c?.state || pup?.state || "up";
  if (state === "starting") return "Beschreibungs-KI startet gerade. Andere Berechnungen laufen solange weiter; startet sie nicht rechtzeitig, werden die Bilder ohne sie berechnet (Qualität evtl. geringer).";
  if (state === "down") return "Beschreibungs-KI ist aus – die Bilder werden ohne sie berechnet (Qualität evtl. geringer).";
  return null;
}
/** Room analysis in progress on this device (state kept by step ①: analysisOf(doc).phase). */
export function isAnalyzing(doc, view, store, raum) {
  if (!doc?.room) return false;
  const a = raum?.analysisOf?.(doc);
  return a ? a.phase === "upload" || a.phase === "analyzing" : !!view?.analyzing;
}
/** Primary of step ④ (§10.6). */
export function primaryOf({ doc, jobs, analyzing, estimate }) {
  if (activeJobs(jobs).length) return { label: "Zum Ergebnis", disabled: false, reason: null, kind: "result" };
  const label = estimate ? `Bilder berechnen (${fmt.span(estimate)})` : "Bilder berechnen";
  if (!designPieces(doc).length) return { label, disabled: true, reason: "Erst ein Möbel auswählen (Schritt 2).", kind: "start" };
  if (analyzing) return { label, disabled: true, reason: "Raum wird noch vermessen …", kind: "start" };
  return { label, disabled: false, reason: null, kind: "start" };
}
const jobPct = j => Math.round(jobProgress(j) * 100);
const DESIGN = /^(pieces|room|room_size|removes|geometry|calibration|protect|tidy|guide|mockup|room_changes|setting|sample|count|quality|judge)\b/;

export default {
  id: "berechnen", title: "Berechnen",
  state(doc, view, store) {
    const done = liveVariants(doc).some(v => v.kind !== "render");
    return { done, available: true, reason: null };
  },
  mount(el, sctx) {
    ensureStyle("bc-style", CSS);
    const store = sctx.store, unsub = [];
    const plans = { entwurf: null, fein: null };
    let starting = false, seq = 0;
    const doc = () => store.doc;
    const quality = () => doc().quality === "fein" ? "fein" : "entwurf";
    const jobs = () => store.projectJobs?.() || [];
    const kind = () => doc().room ? "room" : "free";

    const sumLine = h("p", {}), scope = h("p", { class: "muted" }), warns = h("ul", { class: "bc-warn" });
    const items = h("ul", { class: "bc-items" }), baseCard = h("div", { class: "card bc-base" });
    const mockCard = h("details", { class: "card details bc-mock" });
    const checkCard = h("div", { class: "card bc-check", "aria-live": "polite" });
    let check = { key: null, state: "idle", res: null };
    const baseOff = () => !!sctx.view?.baseOff;
    const countBtns = [1, 2, 3, 4].map(n => h("button", { type: "button", class: "btn secondary", role: "radio", "aria-label": `${n} ${n === 1 ? "Vorschlag" : "Vorschläge"}`,
      on: { click: () => store.set("count", n, { now: true }) } }, String(n)));
    const qOpt = (q, title) => {
      const min = h("span", {}, "ca. … Min.");
      const b = h("button", { type: "button", class: "bc-opt", role: "radio", on: { click: () => store.set("quality", q, { now: true }) } }, h("b", {}, title), min);
      b._min = min; return b;
    };
    const qE = qOpt("entwurf", "Entwurf (schneller)"), qF = qOpt("fein", "Fein (für den Kunden)");
    const qHint = h("p", { class: "muted" }, "Zum Ausprobieren: Entwurf · Für den Kunden: Fein");
    const qLine = h("p", { class: "bc-queue", "aria-live": "polite" });
    const etaLine = h("p", { class: "muted", style: "margin:8px 0 0" });   // „Bilder in ca. 3½ Min., Bewertung ca. 30 Sek. danach“
    const notice = h("div", { class: "card bc-notice", role: "status" });
    const run = h("div", { class: "card bc-run", "aria-live": "polite" });

    const toggles = [];
    const toggleRow = (path, title, desc, get = () => !!pathGet(doc(), path)) => {
      const b = uiToggle({ title: typeof title === "function" ? title() : title, text: typeof desc === "function" ? desc() : desc,
                           checked: get(), onChange: v => store.set(path, v, { now: true }) });
      toggles.push(() => {
        b.set(get());
        if (typeof title === "function") b.querySelector(".tr-title").textContent = title();
        if (typeof desc === "function") b.setText(desc());
      });
      return b;
    };
    const retryDesc = () => {
      const p = plans[quality()], per = p?.estimate_s ? p.estimate_s / Math.max(1, doc()?.count || 1) : 60;
      return `Bei schlechtem Ergebnis einmal neu rechnen (+ ca. ${mins(per)} Min.)`;
    };
    const changes = h("textarea", { rows: "2", placeholder: "z. B. Wand dahinter dunkelgrün streichen" });
    const setting = h("textarea", { rows: "2", placeholder: "z. B. heller Altbau-Flur mit Dielenboden" });
    unsub.push(store.bind(changes, "room_changes"), store.bind(setting, "setting"));
    const roomOnly = [], noRoomOnly = [], judgeOnly = [];
    const adv = h("div", { class: "bc-adv" },
      r(roomOnly, toggleRow("protect", "Rest des Fotos unverändert lassen", () => protectState(doc()).text, () => doc().protect !== false)),
      r(roomOnly, toggleRow("tidy", "Herumliegendes entfernen (Papiere, Kabel …)", "", () => doc().tidy !== false)),
      r(roomOnly, h("label", { class: "field" }, h("span", {}, "Raum selbst ändern (z. B. Wand streichen)"), changes)),
      r(noRoomOnly, h("label", { class: "field" }, h("span", {}, "Umgebung"), setting)),
      r(roomOnly, toggleRow("mockup", "Nach dem 3D-Modell bauen", "Die KI hält sich an das vermessene Modell: Größe, Lage, Türen und Fächer. Aus: freier, aber ungenauer.",
        () => doc().mockup !== false)),
      toggleRow("judge.enabled", "KI-Qualitätsprüfung", "", () => doc().judge?.enabled !== false),
      r(judgeOnly, toggleRow("judge.auto_retry", retryDesc, "", () => !!doc().judge?.auto_retry)));
    function r(list, node) { list.push(node); return node; }
    const details = h("details", { class: "details card" }, h("summary", {}, "Erweiterte Einstellungen"), adv);
    if (sctx.view?.advOpen) details.open = true;
    details.addEventListener("toggle", () => { sctx.view.advOpen = details.open; sctx.saveView?.(); });

    el.replaceChildren(h("div", { class: "bc" },
      h("div", { class: "card" }, h("h2", {}, "Bilder berechnen"), items, sumLine, scope, warns),
      checkCard, baseCard, mockCard, run,
      h("div", { class: "card" }, h("h3", {}, "Wie viele Vorschläge?"), h("div", { class: "bc-count", role: "radiogroup", "aria-label": "Wie viele Vorschläge?" }, ...countBtns)),
      h("div", { class: "card" }, h("h3", {}, "Qualität"), h("div", { class: "bc-q", role: "radiogroup", "aria-label": "Qualität" }, qE, qF), qHint, etaLine),
      qLine, notice, details));

    function render() {
      const d = doc(); if (!d) return;
      const s = summary(d, store.catalog || {});
      sumLine.textContent = s.count ? "" : "Noch kein Möbel ausgewählt.";
      sumLine.hidden = !!s.count;
      items.replaceChildren(...(s.items || []).map(it => h("li", {}, h("b", {}, it.name), " ", h("span", {}, it.rest),
        it.placed ? null : h("span", { class: "muted" }, " · noch nicht platziert"))));
      renderBase(); renderMock(); renderCheck();
      scope.textContent = s.scope;
      const plan = plans[quality()];
      warns.replaceChildren(...(plan?.warnings || []).map(w => h("li", {}, w)));
      warns.hidden = !warns.children.length;
      const c = Math.max(1, Math.min(4, d.count || 1));
      countBtns.forEach((b, i) => b.setAttribute("aria-checked", String(i + 1 === c)));
      for (const [b, q] of [[qE, "entwurf"], [qF, "fein"]]) {
        b.setAttribute("aria-checked", String(quality() === q));
        b._min.textContent = plans[q]?.estimate_s ? fmt.span(plans[q].estimate_s) : "ca. … Min.";
      }
      etaLine.textContent = plan?.estimate_text || "";
      etaLine.hidden = !plan?.estimate_text;

      roomOnly.forEach(n => { n.hidden = !d.room; });
      noRoomOnly.forEach(n => { n.hidden = !!d.room; });
      judgeOnly.forEach(n => { n.hidden = d.judge?.enabled === false; });
      toggles.forEach(f => f());
      renderQueue();
    }
    async function runCheck() {   // KI-Plancheck of the 3d template (window, door, radiator, floating) — once per template
      const p = plans[quality()], i = (p?.roles || []).indexOf("guide");
      const key = i >= 0 ? p.refs[i] : null;
      if (key === check.key) return;
      check = { key, state: key ? "busy" : "idle", res: null };
      renderCheck();
      if (!key) return;
      try {
        const r = await api("POST", `/api/workshop/${doc().id}/check_plan`, {});
        if (check.key !== key) return;
        check = { key, state: r?.status === "ok" ? "done" : r?.status === "none" ? "idle" : "off", res: r };
      } catch { if (check.key === key) check = { key, state: "off", res: null }; }
      renderCheck();
    }
    const checkProblems = () => (check.state === "done" ? (check.res.pieces || []).filter(x => !x.ok) : []);
    function renderCheck() {
      checkCard.hidden = check.state === "idle";
      if (checkCard.hidden) return;
      if (check.state === "busy") return checkCard.replaceChildren(h("h3", {}, "KI prüft den Plan …"),
        h("p", { class: "muted" }, "Fenster, Türen, Heizkörper – dauert ca. 15 Sek. Sie können trotzdem schon starten."));
      if (check.state === "off") return checkCard.replaceChildren(h("p", { class: "muted", style: "margin:0" }, "KI-Plancheck gerade nicht verfügbar."));
      const bad = checkProblems();
      if (!bad.length) return checkCard.replaceChildren(h("p", { class: "bc-ok", style: "margin:0" }, "✓ KI-Plancheck: keine Probleme mit Fenstern, Türen oder Heizkörpern erkannt."));
      checkCard.replaceChildren(h("h3", {}, "⚠ KI-Plancheck: bitte prüfen"),
        h("ul", { class: "bc-warn" }, ...bad.map(x => h("li", {}, h("b", {}, `${x.label}: `), x.problems.join(" "), x.fix ? h("span", { class: "muted" }, ` → ${x.fix}`) : null))),
        h("button", { type: "button", class: "btn secondary", on: { click: () => sctx.go ? sctx.go("platzieren") : history.back() } }, "Zu Schritt 3 (Platzieren)"));
    }
    function renderMock() {   // the 3d template the AI builds from: wrong spot or door count is visible before starting
      const p = plans[quality()], i = (p?.roles || []).indexOf("guide");
      mockCard.hidden = i < 0;
      if (i < 0) return;
      const src = img(p.refs[i]);
      if (mockCard.dataset.src === src) return;
      mockCard.dataset.src = src;
      mockCard.replaceChildren(h("summary", {}, "3D-Vorlage für die KI ansehen"),
        h("p", { class: "muted" }, "So baut die KI: Größe, Lage, Türen und Fächer kommen aus diesem Modell. Stimmt etwas nicht, in Schritt 2 oder 3 korrigieren."),
        h("img", { src, alt: "3D-Vorlage der geplanten Möbel im Foto", class: "bc-base-img", style: "max-height:420px" }));
    }
    function renderBase() {   // „baut auf Vorschlag … auf“: earlier furniture stays, only the new piece is drawn
      const b = plans[quality()]?.base, off = baseOff();
      baseCard.hidden = !b && !off;
      if (baseCard.hidden) return;
      const sw = uiToggle({ title: "Auf dem gewählten Vorschlag aufbauen", checked: !off,
        text: b ? `${b.kept.join(", ")} ${b.kept.length === 1 ? "bleibt" : "bleiben"} genau so – neu gezeichnet wird nur: ${b.new.join(", ")}.`
          : "Aus: alle Möbel werden neu gezeichnet.",
        onChange: v => { sctx.view.baseOff = !v; sctx.saveView?.(); estimate(); render(); } });
      baseCard.replaceChildren(...[b && !off ? h("img", { src: img(b.image), alt: "Vorschlag, auf dem aufgebaut wird", class: "bc-base-img" }) : null, sw].filter(Boolean));
    }
    function renderQueue() {
      const act = activeJobs(jobs());
      run.hidden = !act.length;
      if (act.length) run.replaceChildren(h("h3", {}, "Berechnung läuft …"), ...act.map(j => {
        const mine = !j.owner || j.owner === store.ownerKey?.();
        return h("div", { class: "bc-job" },
          h("span", {}, `${mine ? "" : `${j.owner_name || "Jemand"} berechnet: `}${j.label || "Vorschläge"}`),
          h("span", { class: "muted" }, jobLine(j)),
          aheadLine(j) ? h("span", { class: "muted" }, aheadLine(j)) : null,
          h("div", { class: "bc-bar", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(jobPct(j)) }, h("i", { style: `width:${jobPct(j)}%` })),
          j.state === "cancelling" ? null : h("button", { type: "button", class: "btn secondary", on: { click: e => cancel(j, e.currentTarget) } }, "Abbrechen"));
      }));
      qLine.textContent = act.length ? "" : queueLine(plans[quality()], store.queue || {}, store.ownerKey?.());
      qLine.hidden = !!act.length;
      const n = act.length ? null : designPieces(doc()).length ? captionNotice(plans[quality()], store.pup?.()) : null;
      notice.replaceChildren(n ? h("p", { style: "margin:0" }, n) : "");
      notice.hidden = !n;
    }
    async function cancel(j, btn) {
      btn.disabled = true; btn.textContent = "Berechnung wird abgebrochen …";
      const reset = () => { btn.disabled = false; btn.textContent = "Abbrechen"; };
      try { if ((await store.cancel(j.id)) == null) reset(); } catch (e) { if (!e?.handled) toast(e.message || "Abbrechen fehlgeschlagen", { kind: "err" }); reset(); }
    }
    const estimate = debounce(async () => {
      const my = ++seq;
      if (!designPieces(doc()).length) { plans.entwurf = plans.fein = null; return render(); }
      const k = kind();
      const extra = baseOff() ? { base: false } : {};
      const res = await Promise.all(["entwurf", "fein"].map(q => store.dryRun({ kind: k, quality: q, ...extra }).catch(() => null)));
      if (my !== seq) return;
      plans.entwurf = res[0]?.plan || res[0]; plans.fein = res[1]?.plan || res[1];
      render(); sctx.refresh?.(); runCheck();
    }, 500);

    async function start() {
      if (starting) return;
      const wide = wideList(doc(), store.catalog);
      if (wide.length && !await confirmDialog({ title: "Breiter als die Wand", ok: "Trotzdem berechnen",
        text: `${wide.join(", ")} – ${wide.length === 1 ? "das Möbel ist" : "die Möbel sind"} breiter als die Wand. Trotzdem berechnen? (In Schritt 2 oder 3 lässt sich die Breite mit einem Tipp anpassen.)` })) return;
      const bad = checkProblems();
      if (bad.length && !await confirmDialog({ title: "Plan hat Probleme", ok: "Trotzdem berechnen",
        text: `${bad.map(x => `${x.label}: ${x.problems.join(" ")}`).join(" · ")} – trotzdem berechnen?` })) return;
      starting = true; sctx.refresh?.();
      try {
        const b = plans[quality()]?.base;
        const r = await store.generate({ kind: kind(), base: baseOff() ? false : b ? b.variant : false });
        if (r?.job) { const pl = r.plan?.warnings || []; if (pl.length) toast(pl.join(" · ")); sctx.next(); }
      } catch (e) { if (!e?.handled && !["changed", "running"].includes(e?.code)) toast(e.message || "Start fehlgeschlagen", { kind: "err" }); }
      finally { starting = false; sctx.refresh?.(); }
    }
    let raum = null;  // step ① owns the analysis state
    import("./step-raum.js").then(m => { raum = m; sctx.refresh?.(); }).catch(() => {});
    render(); estimate();
    return {
      unmount() { unsub.forEach(f => { try { f?.(); } catch {} }); seq++; },
      onDoc(evt) { render(); if (!evt?.paths || evt.paths.some(p => DESIGN.test(p))) estimate(); },
      onQueue() { renderQueue(); },
      primary() {
        const p = primaryOf({ doc: doc(), jobs: jobs(), analyzing: isAnalyzing(doc(), sctx.view, store, raum), estimate: plans[quality()]?.estimate_s });
        if (p.kind === "result") return { label: p.label, disabled: false, reason: null, run: () => sctx.next() };
        return { label: starting ? "Wird gestartet …" : p.label, disabled: p.disabled || starting, reason: p.reason, run: start };
      },
    };
  },
};
function pathGet(o, path) { return path.split(".").reduce((a, k) => a?.[k], o); }
