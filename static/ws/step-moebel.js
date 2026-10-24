/* ② Möbel: one card per piece (active one open), type tiles with real default dimensions, Maße, Holz, Griffe,
   Besonderheiten, optional sketch → photo render, add and undoable delete (spec §12, §16.6). */
import { api, toast, sheet, img, pickImage, upload, toggleRow, confirmDialog } from "./ui.js";
import { el, css, set, clone, pieceOk, placed, emptyPiece, activePiece, pieceName, sceneFor, COLORS, errText, wallCm, wideFixButtons } from "./editor.js";
import { understandCard } from "./verstanden.js";

const MAX = 4;
const S = 'fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"';
// 32×32 line icons per catalog type (inline, no external resources)
export const TYPE_ICONS = {
  schrank: `<rect x="6" y="3" width="20" height="26" rx="1"/><path d="M16 3v26M13 15v3M19 15v3"/>`,
  kueche: `<path d="M3 16h26v13H3zM3 16v-2h26v2M5 4h22v6H5z"/><path d="M12 16v13M20 16v13M10 7h2M20 7h2"/>`,
  regal: `<rect x="5" y="3" width="22" height="26"/><path d="M5 10h22M5 17h22M5 23h22M16 3v26"/>`,
  sideboard: `<rect x="3" y="12" width="26" height="12" rx="1"/><path d="M12 12v12M20 12v12M6 24v3M26 24v3"/>`,
  tisch: `<path d="M3 12h26M6 12v15M26 12v15M3 12l3-3h20l3 3"/>`,
  bett: `<path d="M3 26V8M3 20h26v6M3 15h8a3 3 0 0 1 3 3v2M14 17h15v3"/>`,
  treppe: `<path d="M3 28h6v-6h6v-6h6v-6h6V4h2"/>`,
  garderobe: `<path d="M6 3v26M26 3v26M6 8h20M10 8v5M16 8v5M22 8v5M6 20h20v4H6"/>`,
  bad: `<path d="M4 12h24v3H4zM6 15h20v12H6zM16 15v12M13 20h-2M21 20h-2M16 12V8a2 2 0 0 1 4 0"/>`,
  sitzbank: `<path d="M3 18h26v8H3zM3 14h26v4M12 18v8M20 18v8"/>`,
  wand: `<rect x="4" y="3" width="24" height="26"/><path d="M10 3v26M16 3v26M22 3v26"/>`,
  tuer: `<rect x="8" y="3" width="16" height="26"/><path d="M20 16h1.5M5 29h22"/>`,
  wandregal: `<path d="M3 4v24M5 11h22M5 20h22"/><path d="M8 11V8h4v3M17 20v-4h3v4"/>`,
  haengeschrank: `<path d="M3 4v24"/><rect x="6" y="6" width="22" height="12"/><path d="M17 6v12M5 28h24"/>`,
};
export const typeIcon = t => `<svg viewBox="0 0 32 32" width="32" height="32" ${S} aria-hidden="true">${TYPE_ICONS[t] || '<rect x="5" y="8" width="22" height="18"/>'}</svg>`;

/** Ops for choosing a type: catalog dims fill empty fields and replace untouched defaults of the previous type. */
export function typeOps(piece, type, catalog) {
  const ops = [set(`pieces.${piece.id}.type`, type)];
  const def = catalog?.pieces?.[type]?.dims, prev = catalog?.pieces?.[piece.type]?.dims;
  // wall-hung types bring their usual height above the floor; switching back to a standing type puts it on the floor
  const el0 = catalog?.pieces?.[type]?.elev || 0, elPrev = catalog?.pieces?.[piece.type]?.elev || 0;
  if ((+piece.elev || 0) === elPrev && el0 !== elPrev) ops.push(set(`pieces.${piece.id}.elev`, el0));
  if (def) ["w", "h", "d"].forEach((k, i) => {
    const v = piece[k];
    if (v == null || v === "" || (prev && +v === prev[i])) if (+v !== def[i]) ops.push(set(`pieces.${piece.id}.${k}`, def[i]));
  });
  return ops;
}

