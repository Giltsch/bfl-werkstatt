// node --test tests/ws_store.test.mjs — store.js (§5.2/§10.5) and the node-testable parts of ui.js (§10.4).
import {test} from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {applyOps, rebase, diffPaths, createStore, getPath} from "../static/ws/store.js";
import {ApiError, fmt, ratingOf, positionsText, pieceLabel, uid, esc, statusLabel, SECURE, copyText, shareOrFallback, shareLinks, jobLine} from "../static/ws/ui.js";

const FIX = new URL("./ui/fixtures/", import.meta.url);
const fixture = name => JSON.parse(readFileSync(new URL(name, FIX), "utf8"));
const clone = v => JSON.parse(JSON.stringify(v));
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function until(cond, ms = 2000) {
  const t0 = Date.now();
  while (!cond()) { if (Date.now() - t0 > ms) throw new Error("timeout"); await sleep(5); }
}
const catalog = fixture("catalog.json");
const PID = "a1b2c3d4e5f6";

function memStorage() {
  const m = new Map();
  return {getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k),
    key: i => [...m.keys()][i] ?? null, get length() { return m.size; }, map: m};
}

// Fake backend: GET doc, PATCH (applyOps + rev), sync; `script` entries override the next matching call.
function fakeServer(doc = fixture("doc-full.json")) {
  const srv = {doc: clone(doc), calls: [], script: [], offline: false};
  srv.when = (method, re, reply) => srv.script.push({method, re, reply});
  srv.remote = (ops, who = "ben") => { srv.doc = applyOps(srv.doc, ops); srv.doc.rev++; srv.doc.updated_by = who; };
  srv.api = async (method, url, body) => {
    srv.calls.push({method, url, body: body === undefined ? undefined : clone(body)});
    await sleep(1);
    if (srv.offline) throw new ApiError(0, null);
    const i = srv.script.findIndex(x => x.method === method && x.re.test(url));
    if (i >= 0) return srv.script.splice(i, 1)[0].reply(body, srv, url);
    if (method === "GET" && url.startsWith("/api/projects/workshop/")) return clone(srv.doc);
    if (method === "PATCH") { srv.doc = applyOps(srv.doc, body.ops); srv.doc.rev++; srv.doc.updated_by = "peter"; return {rev: srv.doc.rev, doc: clone(srv.doc)}; }
    if (method === "GET" && url.startsWith("/api/sync")) {
      const rev = Number(new URLSearchParams(url.split("?")[1]).get("rev") || 0);
      return {server: {started: "s1"}, me: {id: "peter", name: "Peter", color: "#59a7ff"}, list_rev: 1,
        project: {rev: srv.doc.rev, doc: rev < srv.doc.rev ? clone(srv.doc) : null, deleted: false}, presence: [], queue: srv.queue || {}};
    }
    if (method === "POST" && /\/(generate|edit)$/.test(url)) return {job: {id: "job1"}, plan: {estimate_s: 60}};
    if (method === "DELETE" && url.startsWith("/api/jobs/")) return {ok: true, state: "cancelled"};
    throw new Error(`unexpected ${method} ${url}`);
  };
  return srv;
}

async function openStore(srv, opts = {}) {
  const S = createStore({api: srv.api, storage: opts.storage || memStorage(), autoSync: false, debounceMs: 5, backoff: [20, 40],
    confirm: opts.confirm || (async () => true), ...opts});
  S.me = {user: {id: "peter", name: "Peter"}, device: "8f3a1c2e4b5d"};
  S.team = [{id: "peter", name: "Peter"}, {id: "ben", name: "Ben"}];
  S.catalog = catalog;
  await S.open(PID);
  return S;
}

const patches = srv => srv.calls.filter(c => c.method === "PATCH");

// ---------- pure op semantics ----------

