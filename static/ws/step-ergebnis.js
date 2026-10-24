// Step ⑤ Ergebnis & Kunde: slider (Vorher left), rated proposals + placeholders, choose ★, adjust, hand to the customer.
import { h, api, img, toast, sheet, confirmDialog, menu, ratingOf, positionsText, copyText, fmt, toggleRow, jobProgress, jobLine, variantName, variantNumbers, versionText, versionBadge, versionSent } from "./ui.js";
import { slider, beforeOf, ensureStyle, present, aheadLine, startPos } from "./present.js";
import { openSend, openApproval, openDelivered } from "./send.js";

const CSS = `
.er{display:grid;gap:var(--gap);padding:4px 0 24px;max-width:1280px;margin:0 auto;grid-template-columns:minmax(0,1fr);
 grid-template-areas:"banner" "stage" "props" "panel" "renders" "versions"}
.er>*{min-width:0}.er .card,.er .details{margin:0}
@media (min-width:1100px){.er{grid-template-columns:minmax(0,1fr) 420px;grid-template-areas:"banner banner" "stage panel" "props panel" "renders panel" "versions panel";align-items:start}
}
.er-banner{grid-area:banner}.er-stage{grid-area:stage}.er-props{grid-area:props}.er-panel{grid-area:panel}.er-renders{grid-area:renders}.er-versions{grid-area:versions}
.er h3{margin:0 0 8px;font-size:17px}
.er .muted{color:var(--muted);font-size:14px;margin:0}
.er-hint{text-align:center;margin:6px 0 0}
.er-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.er-tile{position:relative;display:flex;flex-direction:column;gap:4px;padding:5px;border:2px solid var(--line);border-radius:var(--r);background:var(--panel);color:var(--text);font:inherit;font-size:14px;text-align:left;cursor:pointer;min-height:44px}
.er-tile img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:8px;background:var(--stage)}
.er-tile[aria-current=true]{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent-soft)}
.er-tile .er-star{position:absolute;top:9px;left:9px;padding:2px 7px;border-radius:999px;background:var(--accent);color:var(--accent-ink);font-size:14px}
.er-tile .er-b{display:flex;flex-wrap:wrap;gap:4px}
.er-ph{display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;gap:8px;min-height:150px;padding:16px;border-radius:var(--r);font-size:15px}
.er-ph b{font-size:17px;color:var(--text)}.er-ph .bar{width:min(320px,90%)}
.er-ph .er-spin{width:32px;height:32px;border-radius:50%;border:3px solid var(--line);border-top-color:var(--accent);animation:spin 1s linear infinite}
.er-tilewrap{display:flex;flex-direction:column;gap:6px;min-width:0}
.er-tilewrap .btn{min-height:44px;font-size:15px;padding:6px 8px}
.er-best{display:inline-block;margin-left:6px;padding:1px 8px;border-radius:999px;background:var(--ok);color:#fff;font-size:13px;font-weight:700;vertical-align:1px}
.er-name{font-weight:650;font-size:15px}
.er-panel{display:flex;flex-direction:column;gap:10px}
.er-panel .er-row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.er .er-acts{display:grid;grid-template-columns:1fr;gap:8px}
@media (min-width:500px) and (max-width:1099px){.er .er-acts{grid-template-columns:1fr 1fr}}
.er .er-explain{min-height:44px;cursor:pointer;border:0;font:inherit;font-size:14px}
.er table{border-collapse:collapse;width:100%;font-size:15px}.er td{padding:4px 0;border-bottom:1px solid var(--line)}.er td+td{text-align:right}
.er-ver{display:flex;flex-direction:column;gap:8px}
.er-ver+.er-ver{margin-top:10px}
.er-thumbs{display:flex;gap:6px;flex-wrap:wrap}.er-thumbs img{width:72px;height:54px;object-fit:cover;border-radius:6px;background:var(--stage)}
.er-banner.card{border-left:4px solid var(--accent);display:flex;flex-direction:column;gap:8px}
.er-banner.change{border-left-color:var(--warn)}
.er-banner p{margin:0;overflow-wrap:anywhere}.er-banner .btn{align-self:flex-start}
.er-prog{display:flex;flex-direction:column;gap:6px;margin:0 0 10px}.er-prog p{margin:0;font-size:15px}
.er-sec-head{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.er-sec-head h3{margin:0}
.er-chips{display:flex;flex-wrap:wrap;gap:8px}
.er-wood{display:flex;flex-direction:column;gap:12px}
`;
const ACTIVE = new Set(["queued", "running"]);
const KIND_DE = { room: "Raum", free: "Frei", render: "Skizze", material: "Holz", edit: "Änderung", upscale: "Druck" };
const SCORE_DE = { room_preserved: "Raum erhalten", matches_request: "Wie bestellt", proportions: "Proportionen", realism: "Realismus", artifacts: "Fehlerfrei" };
export const VSTATUS = { offen: ["offen", "info"], angesehen: ["angesehen", "info"], freigegeben: ["freigegeben", "ok"],
                         aenderung: ["Änderungswunsch", "warn"], zurueckgezogen: ["zurückgezogen", "none"] };
