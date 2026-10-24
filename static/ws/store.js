// Werkstatt 3 client state (spec §5.2, §10.5): optimistic PATCH ops on a server base doc, one PATCH in flight per
// project, 409 handling (conflict/gone/locked/stale/changed/running), persisted offline queue, holds for gestures,
// bind() for inputs and the 3 s sync loop (doc, presence, queue). createStore() exists for tests; the app uses `store`.
import {api as defaultApi, uid, deviceLabel, fmt, statusLabel, pieceLabel, confirmDialog, img, debounce} from "./ui.js";

// ---------- pure helpers (client mirror of projects.py) ----------

const isObj = v => v !== null && typeof v === "object" && !Array.isArray(v);
const clone = v => v === undefined ? undefined : JSON.parse(JSON.stringify(v));
export function deepEqual(a, b) {
  if (a === b) return true;
  if (typeof a !== typeof b || a === null || b === null || typeof a !== "object") return false;
  if (Array.isArray(a) !== Array.isArray(b)) return false;
  const ka = Object.keys(a), kb = Object.keys(b);
  if (Array.isArray(a) ? a.length !== b.length : ka.length !== kb.length) return false;
  return ka.every(k => deepEqual(a[k], b[k]));
}

const LISTS = new Set(["pieces", "variants"]);
const SUBDICTS = new Set(["contact", "judge"]);
const IGNORED = new Set(["rev", "frev", "frev_floor", "updated", "updated_by", "thumb", "test", "activity"]);
const BOOKKEEPING = new Set(["rev", "frev", "frev_floor", "updated", "updated_by"]);

// changed_paths granularity (§5.1); {all: true} keeps derived keys (thumb, activity, labels) for re-rendering.
export function diffPaths(a, b, {all = false} = {}) {
  a = a || {}; b = b || {};
  const out = [], skip = all ? BOOKKEEPING : IGNORED;
  for (const k of new Set([...Object.keys(a), ...Object.keys(b)])) {
    if (skip.has(k)) continue;
    if (LISTS.has(k) && (Array.isArray(a[k]) || Array.isArray(b[k]))) {
      const ma = new Map((a[k] || []).map(x => [x && x.id, x])), mb = new Map((b[k] || []).map(x => [x && x.id, x]));
      for (const id of new Set([...ma.keys(), ...mb.keys()])) {
        const x = ma.get(id), y = mb.get(id);
        if (!x || !y) { out.push(`${k}.${id}`); continue; }
        for (const f of new Set([...Object.keys(x), ...Object.keys(y)])) {
          if (!all && ((k === "variants" && f === "locked") || (k === "pieces" && (f === "label" || f.endsWith("_label"))))) continue;
          if (!deepEqual(x[f], y[f])) out.push(`${k}.${id}.${f}`);
        }
      }
    } else if (SUBDICTS.has(k) && isObj(a[k]) && isObj(b[k])) {
      for (const f of new Set([...Object.keys(a[k]), ...Object.keys(b[k])]))
        if (!deepEqual(a[k][f], b[k][f])) out.push(`${k}.${f}`);
    } else if (!deepEqual(a[k], b[k])) out.push(k);
  }
  return out;
}

export function getPath(doc, path) {
  if (!doc) return undefined;
  const s = path.split(".");
  if (LISTS.has(s[0]) && s.length >= 2) {
    const item = (doc[s[0]] || []).find(x => x && x.id === s[1]);
    return s.length === 2 ? item : item ? item[s[2]] : undefined;
  }
  return s.reduce((o, k) => (o == null ? undefined : o[k]), doc);
}

export function setPath(doc, path, value) {
  const s = path.split(".");
  if (LISTS.has(s[0]) && s.length >= 2) {
    const list = doc[s[0]] || [];
    const i = list.findIndex(x => x && x.id === s[1]);
    if (i < 0) return false;
    if (s.length === 2) list[i] = {...value, id: s[1]};
    else list[i][s[2]] = value;
    return true;
  }
  let o = doc;
  for (const k of s.slice(0, -1)) { if (!isObj(o[k])) o[k] = {}; o = o[k]; }
  o[s[s.length - 1]] = value;
  return true;
}

export function opPaths(op) {
  switch (op.op) {
    case "set": return [op.path];
    case "add_piece": return [`pieces.${op.value && op.value.id}`];
    case "remove_piece": return [`pieces.${op.id}`];
    case "delete_variant": case "restore_variant": return [`variants.${op.id}`];
    case "archive": case "unarchive": return ["archived_at"];
    default: return [];
  }
}

export function overlaps(p, q) {
  const a = p.split("."), b = q.split("."), n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) if (a[i] !== b[i]) return false;
  return true;
}