/** Delete ops + their undo: the last piece is replaced by a client-made empty piece (§5.1 remove_piece.replacement). */
export function deleteOps(doc, id, newId) {
  const pieces = doc.pieces || [], gone = pieces.find(p => p.id === id);
  if (!gone) return null;
  const replacement = pieces.length <= 1 ? emptyPiece(gone, newId) : null;
  const op = { op: "remove_piece", id };
  if (replacement) op.replacement = replacement;
  const undo = d => {
    const ops = [{ op: "add_piece", value: clone(gone) }];
    const r = replacement && (d.pieces || []).find(p => p.id === replacement.id);
    if (r && !pieceOk(r) && r.w == null && !r.sketch) ops.push({ op: "remove_piece", id: replacement.id });
    return ops;
  };
  return { ops: [op], undo, replacement };
}

/** How a placed piece fits under the sloped ceilings (geometry.slope_clearance): {lowest, over} in cm, or null. */
export function slopeInfo(piece, doc, scene) {
  const g = doc?.geometry, sl = (g?.slopes || []).filter(s => s.normal?.[1] < -1e-6), pl = piece.placement;
  if (!scene || !sl.length || pl?.mode !== "3d" || !pl.pose) return null;
  let C;
  try { C = scene.boxCorners(piece).slice(4); } catch (e) { console.error(e); return null; }
  const ch = g.ceiling?.height || Infinity;
  const free = C.map(([x, , z]) => Math.min(ch, ...sl.map(s => -(s.normal[0] * x + s.normal[2] * z + s.offset) / s.normal[1])));
  const lowest = Math.max(0, Math.min(...free));
  return { lowest, over: Math.max(0, C[0][1] - lowest) };
}

/** Warning lines for the piece card (higher than the ceiling / wider than the wall / into a sloped ceiling). */
export function pieceWarnings(piece, doc, scene) {
  const out = [], pl = piece.placement;
  if (scene && pl?.mode === "3d" && pl.pose) {
    let r = null;
    try { r = scene.relations(piece); } catch (e) { console.error(e); }
    const sl = slopeInfo(piece, doc, scene);
    if (sl && sl.over > 3 && !piece.slope_fit) out.push(`Ragt ${Math.round(sl.over)} cm in die Dachschräge (dort nur ${Math.round(sl.lowest)} cm frei)!`);
    else if (r?.tooHigh && !(piece.slope_fit && sl?.over > 0)) out.push(`Höher als die Decke (${Math.round(r.ceilingHeight)} cm)!`);
    if (r?.tooWide) out.push(`Breiter als die Wand (${wallCm(r)} cm)!`);
  } else {
    const ch = doc?.geometry?.ok && doc.geometry.ceiling?.height;
    if (ch && piece.h && +piece.h > ch) out.push(`Höher als die Decke (${Math.round(ch)} cm)!`);
  }
  return out;
}

/** Best finished render for every piece without one (v2's autoRender): graded best, else newest. */
export function autoRenderOps(doc) {
  const ops = [], del = new Set(doc.deleted_variants || []);
  for (const p of doc.pieces || []) {
    if (p.render) continue;
    const rv = (doc.variants || []).filter(v => !del.has(v.id) && v.kind === "render" && (v.piece_ids || []).includes(p.id) && (!v.sketch || v.sketch === p.sketch));
    if (!rv.length || rv.some(v => v.judge?.status === "pending")) continue;
    const ok = rv.filter(v => v.judge?.status === "ok" && v.judge.overall != null);
    ops.push(set(`pieces.${p.id}.render`, (ok.length ? ok.reduce((a, b) => (b.judge.overall > a.judge.overall ? b : a)) : rv[0]).image));
  }
  return ops;
}

const parseCm = v => { const n = parseInt(v, 10); return n >= 1 ? Math.min(2000, n) : null; };
const dimSum = p => ([p.w, p.h, p.d].some(Boolean) ? `${p.w || "–"} × ${p.h || "–"} × ${p.d || "–"} cm${+p.elev > 0 ? `, hängt ${p.elev} cm hoch` : ""}` : "");