test("applyOps mirrors every op of §5.1 and never mutates its input", () => {
  const d0 = fixture("doc-full.json"), frozen = JSON.stringify(d0);
  let d = applyOps(d0, [
    {op: "set", path: "title", value: "Küche Müller"},
    {op: "set", path: "contact.phone", value: "0981 1"},
    {op: "set", path: "judge.auto_retry", value: false},
    {op: "set", path: "pieces.p1aaaa.w", value: 240},
    {op: "set", path: "pieces.p1aaaa.placement", value: {mode: "2d", pose: null, box: [1, 2, 3, 4]}},
    {op: "set", path: "variants.vb222222.fav", value: true},
    {op: "set", path: "appointment", value: {at: "2026-10-20T09:00", what: "Aufmaß"}},
  ]);
  assert.equal(JSON.stringify(d0), frozen);
  assert.equal(d.title, "Küche Müller");
  assert.equal(d.contact.phone, "0981 1");
  assert.equal(d.judge.auto_retry, false);
  assert.equal(getPath(d, "pieces.p1aaaa.w"), 240);
  assert.deepEqual(getPath(d, "pieces.p1aaaa.placement").box, [1, 2, 3, 4]);
  assert.equal(getPath(d, "variants.vb222222.fav"), true);
  d = applyOps(d, [{op: "set", path: "pieces.p2bbbb", value: {type: "tisch", w: 1}}]);
  assert.equal(d.pieces[1].id, "p2bbbb");
  assert.equal(d.pieces[1].type, "tisch");
  // add_piece: max 4, duplicates ignored
  const np = id => ({id, type: null, material: "eiche", handles: "push", placement: {mode: "none", pose: null, box: null}});
  d = applyOps(d, [{op: "add_piece", value: np("k3m9qa")}, {op: "add_piece", value: np("k3m9qa")}, {op: "add_piece", value: np("x1x1x1")}, {op: "add_piece", value: np("y2y2y2")}]);
  assert.deepEqual(d.pieces.map(p => p.id), ["p1aaaa", "p2bbbb", "k3m9qa", "x1x1x1"]);
  // remove_piece; the last piece is replaced by the client-generated replacement
  d = applyOps(d, [{op: "remove_piece", id: "k3m9qa"}, {op: "remove_piece", id: "x1x1x1"}, {op: "remove_piece", id: "p2bbbb"}]);
  assert.deepEqual(d.pieces.map(p => p.id), ["p1aaaa"]);
  d = applyOps(d, [{op: "remove_piece", id: "p1aaaa", replacement: np("q7c2xw")}]);
  assert.deepEqual(d.pieces.map(p => p.id), ["q7c2xw"]);
  // delete / restore variant
  d = applyOps(d, [{op: "delete_variant", id: "vc333333"}]);
  assert.ok(!d.variants.some(v => v.id === "vc333333"));
  assert.equal(d.trash.at(-1).id, "vc333333");
  assert.ok(d.deleted_variants.includes("vc333333"));
  d = applyOps(d, [{op: "restore_variant", id: "vc333333"}]);
  assert.ok(d.variants.some(v => v.id === "vc333333"));
  assert.ok(!d.deleted_variants.includes("vc333333"));
  assert.equal(d.trash.length, 0);
  // archive / unarchive; ops on missing targets are ignored
  d = applyOps(d, [{op: "archive"}]);
  assert.match(d.archived_at, /^\d{4}-\d\d-\d\dT/);
  d = applyOps(d, [{op: "unarchive"}, {op: "set", path: "pieces.nope99.w", value: 1}]);
  assert.equal(d.archived_at, null);
});

test("diffPaths uses changed_paths granularity and ignores derived keys", () => {
  const a = fixture("doc-full.json");
  const b = applyOps(a, [{op: "set", path: "pieces.p1aaaa.w", value: 200}, {op: "set", path: "contact.city", value: "X"},
    {op: "delete_variant", id: "vc333333"}, {op: "set", path: "title", value: "T"}]);
  b.rev = 99; b.thumb = "other.png"; b.activity = []; b.pieces[0].label = "Neu";
  assert.deepEqual(diffPaths(a, b).sort(), ["contact.city", "deleted_variants", "pieces.p1aaaa.w", "title", "trash", "variants.vc333333"].sort());
  assert.ok(diffPaths(a, b, {all: true}).includes("activity"));
  assert.ok(diffPaths(a, b, {all: true}).includes("pieces.p1aaaa.label"));
});

test("rebase keeps local edits on top of remote changes to other paths", () => {
  const base = fixture("doc-full.json");
  const pending = [{op: {op: "set", path: "title", value: "Mein Titel"}}];
  const remote = applyOps(base, [{op: "set", path: "customer", value: "Ben's Kunde"}, {op: "set", path: "title", value: "Remote"}]);
  const d = rebase(remote, pending);
  assert.equal(d.title, "Mein Titel");
  assert.equal(d.customer, "Ben's Kunde");
});

// ---------- store: writes, acks, conflicts ----------