function applyOne(d, op) {
  switch (op.op) {
    case "set": setPath(d, op.path, clone(op.value)); break;
    case "add_piece":
      d.pieces = d.pieces || [];
      if (op.value && !d.pieces.some(p => p.id === op.value.id) && d.pieces.length < 4) d.pieces.push(clone(op.value));
      break;
    case "remove_piece": {
      const i = (d.pieces || []).findIndex(p => p.id === op.id);
      if (i < 0) break;
      d.pieces.splice(i, 1);
      if (!d.pieces.length && op.replacement) d.pieces.push(clone(op.replacement));
      break;
    }
    case "delete_variant": {
      const i = (d.variants || []).findIndex(v => v.id === op.id);
      if (i < 0) break;
      const [v] = d.variants.splice(i, 1);
      d.trash = [...(d.trash || []).filter(x => x.id !== v.id), v].slice(-20);
      d.deleted_variants = [...(d.deleted_variants || []).filter(x => x !== v.id), v.id];
      break;
    }
    case "restore_variant": {
      const v = (d.trash || []).find(x => x.id === op.id);
      if (!v) break;
      d.trash = d.trash.filter(x => x.id !== op.id);
      d.deleted_variants = (d.deleted_variants || []).filter(x => x !== op.id);
      d.variants = [...(d.variants || []), v].sort((x, y) => String(y.image || "").localeCompare(String(x.image || "")));
      break;
    }
    case "archive": d.archived_at = d.archived_at || new Date().toISOString().slice(0, 19); break;
    case "unarchive": d.archived_at = null; break;
  }
}

export function applyOps(doc, ops) {
  if (!doc) return doc;
  const d = clone(doc);
  for (const op of ops || []) applyOne(d, op);
  return d;
}

export function rebase(base, pending) {
  return applyOps(base, (pending || []).map(e => (e && e.op && typeof e.op === "object" ? e.op : e)));
}

function opValue(op) {
  if (op.op === "set" || op.op === "add_piece") return op.value;
  if (op.op === "archive") return new Date().toISOString().slice(0, 19);
  if (op.op === "restore_variant") return true;
  return null;
}

const FIELD_LABELS = {title: "Titel", customer: "Kunde", ref: "Vorgangs-Nr.", ext_ref: "Angebots-Nr.", "contact.phone": "Telefon",
  "contact.email": "E-Mail", "contact.street": "Straße", "contact.zip": "PLZ", "contact.city": "Ort", appointment: "Termin",
  status: "Status", internal_notes: "Interne Notizen", setting: "Umgebung", room_changes: "Raumänderung", room: "Raumfoto",
  room_size: "Raumfoto", sample: "Holzmuster", tidy: "Aufräumen", protect: "Raum schützen", guide: "Umrisse",
  count: "Anzahl Vorschläge", quality: "Qualität", mockup: "3D-Modell", "judge.enabled": "KI-Prüfung", "judge.auto_retry": "Neu rechnen",
  removes: "Entfernen-Rahmen", geometry: "Vermessung", calibration: "Maßstab", archived_at: "Archiv"};
const PIECE_FIELDS = {type: "Möbelart", text: "Details", material: "Holz", material_custom: "Eigene Oberfläche", handles: "Griffe",
  w: "Breite", h: "Höhe", d: "Tiefe", sketch: "Skizze", render: "Rendering", use_render: "Rendering als Vorlage",
  sample: "Holzmuster", where: "Ort", elev: "Abstand vom Boden", placement: "Position", slope_fit: "An Dachschräge angepasst"};
const IMAGE_PATH = /^[\w./-]+\.(png|jpe?g|webp)$/i;

function safeStorage() {
  try { const s = globalThis.localStorage; s.getItem("x"); return s; }
  catch { const m = new Map(); return {getItem: k => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k),
    key: i => [...m.keys()][i] ?? null, get length() { return m.size; }}; }
}

// ---------- the store ----------