css("ws-moebel-css", `
.mb{display:flex;flex-direction:column;gap:12px;max-width:820px;margin:0 auto;width:100%}
.mb-card{border:1px solid var(--line);border-radius:var(--r,12px);background:var(--panel)}
.mb-card.open{border-color:var(--accent)}
.mb-head{width:100%;min-height:56px;display:flex;align-items:center;gap:10px;padding:8px 14px;background:none;border:0;color:var(--text);font:inherit;text-align:left;cursor:pointer}
.mb-head .dot{width:14px;height:14px;border-radius:50%;flex:none}
.mb-head b{font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mb-head small{color:var(--muted);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.mb-head .st{margin-left:auto;flex:none}
.mb-body{display:flex;flex-direction:column;gap:16px;padding:4px 14px 16px}
.mb-body h3{margin:0 0 6px;font-size:16px}
.mb .tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px}
.mb .tile{min-height:84px;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;padding:8px 4px;border:1px solid var(--line);
 border-radius:var(--r,12px);background:var(--bg);color:var(--text);font:inherit;font-size:14px;text-align:center;cursor:pointer;line-height:1.2;overflow-wrap:anywhere;hyphens:auto}
.mb .tile.active{border-color:var(--accent);background:var(--accent-soft);box-shadow:inset 0 0 0 1px var(--accent);font-weight:600}
.mb-dims{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
.mb-dims label{display:flex;flex-direction:column;gap:4px;font-size:14px;color:var(--muted);min-width:0}
.mb-dims .u{display:flex;align-items:center;gap:4px}
.mb-dims input{width:100%;min-width:0;min-height:52px;font-size:20px;text-align:center;padding:6px}
.mb-warn{color:var(--warn);font-size:15px;margin:0}
.mb .chips{display:flex;flex-wrap:wrap;gap:8px}
.mb .chip{min-height:44px}
.mb textarea,.mb input[type=text]{width:100%;min-height:44px;font-size:16px}
.mb-sk{display:flex;flex-direction:column;gap:10px}
.mb-sk .row{display:flex;gap:8px;flex-wrap:wrap}.mb-sk .row .btn{flex:1;min-width:160px}
.mb-sk img{max-width:160px;max-height:120px;border-radius:8px;object-fit:contain;background:#f2f1ee}
.mb-foot{display:flex;gap:8px;flex-wrap:wrap}.mb-foot .btn{flex:1;min-width:180px}
.mb-del{display:flex;justify-content:flex-end}.mb-del .btn{color:var(--danger)}
.mb .muted{color:var(--muted);font-size:14px;margin:0}
`);