test("patch is optimistic, coalesces sets on one path and keeps one PATCH in flight", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  const events = [];
  S.on("doc", e => events.push(e));
  S.patch([{op: "set", path: "title", value: "A"}]);
  S.patch([{op: "set", path: "title", value: "AB"}]);
  assert.equal(S.doc.title, "AB");
  assert.equal(events.at(-1).source, "local");
  await S.flush();
  assert.equal(patches(srv).length, 1);
  assert.deepEqual(patches(srv)[0].body.ops, [{op: "set", path: "title", value: "AB"}]);
  assert.equal(patches(srv)[0].body.base_rev, 12);
  // one in flight: the second patch waits for the first ack and uses its rev
  const p1 = S.patch([{op: "set", path: "customer", value: "K1"}], {now: true});
  await sleep(0);
  const p2 = S.patch([{op: "set", path: "setting", value: "S"}], {now: true});
  await Promise.all([p1, p2]);
  const ps = patches(srv);
  assert.equal(ps.length, 3);
  assert.equal(ps[2].body.base_rev, ps[1].body.base_rev + 1);
  assert.equal(S.doc.rev, srv.doc.rev);
  assert.equal(S.saveState, "saved");
});

test("409 conflict: readable dialog data, other ops re-sent, 'mine' re-sends with the new base_rev", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  srv.when("PATCH", /workshop/, (body, s) => {
    s.remote([{op: "set", path: "pieces.p1aaaa.w", value: 220}]);
    throw new ApiError(409, {code: "conflict", message: "Gleichzeitig geändert.", rev: s.doc.rev, doc: clone(s.doc),
      conflicts: [{path: "pieces.p1aaaa.w", mine: 240, theirs: 220, by: "ben", by_name: "Ben", at: "2026-10-17T18:01:55"}]});
  });
  let evt = null;
  S.on("conflict", e => { evt = e; });
  const done = S.patch([{op: "set", path: "pieces.p1aaaa.w", value: 240}, {op: "set", path: "title", value: "Neu"}], {now: true});
  await until(() => evt);
  assert.equal(evt.kind, "conflict");
  const c = evt.conflicts[0];
  assert.equal(c.path, "pieces.p1aaaa.w");
  assert.equal(c.label, "Breite (Sideboard)");
  assert.equal(c.mineText, "240 cm");
  assert.equal(c.theirsText, "220 cm");
  assert.equal(c.by_name, "Ben");
  await until(() => patches(srv).length === 2);                  // the title op went out on its own
  assert.deepEqual(patches(srv)[1].body.ops, [{op: "set", path: "title", value: "Neu"}]);
  assert.equal(S.doc.title, "Neu");
  assert.equal(getPath(S.doc, "pieces.p1aaaa.w"), 220);
  evt.resolve({"pieces.p1aaaa.w": "mine"});
  await done;
  const last = patches(srv).at(-1);
  assert.deepEqual(last.body.ops, [{op: "set", path: "pieces.p1aaaa.w", value: 240}]);
  assert.ok(last.body.base_rev >= 13);
  assert.equal(getPath(srv.doc, "pieces.p1aaaa.w"), 240);
});

test("409 conflict resolved 'theirs' drops my op", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  srv.when("PATCH", /workshop/, (body, s) => {
    s.remote([{op: "set", path: "customer", value: "Ben"}]);
    throw new ApiError(409, {code: "conflict", rev: s.doc.rev, doc: clone(s.doc), conflicts: [{path: "customer", theirs: "Ben", by: "ben", by_name: "Ben"}]});
  });
  S.on("conflict", e => e.resolve("theirs"));
  await S.patch([{op: "set", path: "customer", value: "Ich"}], {now: true});
  assert.equal(patches(srv).length, 1);
  assert.equal(S.doc.customer, "Ben");
});

test("409 gone/locked drop the op and emit a message; 400 rejects and reports", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  const evts = [], errs = [];
  S.on("conflict", e => evts.push(e));
  S.on("error", e => errs.push(e));
  srv.when("PATCH", /workshop/, (b, s) => {
    s.remote([{op: "remove_piece", id: "p2bbbb"}]);
    throw new ApiError(409, {code: "gone", rev: s.doc.rev, doc: clone(s.doc), conflicts: [{path: "pieces.p2bbbb.w", by: "ben", by_name: "Ben"}]});
  });
  await S.patch([{op: "set", path: "pieces.p2bbbb.w", value: 99}], {now: true});
  assert.equal(evts[0].kind, "gone");
  assert.equal(evts[0].message, "Dieses Möbel wurde inzwischen von Ben gelöscht.");
  srv.when("PATCH", /workshop/, (b, s) => {
    throw new ApiError(409, {code: "locked", message: "Dieser Vorschlag gehört zu einer gesendeten Version. Ziehen Sie zuerst die Version zurück.", doc: clone(s.doc)});
  });
  await S.patch([{op: "delete_variant", id: "va111111"}], {now: true});
  assert.equal(evts[1].kind, "locked");
  assert.ok(S.doc.variants.some(v => v.id === "va111111"));
  srv.when("PATCH", /workshop/, () => { throw new ApiError(400, "Ungültige Änderung: title."); });
  await assert.rejects(S.patch([{op: "set", path: "title", value: 5}], {now: true}));
  assert.match(errs[0].message, /^Speichern fehlgeschlagen: Ungültige Änderung/);
});