export function createStore({api = defaultApi, storage = safeStorage(), confirm = confirmDialog, debounceMs = 400,
                             syncMs = 3000, dashMs = 10000, backoff = [2000, 4000, 8000, 16000, 30000], autoSync = true} = {}) {
  const listeners = new Map();
  const binds = new Set();
  const projs = new Set();              // projects with unsent work (the open one + ones closed while saving)
  const holds = new Map();              // path → {n, value (frozen base value), toast}
  let proj = null, syncTimer = null, lastStarted = null, lastListRev = null, retryOnline = null;

  const S = {
    me: null, team: [], settings: null, catalog: null, status: null,
    doc: null, pid: null, queue: {}, presence: [], online: true, saveState: "saved", viewName: "",

    on(ev, fn) {
      if (!listeners.has(ev)) listeners.set(ev, new Set());
      listeners.get(ev).add(fn);
      return () => listeners.get(ev).delete(fn);
    },

    async init() {
      const [me, settings, catalog] = await Promise.all([api("GET", "/api/me"), api("GET", "/api/settings").catch(() => null),
        api("GET", "/api/workshop/catalog").catch(() => null)]);
      S.me = me; S.settings = settings; S.catalog = catalog;
      S.team = (me && me.team) || (settings && settings.team) || [];
      api("GET", "/api/status").then(s => { S.status = s; emit("status", s); }, () => {});
      if (autoSync) {
        if (typeof document !== "undefined") document.addEventListener("visibilitychange", () => { if (!document.hidden) S.syncNow(); });
        await S.syncNow();
      }
      return S;
    },

    open(pid) {
      if (proj && proj.pid === pid) return proj.ready;
      if (proj) S.close();
      const p = makeProj(pid);
      proj = p; S.pid = pid; S.doc = null;
      p.ready = (async () => {
        let doc;
        try { doc = await api("GET", `/api/projects/workshop/${pid}`); }
        catch (e) { if (proj === p) { proj = null; S.pid = null; } throw e; }
        p.base = doc;
        p.restore();
        if (proj === p) { recompute("load"); S.syncNow(); }
        p.send();
        return S.doc;
      })();
      return p.ready;
    },

    close() {
      if (!proj) return;
      for (const f of binds) f.flush();
      const p = proj;
      proj = null; S.pid = null; S.doc = null; S.presence = [];
      holds.clear();
      p.send();
      scheduleSync();
    },

    // baseRev: the rev the edited value was seen at (bind passes it when a colleague changed a focused field), so the
    // server reports a conflict instead of silently overwriting; a merged set keeps the oldest one
    patch(ops, {now = false, baseRev = null} = {}) {
      if (!proj || !proj.base) return Promise.reject(new Error("Kein Auftrag geöffnet."));
      const p = proj;
      const done = ops.map(op => new Promise((res, rej) => {
        const entry = {op: clone(op), waiters: [{res, rej}], baseRev};
        if (op.op === "set") {
          const i = p.pending.findIndex(e => e.op.op === "set" && e.op.path === op.path);
          if (i >= 0) {
            const old = p.pending[i].baseRev;
            entry.waiters.unshift(...p.pending[i].waiters); p.pending.splice(i, 1);
            if (old != null) entry.baseRev = entry.baseRev == null ? old : Math.min(old, entry.baseRev);
          }
        }
        p.pending.push(entry);
      }));
      p.persist();
      recompute("local");
      setSave("saving");
      if (now) queueMicrotask(() => p.send());
      else { clearTimeout(p.debTimer); p.debTimer = setTimeout(() => p.send(), debounceMs); }
      return Promise.all(done).then(() => undefined);
    },

    set(path, value, opts) { return S.patch([{op: "set", path, value}], opts); },

    async flush() {
      for (const f of binds) f.flush();
      await Promise.all([...projs].map(p => p.drain()));
    },

    hasPending(path) {
      return !!proj && [...(proj.inflight ? proj.inflight.entries : []), ...proj.pending].some(e => opPaths(e.op).includes(path));
    },

    view(pid = S.pid) {
      if (!pid) return {};
      views[pid] ||= (() => { try { return JSON.parse(storage.getItem(`ws.view.${pid}`)) || {}; } catch { return {}; } })();
      return views[pid];
    },
    saveView(pid = S.pid) {
      if (!pid || !views[pid]) return;
      try { storage.setItem(`ws.view.${pid}`, JSON.stringify(views[pid])); } catch { /* full */ }
    },
    setView(name) { S.viewName = name || ""; },

    projectJobs(pid = S.pid) { return (S.queue.project_jobs || []).filter(j => !pid || j.project === pid); },
    myJobs() { return S.queue.my_jobs || []; },
    pup() { return (S.queue && S.queue.pup) || {state: "up", backend: "vllm"}; },

    generate(body = {}) { return jobRequest("generate", body); },
    edit(body = {}) { return jobRequest("edit", body); },
    async dryRun(body = {}) {
      const pid = S.pid;
      const r = await api("POST", `/api/workshop/${pid}/generate`, {kind: "room", ...body, dry_run: true,
        active_piece: body.active_piece || S.view(pid).active_piece || (S.doc && S.doc.pieces[0] && S.doc.pieces[0].id)});
      return r.plan;
    },
    async cancel(jobId, {force = false} = {}) {
      try { const r = await api("DELETE", `/api/jobs/${jobId}?force=${force ? 1 : 0}`); S.syncNow(); return r; }
      catch (e) {
        if (e.status === 403 && e.code === "not_owner" && !force) {
          const ok = await confirm({title: "Berechnung abbrechen?", text: e.message, ok: "Trotzdem abbrechen", cancel: "Weiterlaufen lassen", danger: true});
          return ok ? S.cancel(jobId, {force: true}) : null;
        }
        throw e;
      }
    },

    ownerKey() {   // the scheduler's owner key of this device: team member id, else "dev:<8 hex>"
      if (!S.me) return null;
      return (S.me.user && S.me.user.id) || (S.me.device ? `dev:${String(S.me.device).slice(0, 8)}` : null);
    },

    nameOf(id) {
      if (!id) return "Jemand";
      const m = (S.team || []).find(t => t.id === id);
      if (m) return m.name;
      if (id === "kunde") return "Kunde";
      if (id === "system") return "System";
      if (String(id).startsWith("dev:")) return `Gerät ${String(id).slice(4, 8)}`;
      return id;
    },

    labelFor(path, doc = S.doc) {
      if (FIELD_LABELS[path]) return FIELD_LABELS[path];
      const s = path.split(".");
      if (s[0] === "pieces") {
        const pieces = (doc && doc.pieces) || [];
        const i = pieces.findIndex(p => p.id === s[1]);
        const name = pieceLabel(pieces[i], i < 0 ? pieces.length : i, S.catalog);
        return s.length === 2 ? name : `${PIECE_FIELDS[s[2]] || s[2]} (${name})`;
      }
      if (s[0] === "variants") {
        const i = ((doc && doc.variants) || []).findIndex(v => v.id === s[1]);
        return `Auswahl (Vorschlag ${i < 0 ? "?" : i + 1})`;
      }
      if (s[0] === "contact" || s[0] === "judge") return FIELD_LABELS[path] || s[1];
      return path;
    },

    formatValue(path, value, doc = S.doc) {
      const s = path.split("."), f = s[0] === "pieces" ? s[2] : s.length > 1 && !LISTS.has(s[0]) ? path : s[0];
      if (s[0] === "pieces" && s.length === 2) {
        if (!value) return "gelöscht";
        return pieceLabel(value, Math.max(0, ((doc && doc.pieces) || []).findIndex(p => p.id === s[1])), S.catalog);
      }
      if (s[0] === "variants" && s.length === 2) return value === true ? "wiederhergestellt" : value ? "vorhanden" : "gelöscht";
      if (typeof value === "boolean") return value ? "an" : "aus";
      if (f === "placement") return value && value.mode && value.mode !== "none" ? "neu platziert" : "nicht platziert";
      if (f === "geometry" || f === "calibration") return value ? "neu vermessen" : "zurückgesetzt";
      if (f === "removes") return `${(value || []).length} Entfernen-Rahmen`;
      if (value == null || value === "") return "(leer)";
      if (f === "status") return statusLabel(value);
      if (f === "appointment") return isObj(value) ? [fmt.dateTime(value.at), value.what].filter(Boolean).join(" · ") : "geändert";
      if (f === "archived_at") return "archiviert";
      if (typeof value === "string" && IMAGE_PATH.test(value)) return {thumb: img(value)};
      const cat = S.catalog || {};
      if (f === "w" || f === "h" || f === "d" || f === "elev") return fmt.cm(value);
      if (f === "type") return (cat.pieces && cat.pieces[value] && cat.pieces[value].de) || "geändert";
      if (f === "material") return (cat.materials && cat.materials[value] && cat.materials[value].de.replace(/…$/, "")) || "geändert";
      if (f === "handles") return (cat.handles && cat.handles[value] && cat.handles[value].de) || "geändert";
      if (f === "quality") return (cat.quality && cat.quality[value] && cat.quality[value].label) || (value === "fein" ? "Fein" : "Entwurf");
      if (typeof value === "number") return fmt.num(value, Number.isInteger(value) ? 0 : 1);
      if (typeof value === "string") return value.length > 160 ? value.slice(0, 157) + " …" : value;
      return "geändert";
    },

    hold(path) {
      const h = holds.get(path);
      if (h) h.n++;
      else holds.set(path, {n: 1, value: clone(getPath(proj && proj.base, path)), toast: null});
    },
    release(path) {
      const h = holds.get(path);
      if (!h) return;
      if (--h.n > 0) return;
      queueMicrotask(() => {
        if (holds.get(path) !== h) return;
        holds.delete(path);
        const before = clone(getPath(S.doc, path));
        recompute("remote", {by_name: h.toast});
        if (h.toast && !deepEqual(before, getPath(S.doc, path))) placementToast(path, h.toast);
      });
    },

    bind(input, path, {parse, format, debounce: ms = debounceMs} = {}) {
      const check = input.type === "checkbox", discrete = check || input.tagName === "SELECT";
      format = format || (v => v ?? "");
      parse = parse || (input.type === "number" ? (t => (t === "" ? null : Number(String(t).replace(",", ".")))) : (t => t));
      let shownRev = null, stale = null;   // the rev the input shows; set when a colleague changed it while focused
      const render = () => {
        const v = getPath(S.doc, path);
        shownRev = proj && proj.base ? proj.base.rev : null; stale = null;
        if (check) input.checked = !!v;
        else { const t = String(format(v) ?? ""); if (input.value !== t) input.value = t; }
      };
      const write = () => {
        const baseRev = stale; stale = null;
        return S.patch([{op: "set", path, value: check ? input.checked : parse(input.value)}], {now: true, baseRev}).catch(() => {});
      };
      const d = debounce(write, ms);
      const onInput = () => (discrete ? write() : d());
      const onBlur = () => { d.flush(); if (!S.hasPending(path)) render(); };
      input.addEventListener(discrete ? "change" : "input", onInput);
      input.addEventListener("blur", onBlur);
      const off = S.on("doc", evt => {
        if (typeof document !== "undefined" && document.activeElement === input) {
          if (evt && evt.source === "remote" && (evt.paths || []).some(q => overlaps(q, path)) && stale == null) stale = shownRev;
          return;
        }
        render();
      });
      binds.add(d);
      render();
      return () => {
        d.flush(); binds.delete(d); off();
        input.removeEventListener(discrete ? "change" : "input", onInput);
        input.removeEventListener("blur", onBlur);
      };
    },

    async switchUser() {
      try {
        await Promise.race([S.flush(), new Promise((_, rej) => setTimeout(() => rej(new Error("timeout")), 8000))]);
        return true;
      } catch {
        const ok = await confirm({title: "Ungespeicherte Änderungen verwerfen?",
          text: "Ohne Verbindung lassen sich Ihre letzten Änderungen gerade nicht speichern.", ok: "Verwerfen", danger: true});
        if (!ok) return false;
        for (const p of projs) p.discard();
        if (proj) proj.discard();
        for (let i = storage.length - 1; i >= 0; i--) {
          const k = storage.key(i);
          if (k && k.startsWith("ws.pending.")) storage.removeItem(k);
        }
        recompute("local");
        return true;
      }
    },

    syncNow() { clearTimeout(syncTimer); return syncOnce(); },
    stop() { clearTimeout(syncTimer); syncTimer = null; S.stopped = true; },
  };
  const views = {};

  function emit(ev, data) {
    for (const fn of [...(listeners.get(ev) || [])]) {
      try { fn(data); } catch (e) { console.error(e); }
    }
  }

  function setSave(state) {
    if (S.saveState === state) return;
    S.saveState = state;
    emit("save", {state});
  }

  function netOk() {
    clearTimeout(retryOnline);
    if (!S.online) { S.online = true; emit("online", {}); }
  }
  function netFail() {
    if (S.online) { S.online = false; emit("offline", {}); }
  }

  function effectiveBase() {
    if (!proj || !proj.base) return null;
    if (!holds.size) return proj.base;
    const b = clone(proj.base);
    for (const [path, h] of holds) if (h.value !== undefined && getPath(b, path) !== undefined) setPath(b, path, clone(h.value));
    return b;
  }

  function recompute(source, extra = {}) {
    const prev = S.doc;
    const b = effectiveBase();
    S.doc = b ? applyOps(b, [...(proj.inflight ? proj.inflight.entries : []), ...proj.pending].map(e => e.op)) : null;
    const paths = diffPaths(prev, S.doc, {all: true});
    if (paths.length || source === "load") emit("doc", {source, paths, by_name: extra.by_name || null});
    return paths;
  }

  function placementToast(path, byName) {
    const id = path.split(".")[1];
    const pieces = (S.doc && S.doc.pieces) || [];
    const i = pieces.findIndex(p => p.id === id);
    if (i < 0) return;
    emit("toast", {text: `${byName} hat ${pieceLabel(pieces[i], i, S.catalog)} verschoben`});
  }

  function isMine(who) {
    if (!who || !S.me) return false;
    return (S.me.user && who === S.me.user.id) || (S.me.device && who === `dev:${String(S.me.device).slice(0, 8)}`);
  }

  // A pending set whose field a colleague changed meanwhile keeps the rev it was edited against: the server then reports
  // the conflict (mine / theirs) instead of silently overwriting the colleague's value.
  function stamp(p, oldBase, doc, own = []) {
    if (!oldBase || !doc) return;
    for (const e of p.pending) {
      if (e.baseRev != null || e.op.op !== "set" || own.some(q => overlaps(q, e.op.path))) continue;
      const fr = (doc.frev || {})[e.op.path];
      if (!deepEqual(getPath(oldBase, e.op.path), getPath(doc, e.op.path)) && !(fr && isMine(fr[1]))) e.baseRev = oldBase.rev;
    }
  }

  function applyRemote(p, doc) {
    if (p !== proj || !doc || (p.base && doc.rev <= p.base.rev)) return;
    const byName = S.nameOf(doc.updated_by);
    stamp(p, p.base, doc);
    p.base = doc;
    for (const [path, h] of holds) if (!deepEqual(getPath(doc, path), h.value)) h.toast = byName;
    const paths = recompute("remote", {by_name: byName});
    if (!isMine(doc.updated_by))
      for (const path of paths) if (/^pieces\.[^.]+\.placement$/.test(path) && !holds.has(path)) placementToast(path, byName);
  }

  // ask(): emits a "conflict" event with resolve(); resolves with the listener's choice (null without listener).
  function ask(data) {
    return new Promise(resolve => {
      if (!(listeners.get("conflict") || new Set()).size) return resolve(null);
      emit("conflict", {...data, resolve});
    });
  }

  async function jobRequest(kind, body) {
    for (const f of binds) f.flush();
    try { await S.flush(); } catch { /* offline: the POST below reports it */ }
    const p = proj;
    if (!p) throw new Error("Kein Auftrag geöffnet.");
    const payload = {...body, client_key: body.client_key || uid("c-"), base_rev: body.base_rev ?? (p.base && p.base.rev)};
    if (kind === "generate" && !payload.active_piece)
      payload.active_piece = S.view(p.pid).active_piece || (S.doc && S.doc.pieces[0] && S.doc.pieces[0].id);
    try {
      const r = await api("POST", `/api/workshop/${p.pid}/${kind}`, payload);
      if (r && r.job && S.queue) {   // visible at once (⑤ is available while a job runs), the next sync replaces it
        S.queue = {...S.queue, project_jobs: [r.job, ...(S.queue.project_jobs || []).filter(j => j.id !== r.job.id)]};
        emit("queue", S.queue);
      }
      S.syncNow();
      return r;
    } catch (e) {
      if (e.status !== 409 || !e.data) throw e;
      if (e.code === "changed") {
        if (e.data.doc) applyRemote(p, e.data.doc);
        const choice = await ask({kind: "changed", message: e.message, changes: e.data.changes || []});
        if (choice === "force" || choice === "start") return jobRequest(kind, {...payload, base_rev: e.data.rev});
        return null;
      }
      if (e.code === "running") {
        const choice = await ask({kind: "running", message: e.message, job: e.data.job || null});
        if (choice === "force" || choice === "start") return jobRequest(kind, {...payload, force: true});
        if (choice === "goto") return {job: e.data.job || null, running: true};
        return null;
      }
      throw e;
    }
  }

  function makeProj(pid) {
    const p = {pid, base: null, pending: [], inflight: null, debTimer: null, retryTimer: null, attempts: 0, drainers: [], ready: null};

    p.key = `ws.pending.${pid}`;
    p.persist = () => {
      const entries = [...(p.inflight ? p.inflight.entries : []), ...p.pending];
      if (!entries.length) { try { storage.removeItem(p.key); } catch { /* ignore */ } return; }
      const groups = [];
      for (const e of entries) {
        const br = e.baseRev ?? (p.base && p.base.rev) ?? 0;
        if (groups.length && groups[groups.length - 1].base_rev === br) groups[groups.length - 1].ops.push(e.op);
        else groups.push({base_rev: br, ops: [e.op]});
      }
      try { storage.setItem(p.key, JSON.stringify({user: (S.me && S.me.user && S.me.user.id) || null, groups})); } catch { /* full */ }
      projs.add(p);
    };
    p.restore = () => {
      let saved = null;
      try { saved = JSON.parse(storage.getItem(p.key)); } catch { saved = null; }
      if (!saved || !Array.isArray(saved.groups)) return;
      const me = (S.me && S.me.user && S.me.user.id) || null;
      if (saved.user !== me) { storage.removeItem(p.key); return; }   // never re-sent under another name
      for (const g of saved.groups) for (const op of g.ops || []) p.pending.push({op, waiters: [], baseRev: g.base_rev, restored: true});
      if (p.pending.length) projs.add(p);
    };
    p.discard = () => {
      for (const e of [...p.pending, ...(p.inflight ? p.inflight.entries : [])]) e.waiters.forEach(w => w.res());
      p.pending = []; p.inflight = null;
      clearTimeout(p.debTimer); clearTimeout(p.retryTimer);
      try { storage.removeItem(p.key); } catch { /* ignore */ }
      p.settle(null);
    };
    p.idle = () => !p.pending.length && !p.inflight;
    p.settle = (err) => {
      if (!p.idle() && !err) return;
      const ds = p.drainers; p.drainers = [];
      ds.forEach(d => (err ? d.rej(err) : d.res()));
      if (p.idle()) { projs.delete(p); if (p === proj || !proj) setSave("saved"); }
    };
    p.drain = () => {
      if (p.idle()) return Promise.resolve();
      const pr = new Promise((res, rej) => p.drainers.push({res, rej}));
      clearTimeout(p.retryTimer); p.retryTimer = null;
      p.send();
      return pr;
    };
    p.requeue = (entries) => { p.pending = [...entries.map(e => ({...e, baseRev: null})), ...p.pending]; };

    p.send = async () => {
      if (p.inflight || !p.pending.length || !p.base || p.retryTimer) return;
      clearTimeout(p.debTimer);
      const br0 = p.pending[0].baseRev;
      let n = 0;
      while (n < p.pending.length && n < 200 && p.pending[n].baseRev === br0) n++;
      const batch = p.pending.splice(0, n);
      const baseRev = br0 ?? p.base.rev;
      p.inflight = {entries: batch, baseRev};
      projs.add(p);
      let r;
      try {
        r = await api("PATCH", `/api/projects/workshop/${p.pid}`, {base_rev: baseRev, ops: batch.map(e => e.op)});
      } catch (e) {
        p.inflight = null;
        return p.failed(e, batch);
      }
      p.inflight = null;
      p.attempts = 0;
      netOk();
      if (r && r.doc && (!p.base || r.doc.rev >= p.base.rev)) {
        const old = p.base;
        stamp(p, old, r.doc, batch.flatMap(e => opPaths(e.op)));
        p.base = r.doc;
        if (p === proj) for (const [path, h] of holds)
          if (batch.some(e => opPaths(e.op).some(q => overlaps(q, path)))) h.value = clone(getPath(r.doc, path));
        if (!old || old.rev !== r.doc.rev) { /* own write: not a remote change */ }
      }
      batch.forEach(e => e.waiters.forEach(w => w.res()));
      p.persist();
      if (p === proj) recompute("server");
      if (p.pending.length) p.send(); else p.settle();
    };

    p.failed = async (e, batch) => {
      if (e.status === 0 || e.status >= 500) {                     // network / server hiccup: keep everything, retry
        p.pending = [...batch, ...p.pending];
        p.persist();
        if (e.status === 0) { netFail(); setSave("offline"); }
        const ms = backoff[Math.min(p.attempts, backoff.length - 1)];
        p.attempts++;
        const ds = p.drainers; p.drainers = [];
        ds.forEach(d => d.rej(e));
        p.retryTimer = setTimeout(() => { p.retryTimer = null; p.send(); }, ms);
        return;
      }
      if (e.status === 409 && e.data && e.data.code) return p.conflict(e.data, batch, e);
      // 400 and friends: these ops are invalid — drop them, keep the rest going
      batch.forEach(x => x.waiters.forEach(w => w.rej(e)));
      if (!batch.every(x => x.restored)) emit("error", {message: `Speichern fehlgeschlagen: ${e.message}`});
      p.persist();
      if (p === proj) recompute("server");
      if (p.pending.length) p.send(); else p.settle();
    };

    p.conflict = async (detail, batch, err) => {
      const code = detail.code;
      const oldBase = p.base;
      if (code === "stale") {
        let fresh;
        try { fresh = await api("GET", `/api/projects/workshop/${p.pid}`); }
        catch (e2) { return p.failed(e2.status ? e2 : {status: 0}, batch); }
        p.base = fresh;
        const ok = [], clash = [];
        for (const e of batch) {
          if (e.op.op !== "set") { ok.push(e); continue; }
          (deepEqual(getPath(oldBase, e.op.path), getPath(fresh, e.op.path)) ? ok : clash).push(e);
        }
        p.requeue(ok);
        const items = clash.map(e => {
          const fr = (fresh.frev || {})[e.op.path];
          return item(e, getPath(fresh, e.op.path), fr ? S.nameOf(fr[1]) : S.nameOf(fresh.updated_by));
        });
        return p.afterConflict(items, "conflict");
      }
      if (detail.doc) p.base = detail.doc;
      const cs = detail.conflicts || [];
      const hit = e => opPaths(e.op).some(q => cs.some(c => overlaps(q, c.path)));
      if (code === "conflict") {
        const clash = batch.filter(hit), ok = batch.filter(e => !hit(e));
        p.requeue(ok);
        const items = clash.map(e => {
          const path = opPaths(e.op)[0];
          const c = cs.find(x => overlaps(path, x.path)) || {};
          return item(e, "theirs" in c ? c.theirs : getPath(p.base, path), c.by_name || S.nameOf(c.by));
        });
        return p.afterConflict(items, "conflict");
      }
      if (code === "gone" || code === "locked") {
        const drop = code === "gone" ? batch.filter(e => hit(e) || targetMissing(p.base, e.op))
          : batch.filter(e => (cs.length ? hit(e) : e.op.op === "delete_variant"));
        p.requeue(batch.filter(e => !drop.includes(e)));
        drop.forEach(e => e.waiters.forEach(w => w.res()));
        const c = cs[0] || {};
        const message = code === "gone" ? `Dieses Möbel wurde inzwischen von ${c.by_name || S.nameOf(c.by || p.base.updated_by)} gelöscht.` : err.message;
        if (p === proj) emit("conflict", {kind: code, message, conflicts: drop.map(e => ({path: opPaths(e.op)[0], label: S.labelFor(opPaths(e.op)[0], oldBase)})), resolve() {}});
        p.persist();
        if (p === proj) recompute("remote", {by_name: S.nameOf(p.base.updated_by)});
        if (p.pending.length) p.send(); else p.settle();
        return;
      }
      batch.forEach(e => e.waiters.forEach(w => w.rej(err)));
      emit("error", {message: err.message});
      p.persist();
      if (p === proj) recompute("server");
      if (p.pending.length) p.send(); else p.settle();
    };

    function item(e, theirs, byName) {
      const path = opPaths(e.op)[0], mine = opValue(e.op), doc = S.doc || p.base;
      return {path, label: S.labelFor(path, doc), mine, theirs, mineText: S.formatValue(path, mine, doc),
              theirsText: S.formatValue(path, theirs, doc), by_name: byName || "Jemand", entry: e};
    }

    p.afterConflict = (items, kind) => {
      p.persist();
      if (p === proj) recompute("remote", {by_name: S.nameOf(p.base.updated_by)});
      if (p.pending.length) p.send();
      if (!items.length) { if (!p.pending.length) p.settle(); return; }
      const resolve = (choices) => {
        const again = [];
        for (const it of items) {
          const c = typeof choices === "string" ? choices : (choices && choices[it.path]) || "theirs";
          if (c === "mine") again.push(it.entry); else it.entry.waiters.forEach(w => w.res());
        }
        p.requeue(again);
        p.persist();
        if (p === proj) recompute("local");
        if (p.pending.length) p.send(); else p.settle();
      };
      const pub = items.map(({entry, ...rest}) => rest);
      if (p !== proj || !(listeners.get("conflict") || new Set()).size) return resolve("theirs");
      emit("conflict", {kind, conflicts: pub, resolve});
    };
    return p;
  }

  function targetMissing(doc, op) {
    const path = opPaths(op)[0];
    if (!path || !(path.startsWith("pieces.") || path.startsWith("variants.")) || op.op === "add_piece" || op.op === "restore_variant") return false;
    return getPath(doc, path.split(".").slice(0, 2).join(".")) === undefined;
  }

  // ---------- sync loop ----------
  function scheduleSync() {
    clearTimeout(syncTimer);
    if (!autoSync || S.stopped) return;
    const hidden = typeof document !== "undefined" && document.hidden;
    syncTimer = setTimeout(syncOnce, hidden ? Math.max(15000, syncMs) : proj ? syncMs : dashMs);
  }

  // one sync at a time; a syncNow() during a running sync runs once more right after it (and resolves after that)
  let syncP = null, syncAgain = false;
  function syncOnce() {
    if (syncP) { syncAgain = true; return syncP; }
    syncP = (async () => {
      try { do { syncAgain = false; await syncRun(); } while (syncAgain); }
      finally { syncP = null; scheduleSync(); }
    })();
    return syncP;
  }

  async function syncRun() {
    // hidden tabs pause, except while one of my jobs runs (slow poll, so attention() can fire when it finishes)
    if (typeof document !== "undefined" && document.hidden && !(S.queue.my_jobs || []).length) return;
    const p = proj;
    const q = new URLSearchParams();
    if (p) { q.set("project", p.pid); q.set("rev", String((p.base && p.base.rev) || 0)); }
    q.set("view", S.viewName || ""); q.set("device", deviceLabel());
    try {
      const r = await api("GET", "/api/sync?" + q);
      netOk();
      await handleSync(r, p);
    } catch (e) {
      if (e && e.status === 0) netFail();
    }
  }

  async function handleSync(r, p) {
    if (!r) return;
    const started = r.server && r.server.started;
    if (started && lastStarted && started !== lastStarted) {
      lastStarted = started;
      emit("restart", {});
      try {
        const [me, settings, catalog] = await Promise.all([api("GET", "/api/me"), api("GET", "/api/settings"), api("GET", "/api/workshop/catalog")]);
        S.me = me; S.settings = settings; S.catalog = catalog; S.team = me.team || settings.team || S.team;
        emit("me", S.me);
        if (p && p === proj) applyRemote(p, await api("GET", `/api/projects/workshop/${p.pid}`));
      } catch { /* next sync retries */ }
    }
    if (started) lastStarted = started;
    if (r.me !== undefined && S.me && !deepEqual(r.me, S.me.user)) { S.me = {...S.me, user: r.me}; emit("me", S.me); }
    if (r.news != null && r.news !== S.news) { S.news = r.news; emit("news", r.news); }
    if (r.list_rev != null) {
      if (lastListRev != null && r.list_rev !== lastListRev) emit("list", {list_rev: r.list_rev});
      lastListRev = r.list_rev;
    }
    if (p && p === proj && r.project) {
      if (r.project.deleted) emit("deleted", {pid: p.pid});
      else if (r.project.doc) applyRemote(p, r.project.doc);
    }
    const pres = p && p === proj ? r.presence || [] : [];
    if (!deepEqual(pres, S.presence)) { S.presence = pres; emit("presence", pres); }
    if (r.queue) {
      const prev = S.queue || {};
      S.queue = r.queue;
      const now = new Set((r.queue.my_jobs || []).map(j => j.id));
      for (const j of prev.my_jobs || []) {
        if (now.has(j.id)) continue;
        const fin = [...(r.queue.project_jobs || []), ...(r.queue.jobs || [])].find(x => x.id === j.id);
        emit("jobdone", {job: fin ? {...j, ...fin} : j});
      }
      emit("queue", S.queue);
    }
  }

  return S;
}

export const store = createStore();