const TEMPLATE_EXPLAIN = "Dieser Vorschlag wurde ohne Beschreibungs-KI berechnet, weil sie gerade nicht lief. Die Qualität kann geringer sein – bei Bedarf neu berechnen.";
const LOCKED = "Dieser Vorschlag gehört zu einer gesendeten Version. Ziehen Sie zuerst die Version zurück.";
const de1 = n => String(Math.round(n * 10) / 10).replace(".", ",");
const mins = s => Math.max(1, Math.round((+s || 0) / 60));

/** Tiles for images still coming, from the project's jobs (any owner). */
export function placeholders(jobs, meId = null) {
  const out = [];
  for (const j of jobs || []) {
    if (!ACTIVE.has(j.state)) continue;
    const n = Math.max(0, (j.count || 1) - (j.done_images || 0));
    const render = String(j.dedupe || "").startsWith("render:");
    const who = j.owner && meId && j.owner !== meId ? `${j.owner_name || "Jemand"} berechnet …` : null;
    for (let k = 0; k < n; k++)
      out.push({ job: j.id, k, render, who, retry: !!j.retry_of, title: j.retry_of ? "Neuer Versuch mit Korrektur …" : who || j.label || "Vorschlag",
                 text: jobLine(j), progress: jobProgress(j), ahead: aheadLine(j) });
  }
  return out;
}
/** Badges on a proposal: approved, made without the caption model, draft quality. */
export function variantBadges(v, doc) {
  const out = [];
  if (doc?.approved?.variant_id === v.id) out.push({ tone: "ok", text: "Freigegebener Entwurf" });
  if (v.caption_backend === "template") out.push({ tone: "warn", text: "ohne Beschreibungs-KI", explain: TEMPLATE_EXPLAIN });
  if (Number.isFinite(v.steps) && v.steps < 16) out.push({ tone: "info", text: "Entwurfsqualität" });
  return out;
}
export const chosenIds = doc => (doc?.variants || []).filter(v => v.fav).map(v => v.id);
/** Primary of step ⑤ (§10.6). */
export function primaryOf(doc) {
  if (doc?.approved && !doc.approved.outdated && ["freigegeben", "auftrag", "fertig"].includes(doc.status))
    return { label: "Weiter: Angebot & Auftrag", disabled: false, reason: null, next: "auftrag" };  // what comes after the approval
  const any = chosenIds(doc).length > 0;
  return { label: "An Kunden senden …", disabled: !any, reason: any ? null : "Erst einen Entwurf mit ★ für den Kunden auswählen." };
}
/** The best graded proposal of the newest batch (≥ 2 images of one job, all graded), else null. */
export function bestOf(list) {
  const newest = list.reduce((a, v) => (!a || String(v.image) > String(a.image) ? v : a), null);
  if (!newest?.job) return null;
  const batch = list.filter(v => v.job === newest.job);
  if (batch.length < 2 || batch.some(v => !v.judge || v.judge.status === "pending")) return null;
  const score = v => (v.judge?.status === "ok" && v.judge.overall != null ? v.judge.overall + (v.judge.pass ? 10 : 0) : -1);
  const best = batch.reduce((a, v) => (score(v) > score(a) ? v : a));
  return score(best) >= 0 ? best : null;
}
/** Approval or change-request banner (customer strings stay plain text; the caller renders them with textContent). */
export function responseBanner(doc) {
  const vs = doc?.versions || [];
  if (doc?.approved) {
    const a = doc.approved, ver = vs.find(v => v.n === a.version);
    const n = ver ? ver.variant_ids.indexOf(a.variant_id) + 1 || 1 : 1;
    if (a.outdated) return { kind: "change", variantId: a.variant_id, outdated: true,
      text: `Freigabe veraltet: ${a.name || "Der Kunde"} hat Entwurf ${n} freigegeben (${fmt.date(a.at)}), seitdem wurden Möbel oder Maße geändert. Bitte eine neue Version senden.` };
    return { kind: "approved", variantId: a.variant_id, text: `${a.name || "Der Kunde"} hat Entwurf ${n} freigegeben ✓ (${fmt.date(a.at)})` };
  }
  const ver = [...vs].reverse().find(v => v.status !== "zurueckgezogen" && v.response);
  if (ver && ver.response.action === "aenderung" && ver.status === "aenderung") {
    const r = ver.response;
    return { kind: "change", variantId: r.variant_id, version: ver.n, comment: r.comment || "",
             text: `${r.name || "Der Kunde"} wünscht eine Änderung: „${r.comment || ""}“` };
  }
  return null;
}
export function sortVariants(list, sort) {
  // newest first by the image name's time stamp (YYYYMMDD-HHMMSS-seed): the stored order differs between migrated and new
  // variants; without a stamp the stored order counts, read as oldest first
  const stamp = v => (/^(\d{8}-\d{6})/.exec(v.image || "") || [])[1] || "";
  const l = list.map((v, i) => [v, i]).sort((a, b) => (stamp(b[0]) || "").localeCompare(stamp(a[0]) || "") || b[1] - a[1]).map(x => x[0]);
  if (sort !== "gut") return l;
  const score = v => v.judge?.status === "ok" && v.judge.overall != null ? v.judge.overall + (v.judge.pass ? 10 : 0) : -1;
  return l.sort((a, b) => score(b) - score(a));
}
export function personName(id, team = []) {
  if (!id) return "";
  if (id === "kunde") return "Kunde";
  if (id === "system") return "System";
  const m = team.find(t => t.id === id);
  if (m) return m.name;
  return id.startsWith("dev:") ? `Gerät ${id.slice(4, 8)}` : id;
}
export const versionLine = (ver, team) => versionText(ver, id => personName(id, team));