test("409 stale: re-fetch; unchanged paths re-sent, changed ones become a conflict", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  srv.when("PATCH", /workshop/, (b, s) => {
    s.remote([{op: "set", path: "customer", value: "Remote"}]);
    s.doc.frev = {customer: [s.doc.rev, "ben", "t"]};
    throw new ApiError(409, {code: "stale", rev: s.doc.rev});
  });
  let evt = null;
  S.on("conflict", e => { evt = e; });
  const p = S.patch([{op: "set", path: "customer", value: "Ich"}, {op: "set", path: "title", value: "T2"}], {now: true});
  await until(() => evt);
  assert.equal(evt.conflicts.length, 1);
  assert.equal(evt.conflicts[0].path, "customer");
  assert.equal(evt.conflicts[0].theirsText, "Remote");
  assert.equal(evt.conflicts[0].by_name, "Ben");
  await until(() => srv.doc.title === "T2");
  evt.resolve("theirs");
  await p;
  assert.equal(S.doc.customer, "Remote");
});

test("formatValue is always readable (never an object or a file path) and labelFor is German", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  const d = S.doc;
  const plain = v => typeof v === "string" && !/uploads\/|\{|\[object/.test(v);
  for (const [path, value] of [["pieces.p1aaaa.placement", {mode: "3d", pose: {x: 1, z: 2, yaw: 0}, box: [1, 2, 3, 4]}],
    ["pieces.p1aaaa.placement", {mode: "none"}], ["geometry", d.geometry], ["geometry", null], ["calibration", {scale: 1.1}],
    ["removes", [[1, 2, 3, 4], [5, 6, 7, 8]]], ["pieces.p1aaaa", d.pieces[0]], ["pieces.p1aaaa", null], ["appointment", d.appointment],
    ["variants.va111111", d.variants[0]], ["judge", {enabled: true}], ["room_size", [1, 2]]]) {
    const t = S.formatValue(path, value, d);
    assert.ok(plain(t), `${path} → ${JSON.stringify(t)}`);
  }
  assert.equal(S.formatValue("removes", [[1, 2, 3, 4], [5, 6, 7, 8]]), "2 Entfernen-Rahmen");
  assert.equal(S.formatValue("geometry", {}), "neu vermessen");
  assert.equal(S.formatValue("tidy", true), "an");
  assert.equal(S.formatValue("judge.enabled", false), "aus");
  assert.equal(S.formatValue("status", "kunde"), "Beim Kunden");
  assert.equal(S.formatValue("pieces.p1aaaa.material", "nuss"), "Nussbaum");
  assert.equal(S.formatValue("pieces.p1aaaa", d.pieces[0]), "Sideboard");
  assert.equal(S.formatValue("appointment", {at: "2026-10-20T09:00", what: "Aufmaß"}), "Di 20.10., 09:00 · Aufmaß");
  assert.deepEqual(S.formatValue("room", "uploads/room.png"), {thumb: "/outputs/uploads/room.png"});
  assert.deepEqual(S.formatValue("pieces.p1aaaa.sketch", "uploads/s.png"), {thumb: "/outputs/uploads/s.png"});
  assert.equal(S.labelFor("pieces.p1aaaa.w"), "Breite (Sideboard)");
  assert.equal(S.labelFor("pieces.p2bbbb.placement"), "Position (Regalwand)");
  assert.equal(S.labelFor("contact.phone"), "Telefon");
  assert.equal(S.labelFor("judge.auto_retry"), "Neu rechnen");
  assert.equal(S.labelFor("variants.va111111.fav"), "Auswahl (Vorschlag 1)");
  assert.equal(S.labelFor("ext_ref"), "Angebots-Nr.");
});

test("generate: 409 changed → resolve('force') re-sends with the new base_rev; 'view' applies the doc", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  srv.when("POST", /generate$/, (b, s) => {
    s.remote([{op: "set", path: "pieces.p1aaaa.w", value: 190}]);
    throw new ApiError(409, {code: "changed", message: "Der Auftrag wurde gerade von Ben geändert. Bitte kurz prüfen und dann erneut starten.",
      rev: s.doc.rev, doc: clone(s.doc), changes: [{path: "pieces.p1aaaa.w", by_name: "Ben"}]});
  });
  S.on("conflict", e => { assert.equal(e.kind, "changed"); assert.equal(e.changes[0].by_name, "Ben"); e.resolve("force"); });
  const r = await S.generate({kind: "room"});
  assert.equal(r.job.id, "job1");
  const posts = srv.calls.filter(c => c.method === "POST");
  assert.equal(posts.length, 2);
  assert.equal(posts[0].body.base_rev, 12);
  assert.equal(posts[1].body.base_rev, 13);
  assert.equal(posts[1].body.client_key, posts[0].body.client_key);
  assert.equal(posts[0].body.active_piece, "p1aaaa");
  assert.equal(getPath(S.doc, "pieces.p1aaaa.w"), 190);
});