export default {
  id: "moebel", title: "Möbel",
  state(doc) { return { done: (doc.pieces || []).length > 0 && doc.pieces.every(pieceOk), available: true, reason: null }; },
  mount(root, sctx) {
    const { store } = sctx;
    let alive = true, moreWoods = false, renderEst = null, skOpen = false;
    const unbinds = [];
    const doc = () => store.doc, cat = () => store.catalog || { pieces: {}, materials: {}, handles: {} };
    const active = () => activePiece(doc(), sctx.view);
    const patch = ops => store.patch(ops, { now: true }).catch(e => toast(errText(e), { kind: "err" }));
    const select = id => { const v = sctx.view; v.active_piece = id; sctx.saveView(); moreWoods = false; skOpen = false; render(true); };
    // one instance for the whole visit: render() moves the same node, so a recording or a result survives re-renders
    const speak = understandCard({ store, select: id => { select(id); document.getElementById(`pc-${id}`)?.scrollIntoView?.({ block: "nearest" }); } });

    function head(p, i, open) {
      const st = !doc().room ? "" : placed(p) ? "platziert" : "nicht platziert";
      const mat = p.material === "custom" ? (p.material_custom || "Eigene Angabe") : cat().materials?.[p.material]?.de || "";
      return el("button.mb-head", { type: "button", "aria-expanded": String(open), on: { click: () => (open ? null : select(p.id)) } },
        el("i.dot", { style: { background: COLORS[i % COLORS.length] } }),
        el("span", { style: { minWidth: "0", display: "flex", flexDirection: "column" } }, el("b", {}, pieceName(p, i, cat())),
          el("small", {}, [dimSum(p), mat].filter(Boolean).join(" · ") || "Noch nicht festgelegt")),
        st ? el("span.badge.st" + (placed(p) ? ".ok" : ".info"), {}, st) : null);
    }

    function chips(map, cur, onPick, keys) {
      return el("div.chips", { role: "group" }, (keys || Object.keys(map)).filter(k => map[k]).map(k =>
        el("button.chip" + (cur === k ? ".active" : ""), { type: "button", "aria-pressed": String(cur === k), on: { click: () => onPick(k) } }, map[k].de.replace("…", ""))));
    }

    function woods(p) {
      const mats = cat().materials || {}, common = (cat().common_materials || ["eiche", "eicheweiss", "nuss", "weiss"]).filter(k => mats[k]);
      const showAll = moreWoods || (p.material && !common.includes(p.material));
      const pick = k => patch([set(`pieces.${p.id}.material`, k)]);
      const keys = showAll ? [...common, ...Object.keys(mats).filter(k => !common.includes(k) && k !== "custom")] : common;
      const box = el("div", {}, el("h3", {}, "Holz & Oberfläche"), chips(mats, p.material, pick, keys));
      const row = box.lastChild;
      if (showAll) row.append(el("button.chip" + (p.material === "custom" ? ".active" : ""), { type: "button", "aria-pressed": String(p.material === "custom"), on: { click: () => pick("custom") } }, "Eigene Angabe"));
      else row.append(el("button.chip", { type: "button", on: { click: () => { moreWoods = true; render(true); } } }, "Mehr Hölzer …"));
      if (p.material === "custom") {
        const inp = el("input", { type: "text", placeholder: "z. B. Lack in Salbeigrün RAL 6021", "aria-label": "Eigene Angabe" });
        unbinds.push(store.bind(inp, `pieces.${p.id}.material_custom`));
        box.append(el("div", { style: { marginTop: "8px" } }, inp));
      }
      return box;
    }

    function sketchPart(p) {
      const jobs = (store.projectJobs?.() || []).filter(j => j.dedupe === `render:${p.id}` && (j.state === "queued" || j.state === "running"));
      const judgeOk = !!(store.status?.judge?.available ?? store.queue?.judge?.available);
      const det = el("details.details", { open: skOpen || !!p.sketch || jobs.length > 0 || null, on: { toggle: e => { skOpen = e.target.open; } } },
        el("summary", {}, "Skizze oder CAD hinzufügen (optional)"));
      const body = el("div.mb-sk");
      det.append(body);
      const pickSketch = async camera => {
        const f = await pickImage({ camera });
        if (!f) return;
        try { const up = await upload(f, { maxSide: 2048 }); patch([set(`pieces.${p.id}.sketch`, up.path), set(`pieces.${p.id}.render`, null), set(`pieces.${p.id}.use_render`, false)]); }
        catch (e) { toast(errText(e), { kind: "err" }); }
      };
      if (!p.sketch) {
        body.append(el("p.muted", {}, "Foto der Zeichnung oder CAD-Screenshot."),
          el("div.row", {}, el("button.btn.secondary", { type: "button", on: { click: () => pickSketch(true) } }, "📷 Skizze fotografieren"),
            el("button.btn.secondary", { type: "button", on: { click: () => pickSketch(false) } }, "Skizze hochladen")));
        return det;
      }
      if (renderEst == null) {
        renderEst = 0;
        store.dryRun?.({ kind: "render", piece_id: p.id }).then(pl => { renderEst = pl?.estimate_s || 60; if (alive) render(true); }).catch(() => { renderEst = 60; });
      }
      const min = Math.max(1, Math.round((renderEst || 60) / 60));
      body.append(el("div.row", { style: { alignItems: "center" } }, el("img", { src: img(p.sketch), alt: "Skizze" }),
        el("button.btn.ghost", { type: "button", on: { click: () => patch([set(`pieces.${p.id}.sketch`, null), set(`pieces.${p.id}.render`, null), set(`pieces.${p.id}.use_render`, false)]) } }, "Skizze entfernen")));
      if (jobs.length) {
        const j = jobs[0], pct = j.steps ? Math.round(100 * (j.step || 0) / j.steps) : 10;
        body.append(el("p", { role: "status" }, "Wird gerendert …"), el("p.muted", {}, j.stage_text || ""), el("div.bar", {}, el("i", { style: { width: `${pct}%` } })));
      } else body.append(el("button.btn.secondary", { type: "button", on: { click: async () => {
        try { await store.generate({ kind: "render", piece_id: p.id }); } catch (e) { if (e?.status !== 409) toast(errText(e), { kind: "err" }); }
      } } }, `Skizze als Foto rendern (ca. ${min} Min.)`));
      if (p.render) {
        body.append(el("button.btn.ghost", { type: "button", style: { alignSelf: "flex-start", padding: "4px" }, "aria-label": "Rendering ansehen", on: { click: () => {
          sheet({ title: "Rendering", body: el("img", { src: img(p.render), alt: "Rendering", style: { maxWidth: "100%" } }), actions: [{ label: "Schließen", kind: "secondary" }] });
        } } }, el("img", { src: img(p.render), alt: "" })),
        toggleRow({ title: "Rendering als Vorlage verwenden", checked: !!p.use_render, onChange: v => patch([set(`pieces.${p.id}.use_render`, v)]) }));
      }
      if (judgeOk) body.append(el("button.btn.ghost", { type: "button", on: { click: e => readDims(p, e.currentTarget) } }, "Maße aus der Skizze übernehmen"));
      return det;
    }
    async function readDims(p, btn) {
      btn.disabled = true; btn.textContent = "Liest die Maße …";
      try {
        await store.flush?.();
        const r = await api("POST", `/api/workshop/${doc().id}/sketch_dims`, { piece_id: p.id });
        if (r.status !== "ok") return toast(r.comment || "Prüfer nicht verfügbar", { kind: "err" });
        const was = dimSum(p), got = ["w", "h", "d"].filter(k => r[k]);
        if (!got.length) return toast(`Keine Maße in der Skizze gefunden.${r.comment ? " " + r.comment : ""}`, { kind: "err" });
        await patch(got.map(k => set(`pieces.${p.id}.${k}`, r[k])));
        const now = dimSum({ ...p, ...Object.fromEntries(got.map(k => [k, r[k]])) });
        toast(`${now === was ? "Maße stimmen mit der Skizze überein" : "Maße aus der Skizze übernommen"}: ${now}${was && now !== was ? ` (vorher ${was})` : ""}.${r.comment ? " " + r.comment : ""}`);
      } catch (e) { toast(errText(e), { kind: "err" }); }
      finally { if (btn.isConnected) { btn.disabled = false; btn.textContent = "Maße aus der Skizze übernehmen"; } }
    }

    function body(p) {
      const c = cat(), scene = sceneFor(doc(), c);
      const tiles = el("div.tiles", { role: "group", "aria-label": "Was soll hin?" }, Object.entries(c.pieces || {}).map(([k, t]) =>
        el("button.tile" + (p.type === k ? ".active" : ""), { type: "button", "aria-pressed": String(p.type === k),
          on: { click: () => { if (p.type !== k) patch(typeOps(p, k, c)); } } }, el("span", { html: typeIcon(k) }), t.de)));
      const def = c.pieces?.[p.type]?.dims || [];
      const num = (k, lab, i) => {
        const inp = el("input", { type: "number", inputmode: "numeric", min: "1", max: "2000", placeholder: def[i] ? String(def[i]) : "", "aria-label": `${lab} in cm` });
        unbinds.push(store.bind(inp, `pieces.${p.id}.${k}`, { parse: parseCm, format: v => (v == null ? "" : String(v)) }));
        return el("label", {}, lab, el("span.u", {}, inp, "cm"));
      };
      const text = el("textarea", { rows: 2, placeholder: "z. B. 3 Türen, links ein offenes Fach mit 2 Böden, Sockel zurückgesetzt", "aria-label": "Besonderheiten (optional)" });
      unbinds.push(store.bind(text, `pieces.${p.id}.text`));
      return el("div.mb-body", {},
        el("div", {}, el("h3", {}, "Was soll hin?"), tiles),
        el("div", {}, el("h3", {}, "Maße"), el("div.mb-dims", {}, num("w", "Breite", 0), num("h", "Höhe", 1), num("d", "Tiefe", 2)),
          elevField(p),
          el("div.mb-warns", { "aria-live": "polite" }, warnNodes(p, scene))),
        woods(p),
        el("div", {}, el("h3", {}, "Griffe"), chips(c.handles || {}, p.handles, k => patch([set(`pieces.${p.id}.handles`, p.handles === k ? null : k)]))),
        el("label.field", {}, el("span.label", {}, "Besonderheiten (optional)"), text),
        sketchPart(p),
        p.type ? el("div.mb-3d", {}, el("button.btn.secondary", { type: "button", on: { click: () => open3d(p) } }, "3D-Modell ansehen & herunterladen")) : null,
        (doc().pieces || []).length > 1 ? el("div.mb-del", {}, el("button.btn.ghost", { type: "button", on: { click: () => remove(p) } }, "Möbel löschen …")) : null);
    }

    function open3d(p) {   // the piece as a true-to-size 3d model (from the dimensions, no AI): viewer + GLB / STL / Zuschnitt
      if (!customElements.get("model-viewer")) {
        const sc = document.createElement("script"); sc.type = "module"; sc.src = "/static/vendor/model-viewer.min.js"; document.head.append(sc);
      }
      const base = `/api/workshop/${doc().id}/model/${p.id}`, v = Date.now();
      const mv = document.createElement("model-viewer");
      Object.entries({ src: `${base}.glb?v=${v}`, "camera-controls": "", "auto-rotate": "", "shadow-intensity": "1", exposure: "1.1",
        "camera-orbit": "-30deg 70deg auto", "interaction-prompt": "none", alt: `3D-Modell: ${pieceName(p)}`,
        // AR on the phone: true to scale (no pinch-resize), on the floor or on the wall for wall-hung pieces
        ar: "", "ar-modes": "webxr scene-viewer quick-look", "ar-scale": "fixed", "ar-placement": +p.elev > 0 ? "wall" : "floor" }).forEach(([k, x]) => mv.setAttribute(k, x));
      const arBtn = el("button.btn.primary", { type: "button", slot: "ar-button", style: { position: "absolute", right: "10px", bottom: "10px" } }, "In echt ansehen (AR)");
      mv.append(arBtn);
      mv.style.cssText = "width:100%;height:min(60vh,420px);background:var(--panel2);border-radius:12px";
      const dl = (href, label) => el("a.btn.secondary", { href, download: "" }, label);
      const dims = [p.w, p.h, p.d].every(Boolean) ? `${p.w} × ${p.h} × ${p.d} cm` : "Standardmaße";
      const kind = cat().pieces?.[p.type]?.de || pieceName(p);
      sheet({ title: `3D-Modell: ${kind}`, body: el("div", { style: { display: "flex", flexDirection: "column", gap: "10px" } },
        mv, el("p.muted", { style: { margin: 0 } }, `Maßstabsgetreu aus den Maßen gebaut (${dims}): Korpus 19 mm, Rückwand 8 mm, Fachböden, Fronten. Drehen mit dem Finger. „In echt ansehen“ stellt es per Handy-Kamera in Originalgröße in den Raum (Handy mit AR, sichere Verbindung nötig).`),
        el("div", { style: { display: "flex", flexWrap: "wrap", gap: "8px" } },
          dl(`${base}.glb`, "3D-Datei (GLB)"), dl(`${base}.stl`, "STL 1:1 (CAD)"), dl(`${base}.stl?scale=10`, "STL 1:10 (3D-Druck)"),
          dl(`${base}.csv`, "Zuschnittliste (CSV)"))) });
    }

    function elevField(p) {   // wall-hung pieces: underside above the floor (0 = stands on the floor)
      const inp = el("input", { type: "number", inputmode: "numeric", min: "0", max: "400", placeholder: "0", "aria-label": "Abstand vom Boden in cm" });
      unbinds.push(store.bind(inp, `pieces.${p.id}.elev`, { parse: x => { const n = parseInt(x, 10); return n > 0 ? Math.min(400, n) : 0; },
        format: v => (v ? String(v) : "") }));
      return el("div", { style: { marginTop: "10px" } },
        el("div.mb-dims", {}, el("label", {}, "Abstand vom Boden", el("span.u", {}, inp, "cm"))),
        el("p.muted", { style: { margin: "4px 0 0", fontSize: "14px" } }, "Leer = steht auf dem Boden. Bei Wandregal und Hängeschrank: Höhe der Unterkante. In Schritt 3 setzt ein Tipp auf die Wand die Höhe automatisch."));
    }

    function add() {
      const d = doc();
      if ((d.pieces || []).length >= (cat().max_pieces || MAX)) return toast("Höchstens 4 Möbel pro Auftrag", { kind: "err" });
      const p = emptyPiece(active());
      const v = sctx.view; v.active_piece = p.id; sctx.saveView();
      patch([{ op: "add_piece", value: p }]);
    }
    function warnNodes(p, scene) {
      const sl = slopeInfo(p, doc(), scene);
      const slope = sl && (sl.over > 3 || p.slope_fit) ? toggleRow({ title: "An die Dachschräge anpassen",
        text: `Oberseite schräg zugeschnitten – an der niedrigsten Stelle etwa ${Math.round(sl.lowest)} cm hoch.`,
        checked: !!p.slope_fit, onChange: v => patch([set(`pieces.${p.id}.slope_fit`, v)]) }) : null;
      return [...pieceWarnings(p, doc(), scene).map(w => el("p.mb-warn", {}, "⚠ " + w)), slope, wideFixButtons(p, scene, sctx)];
    }
    async function remove(p) {
      const i = (doc().pieces || []).indexOf(p);
      if (!await confirmDialog({ title: "Möbel löschen", text: `„${pieceName(p, i, cat())}“ wird aus dem Auftrag entfernt.`, ok: "Löschen", danger: true })) return;
      const r = deleteOps(doc(), p.id);
      if (!r) return;
      const v = sctx.view;
      if (v.active_piece === p.id) { v.active_piece = r.replacement?.id || (doc().pieces.find(x => x.id !== p.id) || {}).id; sctx.saveView(); }
      patch(r.ops);
      toast("Möbel gelöscht", { undo: () => { const vv = sctx.view; vv.active_piece = p.id; sctx.saveView(); patch(r.undo(doc())); } });
    }

    let lastKey = null;
    function render(force) {
      if (!alive || !doc()) return;
      const focus = document.activeElement;
      const typing = root.contains(focus) && focus.matches("input,textarea");
      const d = doc(), a = active();
      const key = JSON.stringify([d.pieces, a?.id, moreWoods, store.projectJobs?.().map(j => [j.id, j.state, j.step]), !!store.status?.judge?.available, d.room, d.geometry?.scale, d.variants?.length, renderEst]);
      if (!force && key === lastKey) return;
      if (typing && !force) {   // keep the field; refresh only what the field does not own
        const w = root.querySelector(".mb-warns");
        if (w && a) w.replaceChildren(...warnNodes(a, sceneFor(d, cat())).filter(Boolean));
        const h = root.querySelector(".mb-card.open .mb-head");
        if (h && a) h.replaceWith(head(a, d.pieces.indexOf(a), true));
        return;
      }
      lastKey = key;
      const sy = typeof scrollY === "number" ? scrollY : 0;
      for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ }
      const full = (d.pieces || []).length >= (cat().max_pieces || MAX);
      root.replaceChildren(el("div.mb", {}, speak.el,
        (d.pieces || []).map((p, i) => el("section.mb-card" + (p === a ? ".open" : ""), { id: `pc-${p.id}` }, head(p, i, p === a), p === a ? body(p) : null)),
        el("div.mb-foot", {}, el("button.btn.secondary", { type: "button", disabled: full, on: { click: add } }, "＋ Weiteres Möbel")),
        full ? el("p.muted", {}, "Höchstens 4 Möbel pro Auftrag") : null));
      try { if (force !== "keep") scrollTo(0, sy); } catch { /* jsdom */ }
    }
    function auto() {
      const ops = autoRenderOps(doc());
      if (ops.length) patch(ops);
    }

    render(true); auto();
    return {
      onDoc() { render(); auto(); },
      onQueue() { render(); },
      onStatus() { render(); },
      primary() {
        const d = doc(), missing = (d.pieces || []).some(p => !pieceOk(p));
        return { label: d.room ? "Weiter: Platzieren" : "Weiter: Berechnen", disabled: false,
          warning: missing ? "Bitte wählen Sie aus, was hin soll." : null, run: () => sctx.next() };
      },
      unmount() { alive = false; for (const u of unbinds.splice(0)) try { u(); } catch { /* gone */ } },
    };
  },
};