const fill = (node, ...kids) => node.replaceChildren(...kids.filter(k => k != null && k !== false));   // DOM would print "null"
const badge = (tone, text) => h("span", { class: `badge ${tone}` }, text);

export default {
  id: "ergebnis", title: "Ergebnis & Kunde",
  state(doc, view, store) {
    const jobs = store?.projectJobs?.() || [];
    const has = (doc?.variants || []).length > 0 || jobs.some(j => ACTIVE.has(j.state));
    return { done: !!(doc?.versions || []).length || !!doc?.approved, available: has,
             reason: has ? null : "Noch keine Vorschläge – zuerst in Schritt 4 berechnen." };
  },
  mount(el, sctx) {
    ensureStyle("er-style", CSS);
    const store = sctx.store, view = sctx.view || {}, unsub = [];
    const doc = () => store.doc;
    const jobs = () => store.projectJobs?.() || [];
    let picked = false, sl = null, keys = {}, known = new Set((doc()?.variants || []).map(v => v.id));
    const posById = new Map();  // slider position per proposal (this visit); a new one starts with its furniture in view

    const banner = h("div", { class: "er-banner" }), stage = h("div"), prog = h("div", { class: "er-prog", "aria-live": "polite" });
    const props = h("section", { class: "er-props card" }), panel = h("aside", { class: "er-panel card" });
    const renders = h("section", { class: "er-renders card" }), versions = h("section", { class: "er-versions card" });
    el.replaceChildren(h("div", { class: "er" }, banner, h("div", { class: "er-stage" }, prog, stage), props, panel, renders, versions));

    const all = () => { const d = doc(), del = new Set(d?.deleted_variants || []); return (d?.variants || []).filter(v => !del.has(v.id)); };
    const proposals = () => sortVariants(all().filter(v => v.kind !== "render"), view.sort);
    const sketches = () => sortVariants(all().filter(v => v.kind === "render"), "neu");
    const selected = () => all().find(v => v.id === view.selected) || proposals()[0] || sketches()[0] || null;
    const select = (id, byUser = true) => {
      if (byUser) picked = true;
      view.selected = id; sctx.saveView?.(); render();
      if (byUser && stage.getBoundingClientRect().top < 0) stage.scrollIntoView({ block: "start", behavior: "smooth" });
    };

    function render() {
      const d = doc(); if (!d) return;
      // a new image from a running job is shown at once unless the user picked one in the meantime
      const ids = all().map(v => v.id), fresh = ids.filter(id => !known.has(id));
      known = new Set(ids);
      if (fresh.length && !picked) { view.selected = fresh[fresh.length - 1]; sctx.saveView?.(); }
      const b = bestOf(proposals());  // a finished, graded batch opens on its best proposal (once per batch)
      if (b && b.job !== view.bestJob) {
        view.bestJob = b.job;
        if (!picked) { view.selected = b.id; keys.stage = null; }
        sctx.saveView?.();
      }
      sendAfter();
      renderBanner(); renderProgress(); renderStage(); renderList(props, "Vorschläge", proposals(), false); renderList(renders, "Skizzen-Renderings", sketches(), true);
      renderPanel(); renderVersions(); sctx.refresh?.();
    }
    const changed = (k, val) => { const s = JSON.stringify(val); if (keys[k] === s) return false; keys[k] = s; return true; };

    /** „Fein berechnen, danach senden“: when that job is done, its images are chosen and the send sheet opens. */
    function sendAfter() {
      const sa = view.sendAfter;
      if (!sa) return;
      const job = jobs().find(j => j.id === sa.job), fresh = all().filter(v => v.job === sa.job);
      if (job && ACTIVE.has(job.state)) return;
      delete view.sendAfter; sctx.saveView?.();
      if (!fresh.length || Date.now() - (sa.at || 0) > 3600e3) return;
      const ops = fresh.filter(v => !v.fav).map(v => ({ op: "set", path: `variants.${v.id}.fav`, value: true }));
      if (ops.length) store.patch(ops, { now: true }).catch(() => {});
      setTimeout(() => openSend(sctx, { preselect: fresh.map(v => v.id), note: sa.note }), 300);
    }
    function renderBanner() {
      const b = responseBanner(doc());
      if (!changed("banner", b)) return;
      banner.hidden = !b;
      banner.className = `er-banner card ${b?.kind === "change" ? "change" : ""}`;
      if (!b) return banner.replaceChildren();
      fill(banner, h("p", {}, b.text),
        b.outdated ? h("button", { class: "btn secondary", on: { click: () => openSend(sctx, { preselect: chosenIds(doc()) }) } }, "Neue Version senden …") :
        b.kind === "change" ? h("button", { class: "btn secondary", on: { click: () => {
          if (all().some(v => v.id === b.variantId)) select(b.variantId);
          const v = all().find(x => x.id === b.variantId) || selected();
          if (v) changeSheet(v, b.comment);
        } } }, "Als Änderung übernehmen") : null);
    }
    function renderProgress() {
      // the progress text appears once: in the big placeholder while there is no image yet, else here above the image
      const act = selected() ? jobs().filter(j => ACTIVE.has(j.state)) : [];
      if (!changed("prog", act.map(j => [j.id, jobLine(j), Math.round(jobProgress(j) * 100), aheadLine(j)]))) return;
      prog.hidden = !act.length;
      prog.replaceChildren(...act.map(j => {
        const pct = Math.round(jobProgress(j) * 100);
        const who = j.owner && store.ownerKey?.() && j.owner !== store.ownerKey() ? `${j.owner_name || "Jemand"} · ` : "";
        return h("div", {}, h("p", {}, `${who}${j.label || "Berechnung"}: ${jobLine(j)}`),
          view.sendAfter?.job === j.id ? h("p", { class: "muted" }, "Danach öffnet sich der Versand von selbst.") : null,
          aheadLine(j) ? h("p", { class: "muted" }, aheadLine(j)) : null,
          h("div", { class: "bar", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" }, h("i", { style: `width:${pct}%` })));
      }));
    }
    function renderStage() {
      const v = selected();
      if (!changed("stage", v ? [v.id, v.image, v.from, v.room, v.sketch] : null) && sl) return;
      if (sl?.dragging) { keys.stage = null; return; }
      sl?.destroy(); sl = null;
      if (!v) {
        const ph = placeholders(jobs(), store.ownerKey?.())[0];
        if (!changed("ph", ph ? [ph.title, ph.text, Math.round(ph.progress * 100), ph.ahead] : null) && stage.firstChild) return;
        const pct = ph ? Math.round(ph.progress * 100) : 0;
        stage.replaceChildren(h("div", { class: "placeholder-tile er-ph", style: "min-height:240px", role: "status" },
          ph ? h("span", { class: "er-spin", "aria-hidden": "true" }) : null,
          h("b", {}, ph ? (ph.who || "Vorschläge werden berechnet") : "Noch keine Vorschläge"),
          h("span", {}, ph ? ph.text : "Zuerst in Schritt 4 berechnen."), ph?.ahead ? h("span", { class: "muted" }, ph.ahead) : null,
          ph ? h("div", { class: "bar", role: "progressbar", "aria-valuenow": String(pct), "aria-valuemin": "0", "aria-valuemax": "100" }, h("i", { style: `width:${pct}%` })) : null));
        return;
      }
      keys.ph = null;
      sl = slider(v, doc(), { pos: posById.get(v.id) ?? startPos(v, doc()), onPos: p => { posById.set(v.id, Math.round(p)); }, fit: () => ({
        w: stage.clientWidth || 360, h: Math.max(220, Math.round(innerHeight * (innerWidth >= 1100 ? 0.5 : innerWidth > innerHeight ? 0.62 : 0.5))) }) });
      stage.replaceChildren(sl.el, beforeOf(v, doc()) ? h("p", { class: "muted er-hint" }, "Schieber ziehen · Doppeltippen: ganz vorher/nachher") : "");
    }
    let saveT = null;
    const saveSoon = () => { clearTimeout(saveT); saveT = setTimeout(() => sctx.saveView?.(), 400); };

    function tile(v, nums, best) {
      const r = ratingOf(v.judge || null), sel = selected()?.id === v.id, name = variantName(doc(), v, nums);
      const bs = variantBadges(v, doc()), isBest = best?.id === v.id;
      const t = h("button", { type: "button", class: "er-tile", "aria-current": String(sel),
        "aria-label": `${name}${isBest ? ", bester Vorschlag" : ""}${v.fav ? ", für den Kunden ausgewählt" : ""}`, on: { click: () => select(v.id) } },
        h("img", { src: img(v.image), alt: "", loading: "lazy" }),
        v.fav ? h("span", { class: "er-star", "aria-hidden": "true" }, "★") : null,
        h("span", { class: "er-name" }, name, isBest ? h("span", { class: "er-best" }, "Bester Vorschlag") : null),
        h("span", { class: "er-b" }, badge(r.tone === "pending" ? "info" : r.tone === "none" ? "" : r.tone, r.short || r.text),
          ...bs.map(b => badge(b.tone, b.text))),
        h("span", { class: "muted" }, [KIND_DE[v.kind], v.material].filter(Boolean).join(" · ")));
      if (!isBest) return t;
      return h("div", { class: "er-tilewrap" }, t, h("button", { type: "button", class: `btn ${v.fav ? "secondary" : "primary"}`, "aria-pressed": String(!!v.fav),
        on: { click: () => { select(v.id); store.set(`variants.${v.id}.fav`, !v.fav, { now: true }); } } }, v.fav ? "★ Für den Kunden ausgewählt" : "★ Diesen für den Kunden"));
    }
    function phTile(p) {   // the text is shown once (big placeholder / line above the image): tiles only spin
      return h("div", { class: "placeholder-tile er-ph", role: "status", "aria-label": `${p.title}: ${p.text}` },
        h("span", { class: "er-spin", "aria-hidden": "true" }), p.who ? h("span", {}, p.who) : null);
    }
    function renderList(sec, title, list, isRender) {
      const ph = placeholders(jobs(), store.ownerKey?.()).filter(p => p.render === isRender);
      const sel = selected()?.id;
      const designRunning = jobs().some(j => ACTIVE.has(j.state) && (j.dedupe || "design") === "design");
      const key = [list.map(v => [v.id, v.fav, v.judge?.status, v.judge?.overall, v.judge?.pass, v.caption_backend, v.steps]), ph.map(p => [p.job, p.k, p.who]), sel,
                   doc().approved?.variant_id, view.sort, designRunning, doc().count];
      if (!changed(title, key)) return;
      sec.hidden = !list.length && !ph.length;
      if (sec.hidden) return sec.replaceChildren();
      const head = h("div", { class: "er-sec-head" }, h("h3", {}, title));
      if (!isRender && list.length > 4) {
        const seg = h("div", { class: "seg", role: "radiogroup", "aria-label": "Sortierung" },
          ...[["neu", "Neueste"], ["gut", "Beste zuerst"]].map(([k, l]) => h("button", { type: "button", role: "radio",
            "aria-checked": String((view.sort || "neu") === k), class: (view.sort || "neu") === k ? "active" : "",
            on: { click: () => { view.sort = k; sctx.saveView?.(); render(); } } }, l)));
        head.append(seg);
      }
      const n = Math.max(1, doc().count || 2);
      const nums = variantNumbers(doc()), best = isRender ? null : bestOf(list);
      fill(sec, head, h("div", { class: "er-grid" }, ...ph.map(phTile), ...list.map(v => tile(v, nums, best))),
        !isRender && list.length && !designRunning ? h("button", { class: "btn secondary block", style: "margin-top:10px", on: { click: more } },
          n === 1 ? "Noch 1 Vorschlag berechnen" : `Noch ${n} Vorschläge berechnen`) : null);
    }
    async function more() {
      try {
        const r = await store.generate({ kind: doc().room ? "room" : "free", count: Math.max(1, doc().count || 2) });
        if (r?.job && !r.running) { picked = false; toast("Wird berechnet …", { kind: "ok" }); }
      } catch (e) { if (!e?.handled && !["changed", "running"].includes(e?.code)) toast(e.message || "Start fehlgeschlagen", { kind: "err" }); }
    }

    function renderPanel() {
      const v = selected(), d = doc();
      const key = v ? [v.id, v.fav, v.judge, v.caption_backend, v.steps, v.locked, d.approved?.variant_id, v.size, v.keep, (v.pieces || []).length, chosenIds(d)] : null;
      if (!changed("panel", key)) return;
      panel.hidden = !v;
      if (!v) return panel.replaceChildren();
      const r = ratingOf(v.judge || null), j = v.judge;
      const bs = variantBadges(v, d).map(b => b.explain
        ? h("button", { type: "button", class: `badge ${b.tone} er-explain`, on: { click: () => toast(b.explain, { ms: 8000 }) } }, `${b.text} ⓘ`)
        : badge(b.tone, b.text));
      const details = j?.scores ? h("details", { class: "details" }, h("summary", {}, "Details"),
        h("table", {}, ...Object.entries(SCORE_DE).map(([k, de]) => h("tr", {}, h("td", {}, de), h("td", {}, j.scores[k] != null ? String(j.scores[k]) : "–"))),
          h("tr", {}, h("td", {}, h("b", {}, "Gesamt")), h("td", {}, h("b", {}, j.overall != null ? de1(j.overall) : "–"))))) : null;
      const fav = h("button", { type: "button", class: `btn block ${v.fav ? "secondary" : "primary"}`, "aria-pressed": String(!!v.fav),
        on: { click: () => store.set(`variants.${v.id}.fav`, !v.fav, { now: true }) } }, v.fav ? "★ Ausgewählt" : "★ Für den Kunden auswählen");
      const big = v.size && Math.sqrt(v.size[0] * v.size[1]) >= 2000;
      const moreBtn = h("button", { type: "button", class: "btn ghost", "aria-haspopup": "menu", on: { click: e => menu(e.currentTarget, [
        { label: "Erneut prüfen", onClick: () => regrade(v) },
        { label: "Herunterladen", onClick: () => download(v) },
        { label: "Löschen", danger: true, onClick: () => remove(v) }], { title: "Weitere Aktionen" }) } }, "⋯ Mehr");
      fill(panel,
        h("div", { class: "er-row" }, badge(r.tone === "pending" ? "info" : r.tone === "none" ? "" : r.tone, r.text), ...bs),
        h("h3", { style: "margin:0" }, variantName(d, v)),
        h("p", { class: "muted" }, [KIND_DE[v.kind] || "Vorschlag", v.piece, v.material, v.handles].filter(Boolean).join(" · ")),
        j?.comment ? h("p", { style: "margin:0" }, `KI: ${j.comment}`) : null, details,
        fav, h("p", { class: "muted" }, "Ausgewählte Entwürfe kommen auf Kundenblatt und Kundenlink."),
        h("h3", {}, "Anpassen"),
        h("div", { class: "er-acts" },
          h("button", { class: "btn secondary", on: { click: () => woodSheet(v) } }, "Holz tauschen"),
          h("button", { class: "btn secondary", on: { click: () => changeSheet(v) } }, "Änderung beschreiben"),
          h("button", { class: "btn secondary", disabled: big ? "" : null, title: big ? "Das Bild ist schon groß genug." : null,
                        on: { click: () => upscale(v) } }, "Für Druck vergrößern (ca. 2 Min.)")),
        h("h3", {}, "Für den Kunden"),
        h("div", { class: "er-acts" },
          primaryOf(d).next ? h("button", { class: "btn secondary", on: { click: () => openSend(sctx, { preselect: chosenIds(d) }) } }, "Neue Version senden …") : null,
          h("button", { class: "btn secondary", on: { click: () => sheetPdf(v) } }, "Kundenblatt (PDF)"),
          h("button", { class: "btn secondary", on: { click: () => present(sctx, { variantIds: presentIds(v.id), start: v.id }) } }, "Präsentieren")),
        h("div", { class: "er-row" }, moreBtn));
    }
    // the starred proposals (else all) — and always the one on screen, which the presentation opens with
    const presentIds = cur => { const c = chosenIds(doc()), ids = c.length ? c : proposals().map(v => v.id); return !cur || ids.includes(cur) ? ids : [cur, ...ids]; };

    function renderVersions() {
      const d = doc(), vs = [...(d.versions || [])].reverse();
      if (!changed("versions", [vs.map(v => [v.n, v.status, v.response, v.viewed_at, v.sent_at]), d.approved, (store.team || []).length])) return;
      versions.hidden = !vs.length;
      if (!vs.length) return versions.replaceChildren();
      versions.replaceChildren(h("h3", {}, "Versionen für den Kunden"), ...vs.map(ver => {
        const [label, tone] = versionBadge(ver);
        const live = ver.status !== "zurueckgezogen";
        const approvedHere = d.approved?.version === ver.n;
        const r = ver.response;
        return h("div", { class: "er-ver" },
          h("div", { class: "er-row" }, h("strong", {}, versionLine(ver, store.team || [])), badge(tone, label),
            ver.pin && live ? h("span", { class: "muted", title: "Der Kunde braucht die PIN, um den Link zu öffnen" }, `PIN ${ver.pin}`) : null),
          h("div", { class: "er-thumbs" }, ...(ver.variant_ids || []).map(id => {
            const v = all().find(x => x.id === id) || (d.trash || []).find(x => x.id === id);
            return v ? h("img", { src: img(v.image), alt: "" }) : null;
          })),
          r ? h("p", { class: "muted" }, `${r.name || "Kunde"}: ${r.action === "freigeben" ? "Entwurf freigegeben" : "Änderung gewünscht"}${r.comment ? ` – „${r.comment}“` : ""}`) : null,
          h("div", { class: "er-acts" },
            live ? h("button", { class: `btn ${versionSent(ver) ? "secondary" : "primary"}`, on: { click: () => openDelivered(sctx, ver) } }, versionSent(ver) ? "Erneut senden …" : "Jetzt senden …") : null,
            live ? h("button", { class: "btn secondary", on: { click: () => openApproval(sctx, ver) } }, "Freigabe erfassen") : null,
            approvedHere ? h("button", { class: "btn secondary", on: { click: () => copyPositions(d.approved.variant_id) } }, "Positionstext kopieren") : null,
            live ? h("button", { class: "btn ghost", on: { click: () => withdraw(ver) } }, "Link zurückziehen") : null));
      }));
    }

    async function copyPositions(vid) {
      const res = await copyText(positionsText(doc(), vid, store.catalog));
      if (res === "copied") toast("Text kopiert – in das Angebot einfügen.", { kind: "ok" });
    }
    async function withdraw(ver) {
      if (!await confirmDialog({ title: "Link zurückziehen", danger: true, ok: "Link zurückziehen",
        text: `Version ${ver.n} wird zurückgezogen. Der Kundenlink funktioniert danach nicht mehr; eine neue Version ersetzt sie.` })) return;
      try { await api("POST", `/api/workshop/${doc().id}/versions/${ver.n}/withdraw`); toast(`Version ${ver.n} zurückgezogen.`); }
      catch (e) { toast(e.message, { kind: "err" }); }
    }
    async function regrade(v) {
      toast("Prüfung läuft …");
      try {
        const r = await api("POST", `/api/workshop/${doc().id}/judge/${v.id}`);
        if (r?.status && r.status !== "ok") toast(r.comment || "Prüfung nicht möglich", { kind: "err" });
      } catch (e) { toast(e.message, { kind: "err" }); }
    }
    function download(v) {
      const a = h("a", { href: img(v.image), download: `${(doc().title || "Entwurf").replace(/[^\wäöüÄÖÜß-]+/g, "_")}_${v.id}.png` });
      document.body.append(a); a.click(); a.remove();
    }
    async function remove(v) {
      if (v.locked) return toast(LOCKED, { kind: "err", ms: 8000 });
      try {
        await store.patch([{ op: "delete_variant", id: v.id }], { now: true });
        if (view.selected === v.id) { view.selected = null; sctx.saveView?.(); }
        toast("Vorschlag gelöscht", { undo: () => store.patch([{ op: "restore_variant", id: v.id }], { now: true }) });
      } catch (e) { toast(e.message || LOCKED, { kind: "err" }); }
    }
    async function startEdit(body, label) {
      try {
        const r = await store.edit(body);
        if (r?.job) { picked = false; if (!r.running) toast(`${label} – wird berechnet …`, { kind: "ok" }); return true; }
      } catch (e) { if (!e?.handled && !["changed", "running"].includes(e?.code)) toast(e.message || "Start fehlgeschlagen", { kind: "err" }); }
      return false;
    }
    async function upscale(v) {
      if (v.size && Math.sqrt(v.size[0] * v.size[1]) >= 2000) return toast("Das Bild ist schon groß genug.");
      await startEdit({ op: "upscale", variant_id: v.id }, "Für Druck vergrößern");
    }
    async function sheetPdf(v) {
      const ids = chosenIds(doc()).length ? chosenIds(doc()) : [v.id];
      const tab = window.open("", "_blank");  // now: browsers block popups opened after an await
      try {
        const r = await api("POST", `/api/workshop/${doc().id}/sheet`, { variant_ids: ids });
        if (tab) tab.location = r.pdf;
        else { const a = h("a", { href: r.pdf, download: "" }); document.body.append(a); a.click(); a.remove(); }
        if (r.skipped?.length) toast(`${r.skipped.length} Vorschlag/Vorschläge übersprungen (Bild gelöscht).`);
      } catch (e) { tab?.close(); toast(e.message, { kind: "err" }); }
    }

    async function estimateEdit(body, out, fmtLabel) {
      try {
        const r = await store.edit({ ...body, dry_run: true });
        const s = (r?.plan || r)?.estimate_s;
        if (s) out.textContent = fmtLabel(mins(s));
      } catch {}
    }
    function woodSheet(v) {
      const cat = store.catalog || {}, mats = cat.materials || {};
      const common = (cat.common_materials || ["eiche", "eicheweiss", "nuss", "weiss"]).filter(k => mats[k]);
      const keys = [...common, ...Object.keys(mats).filter(k => k !== "custom" && !common.includes(k))];
      let piece = "", mat = keys.find(k => mats[k]?.de !== v.material) || keys[0];
      const pieces = v.pieces || [];
      const pc = h("div", { class: "er-chips", role: "radiogroup", "aria-label": "Welches Möbel?" });
      const mc = h("div", { class: "er-chips", role: "radiogroup", "aria-label": "Neues Holz" });
      const go = { label: "Holz tauschen (ca. 1 Min.)", kind: "primary", onClick: async () => {
        return (await startEdit({ op: "material", variant_id: v.id, material: mat, piece_id: piece || null }, `Holz: ${mats[mat]?.de || mat}`)) ? undefined : false;
      } };
      const chip = (on, label, fn) => h("button", { type: "button", role: "radio", "aria-checked": String(on), class: `chip${on ? " active" : ""}`, on: { click: fn } }, label);
      const draw = () => {
        pc.replaceChildren(chip(!piece, "alle Möbel", () => { piece = ""; draw(); }),
          ...pieces.map(p => chip(piece === p.id, p.label || "Möbel", () => { piece = p.id; draw(); })));
        mc.replaceChildren(...keys.map(k => chip(mat === k, mats[k].de, () => { mat = k; draw(); })));
      };
      draw();
      const body = h("div", { class: "er-wood" },
        pieces.length > 1 ? h("div", {}, h("h3", {}, "Welches Möbel?"), pc) : null,
        h("div", {}, h("h3", {}, "Neues Holz"), mc));
      const s = sheet({ title: "Holz tauschen", body, actions: [go] });
      const btn = [...(s.el?.querySelectorAll(".sheet-actions button") || [])].find(b => b.textContent === go.label);
      if (btn) estimateEdit({ op: "material", variant_id: v.id, material: mat }, btn, m => `Holz tauschen (ca. ${m} Min.)`);
      return s;
    }
    function changeSheet(v, prefill = "") {
      const ta = h("textarea", { rows: "4", placeholder: "z. B. Türen in Salbeigrün lackieren, Sockel schwarz" });
      ta.value = prefill || "";
      let keepOnly = true;
      const tr = v.keep ? toggleRow({ title: "Nur am Möbel ändern (der Rest bleibt pixelgenau)", checked: true, onChange: x => { keepOnly = x; } }) : null;
      const go = { label: "Änderung berechnen (ca. 1 Min.)", kind: "primary", onClick: async () => {
        const t = ta.value.trim();
        if (!t) { toast("Bitte die Änderung beschreiben.", { kind: "err" }); ta.focus(); return false; }
        return (await startEdit({ op: "edit", variant_id: v.id, text: t, keep_only: !!v.keep && keepOnly }, "Änderung")) ? undefined : false;
      } };
      const s = sheet({ title: "Änderung beschreiben", body: h("div", { class: "er-wood" }, h("label", { class: "field" }, h("span", {}, "Was soll anders werden?"), ta), tr), actions: [go] });
      setTimeout(() => ta.focus(), 50);
      return s;
    }

    render();
    return {
      unmount() { unsub.forEach(f => { try { f?.(); } catch {} }); sl?.destroy(); clearTimeout(saveT); },
      onDoc() { render(); },
      onQueue() { render(); },
      primary() {
        const p = primaryOf(doc());
        if (p.next) return { ...p, run: () => sctx.nav(`#/p/${doc().id}/${p.next}`) };
        return { ...p, run: () => openSend(sctx, { preselect: chosenIds(doc()) }) };
      },
    };
  },
};