test("generate: 409 running → 'force' re-sends with force:true, 'goto' returns the running job", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  const running = () => { throw new ApiError(409, {code: "running", message: "Für diesen Auftrag läuft schon eine Berechnung (Anna, Platz 2).", job: {id: "other"}}); };
  srv.when("POST", /generate$/, running);
  let choice = "force";
  S.on("conflict", e => { assert.equal(e.kind, "running"); assert.equal(e.job.id, "other"); e.resolve(choice); });
  await S.generate({kind: "room"});
  const posts = srv.calls.filter(c => c.method === "POST");
  assert.equal(posts.at(-1).body.force, true);
  srv.when("POST", /generate$/, running);
  choice = "goto";
  const r = await S.generate({kind: "room"});
  assert.deepEqual(r, {job: {id: "other"}, running: true});
});

test("cancel: not_owner asks, then retries with force", async () => {
  const srv = fakeServer();
  let asked = null;
  const S = await openStore(srv, {confirm: async o => { asked = o; return true; }});
  srv.when("DELETE", /\/api\/jobs\/j9\?force=0/, () => { throw new ApiError(403, {code: "not_owner", owner_name: "Anna", message: "Dieser Auftrag gehört Anna. Trotzdem abbrechen?"}); });
  const r = await S.cancel("j9");
  assert.equal(asked.text, "Dieser Auftrag gehört Anna. Trotzdem abbrechen?");
  assert.ok(srv.calls.some(c => c.url === "/api/jobs/j9?force=1"));
  assert.equal(r.ok, true);
});

// ---------- remote changes, holds, bind ----------

test("remote doc via sync: emits paths/by_name; placement toast; hold/release defers a remote placement", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  const docs = [], toasts = [];
  S.on("doc", e => docs.push(e));
  S.on("toast", e => toasts.push(e.text));
  srv.remote([{op: "set", path: "title", value: "Von Ben"}]);
  await S.syncNow();
  assert.equal(S.doc.title, "Von Ben");
  assert.equal(docs.at(-1).source, "remote");
  assert.ok(docs.at(-1).paths.includes("title"));
  assert.equal(docs.at(-1).by_name, "Ben");
  srv.remote([{op: "set", path: "pieces.p2bbbb.placement", value: {mode: "2d", pose: null, box: [10, 10, 500, 300]}}]);
  await S.syncNow();
  assert.deepEqual(toasts, ["Ben hat Regalwand verschoben"]);
  // hold: a colleague's move never yanks the piece mid-gesture
  const path = "pieces.p1aaaa.placement";
  const before = clone(getPath(S.doc, path));
  S.hold(path);
  srv.remote([{op: "set", path, value: {mode: "2d", pose: null, box: [100, 100, 600, 600]}}]);
  await S.syncNow();
  assert.deepEqual(getPath(S.doc, path), before);
  assert.equal(toasts.length, 1);
  S.release(path);
  await sleep(0);
  assert.deepEqual(getPath(S.doc, path).box, [100, 100, 600, 600]);
  assert.deepEqual(toasts.at(-1), "Ben hat Sideboard verschoben");
  // local op at release wins over the held remote value
  S.hold(path);
  srv.remote([{op: "set", path, value: {mode: "2d", pose: null, box: [1, 1, 900, 900]}}]);
  await S.syncNow();
  S.patch([{op: "set", path, value: {mode: "2d", pose: null, box: [5, 5, 50, 50]}}], {now: true});
  S.release(path);
  await sleep(0);
  assert.deepEqual(getPath(S.doc, path).box, [5, 5, 50, 50]);
  await S.flush();
  assert.deepEqual(getPath(S.doc, path).box, [5, 5, 50, 50]);
});

