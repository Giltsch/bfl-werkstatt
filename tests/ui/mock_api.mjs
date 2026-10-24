// Playwright mock of the Werkstatt 3 backend (spec §10.7): /api/**, /outputs/**, /k/** and /static/kunde.html with
// in-memory state. PATCH uses the real op semantics (store.js mirror + server-side conflict rules of §5.1).
//   const mock = await installMock(context, {scenario: "busy,pupdown", routes: [[glob, handler]]});
// Scenarios (comma-separated): default · busy (Ben's job ahead + Ben present) · conflict (first PATCH of pieces.*.w
// collides with Ben) · offline (every API request fails) · restart (server.started changes from the 3rd sync) ·
// pupdown (template caption) · running (generate → 409 running unless force) · changed (first generate → 409 changed)
// · noident (no person chosen) · firstrun (no team yet) · empty (no orders) · geofail (room analysis fails).
// Extra `routes` are registered last, so Playwright tries them first; call route.fallback() to reach the mock.
import {readFileSync, existsSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {dirname, join, basename} from "node:path";
import {applyOps, diffPaths, getPath, setPath, opPaths, overlaps, deepEqual} from "../../static/ws/store.js";

const HERE = dirname(fileURLToPath(import.meta.url));
export const FIX = join(HERE, "fixtures");
export const ROOT = join(HERE, "..", "..");
const fx = name => JSON.parse(readFileSync(join(FIX, name), "utf8"));
const clone = v => JSON.parse(JSON.stringify(v));
const nowIso = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 19);
const hex = n => [...Array(n)].map(() => "0123456789abcdef"[Math.floor(Math.random() * 16)]).join("");
const PDF = Buffer.from("%PDF-1.4\n% Werkstatt mock\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n");
const ZIP = Buffer.from([0x50, 0x4b, 0x05, 0x06, ...new Array(18).fill(0)]);
const MIME = {png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", svg: "image/svg+xml", pdf: "application/pdf", zip: "application/zip", html: "text/html; charset=utf-8"};

const SET_PATHS = [/^(title|customer|setting|room_changes|ref|ext_ref|internal_notes|room|sample|room_size|tidy|protect|guide|mockup|count|quality|removes|geometry|calibration|appointment|status)$/,
  /^judge\.(enabled|auto_retry)$/, /^contact\.(salutation|phone|email|street|zip|city)$/,
  /^pieces\.[a-z0-9]{4,12}(\.(type|text|material|material_custom|handles|w|h|d|sketch|render|use_render|sample|where|elev|placement))?$/,
  /^variants\.[a-z0-9]{4,16}\.fav$/];
const STATUS_ORDER = ["anfrage", "termin", "entwurf", "kunde", "freigegeben", "auftrag", "fertig"];
const STATUS_LABELS = {anfrage: "Anfrage", termin: "Vor-Ort-Termin", entwurf: "Entwurf", kunde: "Beim Kunden", freigegeben: "Freigegeben",
  auftrag: "Auftrag", fertig: "Abgeschlossen", abgesagt: "Abgesagt"};

function imageSize(buf) {
  const png = buf.indexOf(Buffer.from([0x89, 0x50, 0x4e, 0x47]));
  if (png >= 0) return {w: buf.readUInt32BE(png + 16), h: buf.readUInt32BE(png + 20), type: "png"};
  let i = buf.indexOf(Buffer.from([0xff, 0xd8, 0xff]));
  if (i >= 0) {
    i += 2;
    while (i < buf.length - 9) {
      if (buf[i] !== 0xff) { i++; continue; }
      const m = buf[i + 1], len = buf.readUInt16BE(i + 2);
      if (m >= 0xc0 && m <= 0xc2) return {h: buf.readUInt16BE(i + 5), w: buf.readUInt16BE(i + 7), type: "jpg"};
      i += 2 + len;
    }
    return {w: 1344, h: 1008, type: "jpg"};
  }
  return {w: 1344, h: 1008, type: "png"};
}

function createState(scenarioStr, opts) {
  const sc = new Set(String(scenarioStr || "default").split(",").map(s => s.trim()));
  const settings = fx("settings.json"), me = fx("me.json");
  const team = sc.has("firstrun") ? [] : settings.team;
  settings.team = team; me.team = team;
  if (sc.has("noident") || sc.has("firstrun")) me.user = null;
  const docs = {};
  if (!sc.has("empty")) for (const f of ["doc-full.json", "doc-new.json", "doc-portrait.json"]) { const d = fx(f); docs[d.id] = d; }
  return {
    sc, opts, settings, me, docs, listExtra: sc.has("empty") ? [] : fx("list.json").filter(x => !docs[x.id]),
    catalog: fx("catalog.json"), started: "2026-10-18T09:58:40", listRev: 1760772000, syncCount: 0,
    jobs: [], uploads: new Map(), shares: {}, presence: null, conflictDone: false, changedDone: false, nextRef: 6,
    backup: {last: {at: "2026-10-18T03:00:12", file: "werkstatt-20261018-030000.tar.gz", bytes: 812345678, ok: true, error: null},
             target: settings.backup.target, keep: settings.backup.keep, count: 5, running: false, same_disk: true},
  };
}

export async function installMock(context, {scenario = "default", routes = [], jobSeconds = 8, geoDelay = 600} = {}) {
  const st = createState(scenario, {jobSeconds, geoDelay});
  const has = s => st.sc.has(s);
  const meId = () => (st.me.user && st.me.user.id) || "dev:8f3a1c2e";
  const meName = () => (st.me.user && st.me.user.name) || "Gerät 8f3a";
  const nameOf = id => (st.settings.team.find(t => t.id === id) || {}).name || (id === "kunde" ? "Kunde" : id === "system" ? "System" : String(id || ""));

  // ---------- jobs ----------
  const benJob = () => ({id: "bbbbbb0001", kind: "generate", owner: "ben", owner_name: "Ben", project: "b0b0b0b0b0b1",
    project_title: "Küche Huber", label: "2 Vorschläge", count: 2, created: Date.now() / 1000 - 60, fixed: true, dedupe: "design"});
  if (has("busy")) st.jobs.push(benJob());

  function phaseOf(j) {
    if (j.fixed) return {state: "running", phase: "image", done: 0, step: 5, steps: 16, eta: 170};
    if (j.state === "cancelled") return {state: "cancelled", phase: "cancelled", done: j.doneImages || 0};
    const T = st.opts.jobSeconds, t = Date.now() / 1000 - j.created;
    const cap = j.caption ? 0.2 * T : 0, q = 0.15 * T, imgT = (0.8 * T - cap) / j.count;
    if (t < q) return {state: "queued", phase: "waiting", done: 0, eta: T - t};
    if (t < q + cap) return {state: "running", phase: "caption", done: 0, step: Math.round((t - q) / cap * 1100), steps: 1100, eta: T - t};
    const k = Math.floor((t - q - cap) / imgT);
    if (k < j.count) return {state: "running", phase: "image", done: k, step: Math.max(1, Math.round(((t - q - cap) % imgT) / imgT * j.steps)), steps: j.steps, eta: T - t};
    const judging = j.judge && t < T + 1.5;
    return {state: "done", phase: judging ? "judge" : "done", done: j.count, eta: 0};
  }

  function stageText(j, ph, pos) {
    switch (ph.phase) {
      case "waiting": return ph.eta < 15 ? "Startet gleich …" : `In der Warteschlange – Platz ${pos}, Start in ca. ${Math.max(1, Math.round(ph.eta / 60))} Min.`;
      case "caption": return "Beschreibung wird vorbereitet …";
      case "image": return j.count === 1 ? `Bild wird gerechnet … Schritt ${ph.step}/${ph.steps}` : `Bild ${ph.done + 1} von ${j.count} wird gerechnet … Schritt ${ph.step}/${ph.steps}`;
      case "judge": return "Qualitätsprüfung …";
      case "done": return j.count === 1 ? "✓ Fertig – 1 Vorschlag" : `✓ Fertig – ${j.count} Vorschläge`;
      case "cancelled": return `Abgebrochen von ${j.cancelled_by || ""}`;
      default: return "";
    }
  }

  function tick() {
    for (const j of st.jobs) {
      if (j.fixed || j.state === "cancelled") continue;
      const ph = phaseOf(j);
      const doc = st.docs[j.project];
      while (doc && (j.doneImages || 0) < ph.done) {
        const n = j.doneImages || 0;
        j.doneImages = n + 1;
        const vid = "v" + hex(7).replace(/[^a-z0-9]/g, "a");
        const pieces = doc.pieces.map(p => ({id: p.id, label: p.label || "Möbel", material_label: p.material_label, handles_label: p.handles_label, w: p.w, h: p.h, d: p.d}));
        const image = `variant-${((n + (j.seq || 0)) % 3) + 1}.jpg`;
        const v = {id: vid, kind: j.vkind || "room", image, room: doc.room, size: [1344, 1008], seed: 1000 + n, job: j.id, fav: false, note: j.note || "",
          from: j.from || null, piece_ids: pieces.map(p => p.id), pieces, boxes: {}, judge: j.judge ? {status: "pending"} : null,
          by: j.owner, caption_backend: j.caption ? (has("pupdown") ? "template" : "vllm") : "reuse", steps: j.steps, active_piece: pieces[0] && pieces[0].id, locked: false};
        j.results.push({id: vid, width: 1344, height: 1008, seed: v.seed, judge: v.judge});
        mutate(j.project, d => {
          d.variants = [v, ...d.variants];
          if (STATUS_ORDER.indexOf(d.status) < 2) d.status = "entwurf";
        }, j.owner, true);
      }
      if (ph.state === "done" && ph.phase === "done" && j.state !== "done") {
        j.state = "done";
        if (doc) mutate(j.project, d => {
          d.pending_jobs = d.pending_jobs.filter(x => x !== j.id);
          for (const v of d.variants) if (v.job === j.id && v.judge && v.judge.status === "pending")
            v.judge = {status: "ok", model: "fake", scores: {room_preserved: 9, matches_request: 8, proportions: 8, realism: 8, artifacts: 9}, overall: 8.2, pass: true, comment: "", seconds: 1};
          d.activity.push({at: nowIso(), by: j.owner, what: "generated", text: j.count === 1 ? "1 Vorschlag berechnet" : `${j.count} Vorschläge berechnet`});
        }, j.owner, true);
      }
    }
  }

  function view(j, {lite = false} = {}) {
    const ph = phaseOf(j);
    const active = st.jobs.filter(x => ["queued", "running"].includes(phaseOf(x).state));
    let pos = active.indexOf(j);
    if (pos > 0 && phaseOf(active[0]).state === "running" && ph.state === "running") pos = 0;
    const ahead = pos > 0 ? active.slice(0, pos).slice(0, 3).map(x => ({owner_name: x.owner_name, project_title: x.project_title, label: x.label})) : [];
    const v = {id: j.id, kind: j.kind, state: ph.state, phase: ph.phase, owner: j.owner, owner_name: j.owner_name, project: j.project,
      project_title: j.project_title, label: j.label, source: `workshop:${j.project}`, class: "fg", count: j.count,
      done_images: ph.done, width: 1344, height: 1008, stage: ph.phase === "image" ? `Image ${ph.done + 1}/${j.count}` : ph.state === "queued" ? "Queued" : "Generating",
      stage_text: stageText(j, ph, Math.max(1, pos)), step: ph.step || 0, steps: ph.steps || 0, position: Math.max(0, pos), ahead,
      eta_start_s: ph.state === "queued" ? Math.round(0.15 * st.opts.jobSeconds) : 0, eta_done_s: Math.round(ph.eta || 0),
      progress: ph.state === "done" ? 1 : ph.state === "running" ? Math.min(0.97, Math.max(0, 1 - (ph.eta || 0) / (j.fixed ? 400 : 0.85 * st.opts.jobSeconds))) : 0,
      elapsed_s: Math.round(Date.now() / 1000 - j.created), results: (j.results || []).map(r => ({...r})),
      caption_backend: j.caption ? (has("pupdown") ? "template" : "vllm") : null, error: null, cancelled_by: j.cancelled_by || null,
      judge_state: j.judge ? (ph.phase === "judge" ? "running" : ph.state === "done" ? "done" : "pending") : "off", judge_stage: null,
      retry_job: null, retry_of: null, created: j.created, started: ph.state === "queued" ? null : j.created + 1, finished: ph.state === "done" ? Date.now() / 1000 : null,
      dedupe: j.dedupe || null};
    // like jobs.eta_text: images first, the KI grading (~30 s) after; the same line on every device
    const js = j.judge ? 30 : 0;
    v.eta_judge_s = js && ph.state !== "done" ? v.eta_done_s + js : 0;
    v.eta_text = ph.state === "running" ? (v.eta_done_s <= 15 ? "Bilder gleich fertig" : `Bilder in ${span(v.eta_done_s)}`) + (js ? `, Bewertung ${span(js)} danach` : "")
      : ph.phase === "judge" ? "Bewertung in ca. 30 Sek." : "";
    if (lite) { delete v.results; delete v.source; }
    return v;
  }

  function queueView(pid) {
    tick();
    const live = st.jobs.filter(j => ["queued", "running"].includes(phaseOf(j).state) || phaseOf(j).phase === "judge");
    const lite = live.map(j => view(j, {lite: true}));
    const running = lite.find(j => j.state === "running") || null;
    const mine = lite.filter(j => j.owner === meId());
    const pj = pid ? st.jobs.filter(j => j.project === pid && (Date.now() / 1000 - j.created) < 600).map(j => { const v = view(j); delete v.caption; return v; }) : [];
    const down = has("pupdown");
    return {busy: lite.length > 0, wait_s: lite.reduce((s, j) => s + (j.eta_done_s || 0), 0), eta_total_s: lite.reduce((s, j) => s + (j.eta_done_s || 0), 0),
      pup: {backend: down ? "template" : "vllm", state: down ? "down" : "up"}, judge: {available: true},
      running, jobs: lite, my_jobs: mine, project_jobs: pj};
  }

  function newJob(pid, {kind = "generate", count = 1, label, caption = true, steps = 16, vkind = "room", from = null, note = "", dedupe = "design", client_key}) {
    const doc = st.docs[pid];
    const j = {id: hex(10), kind, owner: meId(), owner_name: meName(), project: pid, project_title: doc ? doc.title || "Ohne Titel" : "",
      label, count, created: Date.now() / 1000, caption, steps, vkind, from, note, dedupe, client_key, results: [], judge: !!(doc && doc.judge && doc.judge.enabled),
      seq: st.jobs.length, state: "queued"};
    st.jobs.push(j);
    if (doc) mutate(pid, d => { d.pending_jobs = [...d.pending_jobs, j.id]; }, meId(), true);
    return j;
  }

  // ---------- docs ----------
  function summary(d) {
    const c = d.contact || {};
    return {id: d.id, title: d.title || "Ohne Titel", customer: d.customer, status: d.status, ref: d.ref, ext_ref: d.ext_ref, city: c.city || "",
      appointment: d.appointment, archived_at: d.archived_at, test: /^TEST\b/.test((d.title || "").trim()), retention_due: false,
      created_by: d.created_by, thumb: (d.variants.find(v => d.approved && v.id === d.approved.variant_id) || d.variants.find(v => v.fav) || d.variants[0] || {}).image || null,
      updated: d.updated, updated_by: d.updated_by, updated_by_name: nameOf(d.updated_by), variants: d.variants.length, versions: d.versions.length,
      approved: !!d.approved, attention: (d.versions.at(-1) || {}).status === "aenderung" ? "aenderung"
        : d.status === "freigegeben" && (d.approved || {}).via === "link" ? "freigegeben" : null, pending: d.pending_jobs.length, rev: d.rev,
      new: d.status === "freigegeben" && (d.approved || {}).via === "link" && !(d.approved || {}).seen_at, outdated: !!(d.approved || {}).outdated,
      q: [d.title, d.customer, d.ref, d.ext_ref, ...Object.values(c), (d.internal_notes || "").slice(0, 500)].filter(Boolean).join(" ").toLowerCase()};
  }

  function docFor(pid) {
    if (st.docs[pid]) return st.docs[pid];
    const e = st.listExtra.find(x => x.id === pid);
    if (!e) return null;
    const d = fx("doc-new.json");
    Object.assign(d, {id: pid, title: e.title, customer: e.customer, status: e.status, ref: e.ref, appointment: e.appointment,
      archived_at: e.archived_at, updated: e.updated, updated_by: e.updated_by, test: e.test});
    d.contact.city = e.city || "";
    st.docs[pid] = d;
    st.listExtra = st.listExtra.filter(x => x.id !== pid);
    return d;
  }

  function mutate(pid, fn, who, auto = false) {
    const before = st.docs[pid];
    const after = clone(before);
    fn(after);
    if (deepEqual(before, after)) return before;
    after.rev = before.rev + 1;
    after.frev = after.frev || {};
    for (const p of diffPaths(before, after)) after.frev[p] = auto ? [after.rev, who, nowIso(), "auto"] : [after.rev, who, nowIso()];
    after.updated = nowIso(); after.updated_by = who;
    for (const v of after.variants) v.locked = !!(after.versions.some(x => x.status !== "zurueckgezogen" && x.variant_ids.includes(v.id)) || (after.approved && after.approved.variant_id === v.id));
    st.docs[pid] = after;
    st.listRev++;
    return after;
  }

  function log(d, what, text, who) { d.activity = [...(d.activity || []), {at: nowIso(), by: who, what, text}].slice(-300); }
  function advance(d, target, who) {
    const a = STATUS_ORDER.indexOf(d.status), b = STATUS_ORDER.indexOf(target);
    if (b > a && a >= 0) { log(d, "status", `Status: ${STATUS_LABELS[d.status]} → ${STATUS_LABELS[target]}`, who); d.status = target; return true; }
    return false;
  }

  function patchDoc(pid, body) {
    const doc = st.docs[pid];
    if (!doc) return [404, {detail: "Auftrag nicht gefunden."}];
    const {base_rev = 0, ops = []} = body || {};
    if (!Array.isArray(ops) || ops.length > 200) return [400, {detail: "Ungültige Änderung: ops."}];
    for (const op of ops) {
      if (op.op === "set" && !SET_PATHS.some(r => r.test(op.path || ""))) return [400, {detail: `Ungültige Änderung: ${op.path}.`}];
      if (!["set", "add_piece", "remove_piece", "delete_variant", "restore_variant", "archive", "unarchive"].includes(op.op)) return [400, {detail: `Ungültige Änderung: ${op.op}.`}];
      if (op.op === "add_piece" && (doc.pieces.length >= 4 || !/^[a-z0-9]{4,12}$/.test((op.value || {}).id || ""))) return [400, {detail: "Höchstens 4 Möbel pro Auftrag."}];
      if (op.op === "remove_piece" && doc.pieces.length === 1 && doc.pieces[0].id === op.id && !(op.replacement && op.replacement.id && op.replacement.id !== op.id))
        return [400, {detail: `Ungültige Änderung: pieces.${op.id}.`}];
    }
    if (base_rev < (doc.frev_floor || 0)) return [409, {detail: {code: "stale", message: "Gleichzeitig geändert.", rev: doc.rev, doc}}];
    let cur = doc;
    if (has("conflict") && !st.conflictDone) {
      const op = ops.find(o => o.op === "set" && /^pieces\.[^.]+\.w$/.test(o.path));
      if (op) {
        st.conflictDone = true;
        const theirs = Number(op.value) === 220 ? 240 : 220;
        cur = mutate(pid, d => setPath(d, op.path, theirs), "ben");
      }
    }
    const gone = [], conflicts = [];
    for (const op of ops) for (const p of opPaths(op)) {
      const target = p.split(".").slice(0, 2).join(".");
      if ((p.startsWith("pieces.") || p.startsWith("variants.")) && !["add_piece", "restore_variant"].includes(op.op) && getPath(cur, target) === undefined)
        gone.push({path: p, by: cur.updated_by, by_name: nameOf(cur.updated_by)});
      const changes = !deepEqual(applyOps(cur, [op]), cur);
      if (!changes || /^pieces\.[^.]+\.placement$/.test(p)) continue;
      for (const [q, fr] of Object.entries(cur.frev || {})) {
        if (fr[0] <= base_rev || !overlaps(p, q)) continue;
        if (fr[3] === "auto" && p.startsWith("variants.")) continue;
        conflicts.push({path: p, mine: op.op === "set" ? op.value : op.value || null, theirs: getPath(cur, p) ?? null, by: fr[1], by_name: nameOf(fr[1]), at: fr[2]});
        break;
      }
    }
    if (gone.length) return [409, {detail: {code: "gone", message: "Gleichzeitig geändert.", rev: cur.rev, doc: cur, conflicts: gone}}];
    const locked = ops.find(o => o.op === "delete_variant" && (cur.variants.find(v => v.id === o.id) || {}).locked);
    if (locked) return [409, {detail: {code: "locked", message: "Dieser Vorschlag gehört zu einer gesendeten Version. Ziehen Sie zuerst die Version zurück.", rev: cur.rev, doc: cur, conflicts: [{path: `variants.${locked.id}`}]}}];
    if (conflicts.length) return [409, {detail: {code: "conflict", message: "Gleichzeitig geändert.", rev: cur.rev, doc: cur, conflicts}}];
    const who = meId();
    const after = mutate(pid, d => {
      const roomBefore = d.room;
      Object.assign(d, applyOps(d, ops));
      for (const op of ops) {
        if (op.op === "set" && (op.path === "customer" || op.path.startsWith("contact."))) log(d, "meta", "Kundendaten geändert", who);
        if (op.op === "set" && op.path === "status") log(d, "status", `Status: → ${STATUS_LABELS[op.value]}`, who);
        if (op.op === "add_piece") log(d, "piece_add", `Möbel hinzugefügt: ${op.value.label || "Möbel"}`, who);
        if (op.op === "archive") log(d, "archived", "Archiviert", who);
        if (op.op === "unarchive") log(d, "unarchived", "Aus dem Archiv geholt", who);
        if (op.op === "delete_variant") log(d, "deleted_variant", "Vorschlag gelöscht", who);
      }
      if (!roomBefore && d.room) { log(d, "photo", "Raumfoto hinzugefügt", who); advance(d, "termin", who); }
      for (const p of d.pieces) {
        const t = st.catalog.pieces[p.type];
        p.label = t ? t.de : p.label || "";
        const m = st.catalog.materials[p.material];
        p.material_label = p.material === "custom" ? p.material_custom : m ? m.de : "";
        const hd = st.catalog.handles[p.handles];
        p.handles_label = hd ? hd.de : "";
      }
    }, who);
    return [200, {rev: after.rev, doc: after}];
  }

  function remote(pid, ops, who = "ben") {
    return mutate(pid, d => Object.assign(d, applyOps(d, ops)), who);
  }

  function span(x) {
    if (x < 45) return `ca. ${Math.max(10, Math.floor(x / 10 + 0.5) * 10)} Sek.`;
    const m = Math.floor(x / 30 + 0.5) / 2;
    return `ca. ${Math.floor(m)}${m % 1 ? "½" : ""} Min.`;
  }
  function plan(doc, body) {
    const count = body.kind === "render" ? 1 : body.count || doc.count || 2;
    const quality = body.quality || doc.quality || "entwurf";
    const down = has("pupdown");
    const per = body.kind === "room" && quality === "entwurf" ? 34 : 61;
    const unplaced = doc.room ? doc.pieces.filter(p => p.placement.mode === "none").length : 0;
    const est = (down ? 0 : (doc.room ? 57 : 20)) + count * per, js = doc.judge?.enabled === false ? 0 : 30;
    return {warnings: unplaced ? [`${unplaced} Möbel noch nicht platziert – es wird an einer passenden Stelle ergänzt.`] : [],
      estimate_s: est, judge_s: js, estimate_text: `Bilder in ${span(est)}` + (js ? `, Bewertung ${span(js)} danach` : ""), wait_s: has("busy") ? 170 : 0,
      label: body.kind === "render" ? "Skizze: Möbel" : count === 1 ? "1 Vorschlag" : `${count} Vorschläge`,
      params: {count, quality}, refs: doc.room ? [doc.room] : [], roles: doc.room ? ["room"] : [],
      caption: {backend: down ? "template" : "vllm", state: down ? "down" : "up"}};
  }

  function customerData(token) {
    const sh = st.shares[token] || (token === "tokFixture0000000000000000000001" ? {pid: "a1b2c3d4e5f6", n: 1} : null);
    if (!sh) return [404, {detail: "Dieser Link ist nicht mehr gültig."}];
    const d = st.docs[sh.pid];
    const v = d && d.versions.find(x => x.n === sh.n);
    if (!v || v.status === "zurueckgezogen" || token.startsWith("tokExpired")) return [410, {detail: "Dieser Link ist nicht mehr gültig."}];
    const c = st.settings.company;
    return [200, {respond: !["freigegeben"].includes(v.status), status: v.status, expires: v.expires,
      company: {name: c.name, contact: [c.street, `${c.zip} ${c.city}`.trim(), c.phone, c.email].filter(Boolean).join(" · "), color: c.color, logo: null},
      title: d.title, customer: d.customer, ref: d.ref, version: v.n, date: v.created.slice(0, 10), note: v.customer_note,
      variants: v.snapshot.variants.map(x => ({n: x.n, id: x.id, label: `Entwurf ${x.n}`, before: `img/${x.before}`, after: `img/${x.after}`,
        before_label: x.before_label, after_label: x.after_label, rows: x.rows})),
      response: v.response, approved_variant: d.approved && d.approved.version === v.n ? d.approved.variant_id : null, pdf: "pdf", zip: "zip",
      texts: {ai: "KI-Visualisierung – unverbindlich", privacy: "Datenschutz: Ihre Fotos und Daten speichern wir nur auf unserem eigenen Rechner in der Werkstatt und nutzen sie nur für Ihr Angebot. Es werden keine Cloud-Dienste verwendet. Auf Wunsch löschen wir alles – eine kurze Nachricht genügt.",
              measures: "Maße laut Aufmaß. Preise und Ausführung siehe Angebot."}}];
  }

  // ---------- router ----------
  const J = (route, status, obj, headers = {}) => route.fulfill({status, contentType: "application/json", headers, body: JSON.stringify(obj)});
  const fileBody = (route, buf, type, headers = {}) => route.fulfill({status: 200, contentType: type, headers, body: buf});
  const fixtureFile = name => { const p = join(FIX, basename(name)); return existsSync(p) ? readFileSync(p) : null; };

  async function handle(route) {
    const req = route.request();
    const url = new URL(req.url());
    const p = url.pathname, m = req.method();
    if (has("offline") && (p.startsWith("/api/") || p.startsWith("/outputs/"))) return route.abort("internetdisconnected");
    let body = null;
    if (m !== "GET" && m !== "HEAD") { try { body = req.postDataJSON(); } catch { body = null; } }
    let r;
    if ((r = p.match(/^\/outputs\/(.+)$/))) {
      const rel = decodeURIComponent(r[1]);
      if (st.uploads.has(rel)) { const u = st.uploads.get(rel); return fileBody(route, u.buf, MIME[u.type]); }
      if (rel.endsWith(".pdf")) return fileBody(route, PDF, MIME.pdf);
      const f = fixtureFile(rel);
      return f ? fileBody(route, f, MIME[rel.split(".").pop().toLowerCase()] || "application/octet-stream") : route.fulfill({status: 404, body: "not found"});
    }
    if (p === "/static/kunde.html" || /^\/k\/[^/]+\/?$/.test(p)) {
      const f = join(ROOT, "static", "kunde.html");
      return fileBody(route, existsSync(f) ? readFileSync(f) : Buffer.from("<!doctype html><title>Kunde</title><p>kunde.html fehlt (Stub).</p>"), MIME.html);
    }
    if ((r = p.match(/^\/k\/([^/]+)\/(.+)$/))) {
      const [, token, rest] = r;
      const [code, data] = customerData(token);
      if (code !== 200) return J(route, code, data);
      if (rest === "data") {
        const sh = st.shares[token] || {pid: "a1b2c3d4e5f6", n: 1};
        const v = st.docs[sh.pid].versions.find(x => x.n === sh.n);
        if (v.status === "offen") mutate(sh.pid, d => {   // the customer's first view also counts as sent
          const x = d.versions.find(y => y.n === sh.n);
          if (x.sent_at === null) { Object.assign(x, {sent_at: nowIso(), sent_via: "opened", sent_by: "kunde"}); advance(d, "kunde", "kunde"); }
          x.status = "angesehen"; x.viewed_at = nowIso();
        }, "kunde", true);
        return J(route, 200, data, {"Cache-Control": "private, no-store"});
      }
      if (rest === "respond" && m === "POST") {
        if (!body || !(body.name || "").trim()) return J(route, 400, {detail: "Bitte Ihren Namen angeben."});
        if (!body.confirm) return J(route, 400, {detail: "Bitte bestätigen."});
        const sh = st.shares[token] || {pid: "a1b2c3d4e5f6", n: 1};
        let status = "aenderung";
        mutate(sh.pid, d => {
          const v = d.versions.find(x => x.n === sh.n);
          v.response = {variant_id: body.variant_id, action: body.action, name: body.name, comment: body.comment || "", at: nowIso(), via: "link"};
          if (body.action === "freigeben") {
            status = v.status = "freigegeben";
            d.approved = {version: v.n, variant_id: body.variant_id, at: nowIso(), by: "kunde", via: "link", name: body.name, comment: body.comment || ""};
            advance(d, "freigegeben", "kunde");
          } else { v.status = "aenderung"; if (d.status === "kunde") d.status = "entwurf"; }
        }, "kunde", true);
        return J(route, 200, {ok: true, status});
      }
      if (rest.startsWith("img/")) return fileBody(route, fixtureFile(/Vorher/.test(rest) ? "room.png" : /Entwurf-2/.test(rest) ? "variant-2.jpg" : "variant-1.jpg"), "image/jpeg");
      if (rest === "pdf") return fileBody(route, PDF, MIME.pdf);
      if (rest === "zip") return fileBody(route, ZIP, MIME.zip);
      return route.fulfill({status: 404, body: "not found"});
    }
    if (!p.startsWith("/api/")) return route.fallback();

    // identity, team, access
    if (p === "/api/me" && m === "GET") return J(route, 200, st.me);
    if (p === "/api/me" && m === "POST") {
      const u = st.settings.team.find(t => t.id === (body || {}).id);
      if (!u) return J(route, 404, {detail: "Unbekannte Person."});
      st.me.user = u; return J(route, 200, {user: u});
    }
    if (p === "/api/me" && m === "DELETE") { st.me.user = null; return J(route, 200, {ok: true}); }
    if (p === "/api/team" && m === "POST") {
      const name = String((body || {}).name || "").trim();
      if (!name) return J(route, 400, {detail: "Bitte einen Namen eingeben."});
      let id = name.toLowerCase().replace(/ä/g, "ae").replace(/ö/g, "oe").replace(/ü/g, "ue").replace(/ß/g, "ss").replace(/[^a-z0-9-]+/g, "-").replace(/^-|-$/g, "").slice(0, 20) || "person";
      for (let k = 2; st.settings.team.some(t => t.id === id); k++) id = id.replace(/-\d+$/, "") + "-" + k;
      const u = {id, name, color: body.color || "#59a7ff"};
      st.settings.team.push(u); st.me.team = st.settings.team;
      return J(route, 200, u);
    }
    if ((r = p.match(/^\/api\/team\/([^/]+)$/))) {
      const i = st.settings.team.findIndex(t => t.id === r[1]);
      if (i < 0) return J(route, 404, {detail: "Unbekannte Person."});
      if (m === "DELETE") { st.settings.team.splice(i, 1); return J(route, 200, {ok: true}); }
      Object.assign(st.settings.team[i], body || {}); return J(route, 200, st.settings.team[i]);
    }
    if (p === "/api/login") return J(route, 200, {ok: true});
    if (p === "/api/access" && m === "PUT") { st.settings.access.password_set = !!(body || {}).password; return J(route, 200, {password_set: st.settings.access.password_set}); }
    if (p === "/api/settings" && m === "GET") return J(route, 200, st.settings);
    if (p === "/api/settings" && m === "PUT") {
      for (const [k, v] of Object.entries(body || {})) st.settings[k] = v && typeof v === "object" && !Array.isArray(v) ? {...st.settings[k], ...v} : v;
      return J(route, 200, st.settings);
    }
    if (p === "/api/backup" && m === "GET") return J(route, 200, st.backup);
    if (p === "/api/backup" && m === "POST") { st.backup.last = {...st.backup.last, at: nowIso()}; st.backup.count++; return J(route, 200, st.backup); }
    if (p === "/api/status") {
      const q = queueView(null);
      return J(route, 200, {loaded: {distilled: true, base: false, pup: !has("pupdown"), depth: false}, engine: q.busy ? "busy" : "idle", queue: q.jobs.length,
        mem_free_gb: 41.2, mem_total_gb: 121.7, modes: {}, judge: {available: true, state: "active", model: "fake-judge"}, pup: q.pup,
        busy: q.busy, wait_s: q.wait_s, backup: {at: st.backup.last.at, ok: true}, version: "3.0", started: st.started});
    }
    if (p === "/api/queue") return J(route, 200, queueView(null));
    if (p === "/api/workshop/catalog") return J(route, 200, st.catalog);

    if (p === "/api/sync") {
      st.syncCount++;
      const pid = url.searchParams.get("project"), rev = Number(url.searchParams.get("rev") || 0);
      const q = queueView(pid);
      const doc = pid ? st.docs[pid] : null;
      const presence = !pid ? [] : st.presence || (has("busy") ? [{user: "ben", name: "Ben", color: "#7cc4a0", device: "Tablet", view: "platzieren", since: nowIso()}] : []);
      return J(route, 200, {t: nowIso(), server: {started: has("restart") && st.syncCount > 2 ? "2026-10-18T10:30:00" : st.started, version: "3.0"},
        me: st.me.user, list_rev: st.listRev, project: pid ? {rev: doc ? doc.rev : 0, doc: doc && doc.rev > rev ? doc : null, deleted: !doc} : null,
        presence, queue: q, news: Object.values(st.docs).filter(d => d.status === "freigegeben" && (d.approved || {}).via === "link" && !(d.approved || {}).seen_at).length});
    }

    if (p === "/api/projects/workshop" && m === "GET") {
      const a = url.searchParams.get("archived") || "0";
      const all = [...Object.values(st.docs).map(summary), ...st.listExtra];
      const list = all.filter(x => a === "all" || (a === "1" ? !!x.archived_at : !x.archived_at)).sort((x, y) => String(y.updated).localeCompare(String(x.updated)));
      return J(route, 200, list);
    }
    if (p === "/api/projects/workshop" && m === "POST") {
      const d = fx("doc-new.json");
      const b = body || {};
      Object.assign(d, {id: hex(12), title: b.title || "", customer: b.customer || "", created: nowIso(), updated: nowIso(), created_by: meId(),
        updated_by: meId(), ref: /^TEST\b/.test(b.title || "") ? "" : `2026-${String(st.nextRef++).padStart(3, "0")}`, appointment: b.appointment || null});
      d.contact = {...d.contact, phone: "", city: "", ...(b.contact || {})};
      d.pieces = [{...d.pieces[0], id: "n" + hex(5).replace(/[^a-z0-9]/g, "a")}];
      d.activity = [{at: nowIso(), by: meId(), what: "created", text: "Auftrag angelegt"}];
      st.docs[d.id] = d; st.listRev++;
      return J(route, 200, d);
    }
    if ((r = p.match(/^\/api\/projects\/workshop\/([a-z0-9]+)$/))) {
      const pid = r[1];
      if (m === "GET") { const d = docFor(pid); return d ? J(route, 200, d) : J(route, 404, {detail: "Auftrag nicht gefunden."}); }
      if (m === "PATCH") { tick(); const [c, data] = patchDoc(pid, body); return J(route, c, data); }
      if (m === "PUT") return J(route, 409, {detail: "Die Werkstatt wurde aktualisiert – bitte die Seite neu laden."});
    }
    if (p === "/api/upload" && m === "POST") {
      const buf = req.postDataBuffer() || Buffer.alloc(0);
      const size = imageSize(buf);
      const name = `uploads/mock-${st.uploads.size + 1}.png`;
      st.uploads.set(name, {buf: buf.slice(Math.max(0, buf.indexOf(size.type === "png" ? Buffer.from([0x89, 0x50]) : Buffer.from([0xff, 0xd8])))), ...size});
      return J(route, 200, {path: name, width: size.w, height: size.h});
    }
    if (p === "/api/geometry/analyze") {
      await new Promise(res => setTimeout(res, st.opts.geoDelay));
      const room = (body || {}).room;
      const u = st.uploads.get(room) || (room && room.includes("portrait") ? {w: 1008, h: 1344} : {w: 1344, h: 1008});
      if (has("geofail")) return J(route, 200, {version: 1, ok: false, error: "Kein Boden erkannt.", room});
      const g = fx(u.h > u.w ? "geometry-portrait.json" : "geometry.json");
      g.room = room; g.size = [u.w, u.h]; g.intrinsics.cx = u.w / 2; g.intrinsics.cy = u.h / 2;
      return J(route, 200, g);
    }
    if (p === "/api/geometry/calibrate") {   // mirrors geometry.add_reference: confirmed refs kept, a >10 % contradiction asks
      const b = body || {};
      const g = fx(String(b.room || "").includes("portrait") ? "geometry-portrait.json" : "geometry.json");
      g.room = b.room;
      st.refs = st.refs || {};
      let refs = b.reset ? [] : [...(st.refs[b.room] || [])];
      const ref = b.room_height_cm ? {kind: "height", cm: +b.room_height_cm, raw: 260} : b.reset ? null
        : {kind: "line", cm: Number(b.length_cm) || 92, raw: 92, mode: b.mode || "floor", ...(b.label ? {label: b.label} : {})};
      if (ref) {
        if (ref.kind === "height") refs = refs.filter(r => r.kind !== "height");
        const sNew = ref.cm / ref.raw, bad = refs.filter(r => Math.abs(r.raw * sNew / r.cm - 1) > 0.1);
        const cur = refs.length ? Math.exp(refs.reduce((a, r) => a + Math.log(r.cm / r.raw), 0) / refs.length) : 1;
        if (bad.length && b.on_conflict !== "replace") {
          const r = bad[0];
          g.scale = cur; g.ceiling.height = Math.round(260 * cur); g.refs = refs;
          return J(route, 200, {geometry: g, measured_cm: null, scale: cur, scaled: false, conflict: {kind: r.kind, label: r.label || null, cm: r.cm,
            implied_cm: Math.round(r.raw * sNew), ceiling_cm: Math.round(260 * sNew), new: {kind: ref.kind, label: ref.label || null, cm: ref.cm}, new_implied_cm: Math.round(ref.raw * cur)}});
        }
        refs = [...refs.filter(r => !bad.includes(r)), ref];
      }
      st.refs[b.room] = refs;
      const w = r => (r.kind === "height" ? 2 : 1), sum = refs.reduce((a, r) => a + w(r), 0);
      g.scale = refs.length ? Math.exp(refs.reduce((a, r) => a + w(r) * Math.log(r.cm / r.raw), 0) / sum) : 1;
      g.ceiling.height = Math.round(260 * g.scale * 10) / 10; g.refs = refs;
      return J(route, 200, {geometry: g, measured_cm: ref ? ref.raw : null, scale: g.scale, scaled: true});
    }
    if ((r = p.match(/^\/api\/workshop\/([a-z0-9]+)\/(generate|edit)$/))) {
      const [, pid, what] = r;
      const doc = docFor(pid);
      if (!doc) return J(route, 404, {detail: "Auftrag nicht gefunden."});
      const b = body || {};
      if (what === "generate" && b.dry_run) return J(route, 200, {job: null, plan: plan(doc, b)});
      if (what === "edit" && b.dry_run) return J(route, 200, {job: null, plan: {...plan(doc, {kind: "edit", count: 1}), estimate_s: 74, label: "Änderung"}});
      const existing = b.client_key && st.jobs.find(j => j.client_key === b.client_key);
      if (existing) return J(route, 200, {job: view(existing), plan: plan(doc, b)});
      if (has("running") && !b.force) {
        const bj = {...benJob(), project: pid, project_title: doc.title};
        return J(route, 409, {detail: {code: "running", message: "Für diesen Auftrag läuft schon eine Berechnung (Ben, Platz 2).", job: view(bj, {lite: true})}});
      }
      if (has("changed") && !st.changedDone && b.base_rev != null) {
        st.changedDone = true;
        const path = `pieces.${doc.pieces[0].id}.w`;
        const d2 = mutate(pid, d => setPath(d, path, (getPath(d, path) || 100) + 10), "ben");
        return J(route, 409, {detail: {code: "changed", message: "Der Auftrag wurde gerade von Ben geändert. Bitte kurz prüfen und dann erneut starten.", rev: d2.rev, doc: d2, changes: [{path, by_name: "Ben"}]}});
      }
      let j;
      if (what === "generate") {
        const kind = b.kind || "room", pl = plan(doc, b);
        j = newJob(pid, {count: pl.params.count, label: pl.label, steps: kind === "room" && pl.params.quality === "entwurf" ? 8 : 16, vkind: kind,
          dedupe: kind === "render" ? `render:${b.piece_id}` : "design", client_key: b.client_key, caption: !has("pupdown")});
      } else {
        const mat = (st.catalog.materials[b.material] || {}).de;
        j = newJob(pid, {count: 1, label: b.op === "material" ? `Holz: ${mat}` : b.op === "upscale" ? "Für Druck vergrößern" : "Änderung", vkind: b.op,
          from: b.variant_id, note: b.text || "", dedupe: `${b.op}:${b.variant_id}`, client_key: b.client_key, caption: b.op !== "upscale"});
      }
      return J(route, 200, {job: view(j), plan: plan(doc, b)});
    }
    if ((r = p.match(/^\/api\/jobs\/([a-z0-9]+)$/))) {
      const j = st.jobs.find(x => x.id === r[1]);
      if (!j) return J(route, 404, {detail: "Auftrag nicht gefunden."});
      if (m === "GET") return J(route, 200, view(j));
      if (m === "DELETE") {
        if (j.owner !== meId() && url.searchParams.get("force") !== "1")
          return J(route, 403, {detail: {code: "not_owner", owner_name: j.owner_name, message: `Dieser Auftrag gehört ${j.owner_name}. Trotzdem abbrechen?`}});
        j.state = "cancelled"; j.fixed = false; j.cancelled_by = meName();
        if (st.docs[j.project]) mutate(j.project, d => { d.pending_jobs = d.pending_jobs.filter(x => x !== j.id); log(d, "cancelled", `Berechnung abgebrochen (${meName()})`, meId()); }, meId(), true);
        return J(route, 200, {ok: true, state: "cancelled"});
      }
    }
    if ((r = p.match(/^\/api\/workshop\/([a-z0-9]+)\/(.+)$/))) {
      const [, pid, rest] = r;
      const doc = docFor(pid);
      if (!doc) return J(route, 404, {detail: "Auftrag nicht gefunden."});
      if (rest === "versions" && m === "POST") {
        const ids = (body || {}).variant_ids || [];
        if (!ids.length) return J(route, 400, {detail: "Bitte mindestens einen Entwurf wählen."});
        if (ids.length > 4) return J(route, 400, {detail: "Höchstens 4 Entwürfe pro Version."});
        if (ids.some(id => !doc.variants.some(v => v.id === id))) return J(route, 404, {detail: "Vorschlag nicht gefunden."});
        const n = Math.max(0, ...doc.versions.map(v => v.n)) + 1, token = "tok" + hex(29);
        const files = {pdf: `Visualisierung_${doc.ref}_V${n}.pdf`, zip: `Visualisierung_${doc.ref}_V${n}.zip`, logo: null,
          images: ["01_Vorher.jpg", ...ids.map((_, i) => `0${i + 2}_Entwurf-${i + 1}.jpg`)]};
        const version = {n, created: nowIso(), by: meId(), variant_ids: ids, customer_note: (body || {}).customer_note || "", status: "offen", token,
          expires: "2027-01-16", viewed_at: null, sent_at: null, sent_via: null, sent_by: null, response: null, dir: `exports/workshop-${pid}/v${n}`, files,
          snapshot: {title: doc.title, customer: doc.customer, ref: doc.ref, ext_ref: doc.ext_ref, date: nowIso().slice(0, 10), company: {name: st.settings.company.name},
            variants: ids.map((id, i) => ({n: i + 1, id, before: "01_Vorher.jpg", after: files.images[i + 1], before_label: "Vorher", after_label: "Nachher", rows: []}))}};
        st.shares[token] = {pid, n};
        const d2 = mutate(pid, d => { d.versions = [...d.versions, version]; log(d, "version_ready", `Version ${n} für den Kunden bereitgestellt (${ids.length} Entwürfe)`, meId()); }, meId(), true);
        const pub = st.settings.public_base_url, lan = `http://192.168.178.111:7860/k/${token}`;
        const publicLink = pub ? `${pub.replace(/\/$/, "")}/k/${token}` : null;
        version.link = publicLink || lan;
        return J(route, 200, {version, doc_rev: d2.rev, link: lan, public_link: publicLink, share_link: publicLink || lan,
          lan_only: !pub, qr_svg: '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21"><rect width="21" height="21" fill="#fff"/><path d="M1 1h7v7H1zM13 1h7v7h-7zM1 13h7v7H1z" fill="#000"/></svg>',
          pdf: `/api/workshop/${pid}/versions/${n}/pdf`, zip: `/api/workshop/${pid}/versions/${n}/zip`});
      }
      if ((r = rest.match(/^versions\/(\d+)\/(pdf|zip|qr\.svg)$/))) {
        const kind = r[2];
        if (kind === "qr.svg") return fileBody(route, Buffer.from('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 21 21"><rect width="21" height="21" fill="#fff"/></svg>'), MIME.svg);
        return fileBody(route, kind === "pdf" ? PDF : ZIP, MIME[kind], {"Content-Disposition": `attachment; filename="Visualisierung_V${r[1]}.${kind}"`});
      }
      if ((r = rest.match(/^versions\/(\d+)\/sent$/)) && m === "POST") {
        const n = Number(r[1]), via = (body || {}).via;
        if (!["email", "whatsapp", "link", "share", "qr", "pdf"].includes(via)) return J(route, 400, {detail: "Unbekannter Versandweg."});
        const d2 = mutate(pid, d => {
          const v = d.versions.find(x => x.n === n);
          if (!v || v.sent_at || !("sent_at" in v)) return;
          Object.assign(v, {sent_at: nowIso(), sent_via: via, sent_by: meId()});
          advance(d, "kunde", meId());
          log(d, "version_sent", `Version ${n} an den Kunden gesendet`, meId());
        }, meId(), true);
        return J(route, 200, {version: d2.versions.find(x => x.n === n), status: d2.status, doc_rev: d2.rev});
      }
      if ((r = rest.match(/^versions\/(\d+)\/withdraw$/))) {
        const n = Number(r[1]);
        const d2 = mutate(pid, d => { const v = d.versions.find(x => x.n === n); if (v) v.status = "zurueckgezogen"; log(d, "withdrawn", `Version ${n} zurückgezogen`, meId()); }, meId(), true);
        return J(route, 200, {version: d2.versions.find(x => x.n === n)});
      }
      if (rest === "approval/seen" && m === "POST") {
        mutate(pid, d => { if (d.approved && !d.approved.seen_at) d.approved.seen_at = nowIso(); }, meId(), true);
        return J(route, 200, {ok: true});
      }
      if (rest === "approval" && m === "POST") {
        const b = body || {};
        const d2 = mutate(pid, d => {
          d.approved = {version: b.version, variant_id: b.variant_id, at: nowIso(), by: meId(), via: b.via || "telefon", name: b.name || "", comment: b.comment || ""};
          advance(d, "freigegeben", meId());
          log(d, "approved", `Entwurf freigegeben – ${b.via === "papier" ? "auf Papier" : b.via === "vor_ort" ? "vor Ort" : "telefonisch"} (erfasst von ${meName()})`, meId());
        }, meId(), true);
        return J(route, 200, {approved: d2.approved, doc_rev: d2.rev});
      }
      if (rest === "approval" && m === "DELETE") {
        mutate(pid, d => { d.approved = null; log(d, "unapproved", "Freigabe zurückgenommen", meId()); }, meId(), true);
        return J(route, 200, {approved: null});
      }
      if (rest === "sheet" && m === "POST") return J(route, 200, {pdf: `/outputs/exports/workshop-${pid}/entwurf-20261018-101500/Visualisierung_${doc.ref}_Entwurf.pdf`, skipped: []});
      if (rest === "export") return fileBody(route, ZIP, MIME.zip, {"Content-Disposition": `attachment; filename="Auftrag_${doc.ref || pid}.zip"`});
      if (rest === "purge" && m === "POST") {
        const want = (doc.title || "").trim() || "LÖSCHEN";
        if (((body || {}).confirm || "").trim() !== want) return J(route, 400, {detail: "Bestätigung stimmt nicht – bitte den Titel genau eintippen."});
        delete st.docs[pid]; st.listRev++;
        return J(route, 200, {ok: true, files: 12});
      }
      if ((r = rest.match(/^judge\/([a-z0-9]+)$/))) {
        const verdict = {status: "ok", model: "fake", scores: {room_preserved: 8, matches_request: 8, proportions: 8, realism: 8, artifacts: 8}, overall: 7.9, pass: true, comment: "", seconds: 1};
        mutate(pid, d => { const v = d.variants.find(x => x.id === r[1]); if (v) v.judge = verdict; }, meId(), true);
        return J(route, 200, verdict);
      }
      if (rest === "sketch_dims") return J(route, 200, {status: "ok", w: 180, h: 75, d: 45, doors: 3, drawers: 0, comment: ""});
    }
    return J(route, 404, {detail: `Mock: ${m} ${p} nicht implementiert.`});
  }

  await context.route(u => { const p = new URL(u).pathname; return p.startsWith("/api/") || p.startsWith("/outputs/") || p.startsWith("/k/") || p === "/static/kunde.html"; }, handle);
  for (const [glob, handler] of routes) await context.route(glob, handler);
  return {
    state: st,
    remote,                                           // remote(pid, ops, who = "ben") — a colleague's edit, delivered by the next sync
    setScenario(s) { st.sc = new Set(String(s).split(",").map(x => x.trim())); },
    addScenario(s) { st.sc.add(s); },
    dropScenario(s) { st.sc.delete(s); },
    setPresence(list) { st.presence = list; },
    doc: pid => st.docs[pid],
    queue: pid => queueView(pid),
  };
}