class FakeInput extends EventTarget {
  constructor(type = "text") { super(); this.type = type; this.tagName = "INPUT"; this.value = ""; this.checked = false; }
  focus() { globalThis.document.activeElement = this; }
  blur() { globalThis.document.activeElement = null; this.dispatchEvent(new Event("blur")); }
  typeText(t) { this.value = t; this.dispatchEvent(new Event("input")); }
}

test("bind: debounced writes, no remote overwrite while focused, re-render on blur without pending op", async () => {
  globalThis.document = {activeElement: null, hidden: false};
  try {
    const srv = fakeServer();
    const S = await openStore(srv);
    const inp = new FakeInput("number");
    const unbind = S.bind(inp, "pieces.p1aaaa.w", {debounce: 10});
    assert.equal(inp.value, "180");
    inp.focus();
    inp.typeText("200");
    await until(() => srv.doc.pieces[0].w === 200);
    // remote change while focused: the input keeps the user's text …
    srv.remote([{op: "set", path: "pieces.p1aaaa.w", value: 210}]);
    await S.syncNow();
    assert.equal(S.doc.pieces[0].w, 210);
    assert.equal(inp.value, "200");
    // … and shows the current value on blur (no pending op for the path)
    inp.blur();
    assert.equal(inp.value, "210");
    // not focused: remote values render immediately
    srv.remote([{op: "set", path: "pieces.p1aaaa.w", value: 215}]);
    await S.syncNow();
    assert.equal(inp.value, "215");
    // blur flushes a pending debounce instead of re-rendering
    inp.focus();
    inp.typeText("230");
    inp.blur();
    assert.equal(inp.value, "230");
    await S.flush();
    assert.equal(srv.doc.pieces[0].w, 230);
    unbind();
  } finally { delete globalThis.document; }
});

// ---------- offline queue, identity switch ----------

test("network error: pending ops persisted, retried with backoff, restored on reload with their base_rev", async () => {
  const srv = fakeServer(), storage = memStorage();
  const S = await openStore(srv, {storage});
  const states = [];
  S.on("offline", () => states.push("offline"));
  S.on("online", () => states.push("online"));
  srv.offline = true;
  S.patch([{op: "set", path: "title", value: "Offline-Titel"}], {now: true});
  await until(() => states.includes("offline"));
  const saved = JSON.parse(storage.getItem(`ws.pending.${PID}`));
  assert.equal(saved.user, "peter");
  assert.deepEqual(saved.groups, [{base_rev: 12, ops: [{op: "set", path: "title", value: "Offline-Titel"}]}]);
  // a reload while still offline: a new store restores and re-sends with the saved base_rev
  S.stop();
  srv.offline = false;
  const S2 = await openStore(srv, {storage});
  await S2.flush();
  const last = patches(srv).at(-1);
  assert.equal(last.body.base_rev, 12);
  assert.deepEqual(last.body.ops, [{op: "set", path: "title", value: "Offline-Titel"}]);
  assert.equal(storage.getItem(`ws.pending.${PID}`), null);
  assert.equal(srv.doc.title, "Offline-Titel");
  // the original store recovers by itself (backoff retry) and reports online again
  await until(() => states.includes("online"), 3000);
});

test("persisted ops of another person are never re-sent", async () => {
  const srv = fakeServer(), storage = memStorage();
  storage.setItem(`ws.pending.${PID}`, JSON.stringify({user: "ben", groups: [{base_rev: 12, ops: [{op: "set", path: "title", value: "Bens"}]}]}));
  const S = await openStore(srv, {storage});
  await S.flush();
  assert.equal(patches(srv).length, 0);
  assert.equal(storage.getItem(`ws.pending.${PID}`), null);
  assert.notEqual(S.doc.title, "Bens");
});

test("switchUser flushes pending ops first; offline it asks and clears the persisted queue", async () => {
  const srv = fakeServer(), storage = memStorage();
  let asked = 0;
  const S = await openStore(srv, {storage, confirm: async () => { asked++; return true; }});
  S.patch([{op: "set", path: "title", value: "Vorher"}]);
  assert.equal(await S.switchUser(), true);
  assert.equal(srv.doc.title, "Vorher");
  assert.equal(asked, 0);
  srv.offline = true;
  S.patch([{op: "set", path: "title", value: "Verwerfen"}], {now: true});
  storage.setItem("ws.pending.other0000001", "{}");
  assert.equal(await S.switchUser(), true);
  assert.equal(asked, 1);
  assert.equal([...storage.map.keys()].filter(k => k.startsWith("ws.pending.")).length, 0);
  srv.offline = false;
  await sleep(80);
  assert.equal(srv.doc.title, "Vorher");                      // the discarded op never went out
});

test("switchUser offline + 'Abbrechen' keeps the pending ops", async () => {
  const srv = fakeServer(), storage = memStorage();
  const S = await openStore(srv, {storage, confirm: async () => false});
  srv.offline = true;
  S.patch([{op: "set", path: "title", value: "Behalten"}], {now: true});
  assert.equal(await S.switchUser(), false);
  assert.ok(storage.getItem(`ws.pending.${PID}`));
  srv.offline = false;
  await S.flush();
  assert.equal(srv.doc.title, "Behalten");
});

test("queue helpers and jobdone event", async () => {
  const srv = fakeServer();
  const S = await openStore(srv);
  const done = [];
  S.on("jobdone", e => done.push(e.job));
  srv.queue = {busy: true, pup: {state: "down", backend: "template"}, my_jobs: [{id: "j1", project: PID, count: 2}],
    project_jobs: [{id: "j1", project: PID, state: "running"}], jobs: []};
  await S.syncNow();
  assert.deepEqual(S.pup(), {state: "down", backend: "template"});
  assert.equal(S.myJobs().length, 1);
  assert.equal(S.projectJobs().length, 1);
  srv.queue = {busy: false, my_jobs: [], project_jobs: [{id: "j1", project: PID, state: "done", count: 2}], jobs: []};
  await S.syncNow();
  assert.equal(done.length, 1);
  assert.equal(done[0].state, "done");
});

test("view state is per device in localStorage, never in the doc", async () => {
  const srv = fakeServer(), storage = memStorage();
  const S = await openStore(srv, {storage});
  S.view().step = "platzieren"; S.view().active_piece = "p2bbbb";
  S.saveView();
  assert.deepEqual(JSON.parse(storage.getItem(`ws.view.${PID}`)), {step: "platzieren", active_piece: "p2bbbb"});
  assert.equal(patches(srv).length, 0);
});

// ---------- ui.js (node-safe parts) ----------

test("fmt speaks German", () => {
  assert.equal(fmt.dur(5), "gleich");
  assert.equal(fmt.dur(40), "ca. 40 Sek.");
  assert.equal(fmt.dur(125), "ca. 2 Min.");
  assert.equal(fmt.dur(3600), "ca. 60 Min.");
  assert.deepEqual([8, 25, 75, 200, 615].map(fmt.span), ["ca. 10 Sek.", "ca. 30 Sek.", "ca. 1½ Min.", "ca. 3½ Min.", "ca. 10 Min."]);
  // the server's eta_text wins: phone and desktop show the same line, also during the caption phase
  const j = {state: "running", stage_text: "Beschreibung wird vorbereitet …", eta_done_s: 30, eta_text: "Bilder in ca. 3½ Min., Bewertung ca. 30 Sek. danach"};
  assert.equal(jobLine(j), "Beschreibung wird vorbereitet … · Bilder in ca. 3½ Min., Bewertung ca. 30 Sek. danach");
  assert.equal(jobLine({...j, state: "done", judge_state: "running", stage_text: "Qualitätsprüfung …", eta_text: "Bewertung in ca. 20 Sek."}),
    "Qualitätsprüfung … · Bewertung in ca. 20 Sek.");
  assert.equal(fmt.cm(240), "240 cm");
  assert.equal(fmt.m(240), "2,40 m");
  assert.equal(fmt.num(1234.5, 1), "1.234,5");
  assert.equal(fmt.date("2026-10-18T10:12:00"), "18.10.2026");
  assert.equal(fmt.dateTime("2026-10-20T09:00"), "Di 20.10., 09:00");
  assert.equal(fmt.ago(new Date(Date.now() - 2 * 3600e3).toISOString()), "vor 2 Std.");
  assert.equal(statusLabel("fertig"), "Abgeschlossen");
});

test("ratingOf, pieceLabel, positionsText, esc, uid", () => {
  assert.deepEqual(ratingOf({status: "ok", pass: true, overall: 8.5}).text, "✓ Gut (8,5)");
  assert.equal(ratingOf({status: "ok", pass: false, overall: 5.7}).text, "⚠ Bitte prüfen (5,7)");
  assert.equal(ratingOf({status: "ok", pass: false, overall: 3.2}).tone, "bad");
  assert.equal(ratingOf({status: "pending"}).text, "Prüfung läuft …");
  assert.equal(ratingOf(null).text, "ohne Prüfung");
  assert.equal(ratingOf({status: "unavailable"}).text, "Prüfung nicht möglich");
  assert.equal(pieceLabel({type: null, text: ""}, 1, catalog), "Möbel 2");
  assert.equal(pieceLabel({type: "tisch"}, 0, catalog), "Esstisch");
  const doc = fixture("doc-full.json");
  doc.approved = {version: 1, variant_id: "va111111", at: "2026-10-18T10:00:00"};
  assert.equal(positionsText(doc, "va111111", catalog), [
    "Sideboard, Eiche natur geölt, grifflos (Push), B 180 × H 75 × T 45 cm",
    "Regalwand, Nussbaum, Griffleiste, B 120 × H 220 × T 35 cm",
    "(Visualisierung Vorgang 2026-002, Version 1, Entwurf 1 – freigegeben am 18.10.2026)"].join("\n"));
  assert.equal(esc(`<img src=x onerror="alert(1)">&'`), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;&amp;&#39;");
  const id = uid();
  assert.match(id, /^[a-z][a-z0-9]{9}$/);
  assert.match(uid("c-"), /^c-[a-z][a-z0-9]{9}$/);
});

test("copyText / shareOrFallback take the fallback path when the context is not secure", async () => {
  assert.equal(SECURE, false);
  const calls = [];
  const savedNav = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", {configurable: true, value: {
    clipboard: {writeText: async () => calls.push("clipboard")}, share: async () => calls.push("share"), canShare: () => true}});
  const ta = {value: "", style: {}, setAttribute() {}, select() { calls.push("select"); }, setSelectionRange() {}, remove() {}};
  globalThis.document = {createElement: () => ta, body: {append() {}}, execCommand: c => { calls.push("exec:" + c); return true; }};
  try {
    assert.equal(await copyText("Positionstext"), "copied");
    assert.equal(ta.value, "Positionstext");
    assert.equal(await shareOrFallback({title: "T", text: "x", url: "http://192.168.1.2/k/abc"}), "fallback");
    assert.equal(await shareOrFallback({files: [{}]}), "fallback");
    assert.deepEqual(calls, ["select", "exec:copy"]);
  } finally {
    delete globalThis.document;
    if (savedNav) Object.defineProperty(globalThis, "navigator", savedNav);
  }
  const l = shareLinks({subject: "Entwürfe: Küche", text: "Ihre Entwürfe – das PDF sende ich Ihnen gleich mit."});
  assert.match(l.whatsapp, /^https:\/\/wa\.me\/\?text=Ihre%20Entw%C3%BCrfe/);
  assert.match(l.mail, /^mailto:\?subject=Entw%C3%BCrfe%3A%20K%C3%BCche&body=/);
});

test("a colleague's change during the debounce or in a focused field is a conflict, never a silent overwrite (§5.1)", async () => {
  // case 2: sync lands inside the debounce window → the PATCH carries the rev the edit was made against
  const srv = fakeServer();
  const S = await openStore(srv, {debounceMs: 60});
  const rev0 = S.doc.rev;
  S.set("title", "Meins");
  srv.remote([{op: "set", path: "title", value: "Von Ben"}]);
  await S.syncNow();
  await S.flush();
  const p = patches(srv).at(-1);
  assert.deepEqual(p.body.ops, [{op: "set", path: "title", value: "Meins"}]);
  assert.equal(p.body.base_rev, rev0);
  // a different field changed meanwhile: no stamp, current rev
  S.set("customer", "Frau Kunde");
  srv.remote([{op: "set", path: "ref", value: "R-9"}]);
  await S.syncNow();
  await S.flush();
  assert.equal(patches(srv).at(-1).body.base_rev, srv.doc.rev - 1);
  S.stop();

  // case 1: the focused input still shows the old value when the colleague's doc arrives
  const srv2 = fakeServer();
  const S2 = await openStore(srv2, {debounceMs: 5});
  const ls = {};
  const input = {type: "number", tagName: "INPUT", value: "", addEventListener: (t, f) => { ls[t] = f; }, removeEventListener() {}};
  const prevDoc = globalThis.document;
  globalThis.document = {activeElement: null};
  try {
    const off = S2.bind(input, "pieces.p1aaaa.w");
    const shown = S2.doc.rev;
    globalThis.document.activeElement = input;
    srv2.remote([{op: "set", path: "pieces.p1aaaa.w", value: 220}]);
    await S2.syncNow();
    assert.notEqual(input.value, "220");   // never re-rendered under the user's fingers
    input.value = "250"; ls.input();
    await sleep(20); await S2.flush();
    const q = patches(srv2).at(-1);
    assert.deepEqual(q.body.ops, [{op: "set", path: "pieces.p1aaaa.w", value: 250}]);
    assert.equal(q.body.base_rev, shown);
    off();
  } finally { globalThis.document = prevDoc; S2.stop(); }
});
