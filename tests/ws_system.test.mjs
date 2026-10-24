// node --test tests/ws_system.test.mjs — pure logic of the voice input, the „Verstanden:“ card, the Assistent panel and
// the settings sections Firmenprofil / Kanäle / API & Webhooks (frontend of the photo + voice system).
import test from "node:test";
import assert from "node:assert/strict";
import { pickMime, micSupport, micHint, micError, extFor, clock, isIOS, MIME_CANDIDATES } from "../static/ws/voice.js";
import { pieceLine, withAnswer, withDefaults, undoOps, joinText, applicable, focusOf, waitText } from "../static/ws/verstanden.js";
import { fileUrl, mergeEvents, lastAgent, quickReplies, resultLinks, scoreOf, splitText, pollDelay, startProblem, hhmm, STATE_DE } from "../static/ws/assistent.js";
import { typePatch, aliasPatch, secretValue, parseList, parseChats, chatsText, channelLine, curlExample, ENABLE_WORD } from "../static/ws/settings-connect.js";
import { parseRoute, SECTIONS as APP_SECTIONS } from "../static/ws/app.js";
import { SECTIONS } from "../static/ws/settings.js";
import { applyOps } from "../static/ws/store.js";

const CAT = { pieces: { schrank: { de: "Einbauschrank" }, sideboard: { de: "Sideboard" }, wandregal: { de: "Wandregal" } },
  materials: { weiss: { de: "Lack weiß matt" }, eiche: { de: "Eiche natur geölt" }, custom: { de: "Eigene Angabe…" } },
  handles: { push: { de: "grifflos (Push)" }, leiste: { de: "Griffleiste" } } };

// ---------------- routes & sections

test("routes: Assistent per order and the new-order entry; settings sections exist in both lists", () => {
  assert.deepEqual(parseRoute("#/assistent"), { name: "assistent-neu", query: {} });
  assert.deepEqual(parseRoute("#/p/fc44e668d04b/assistent"), { name: "assistent", pid: "fc44e668d04b", query: {} });
  assert.equal(parseRoute("#/assistent/x"), null);
  assert.equal(parseRoute("#/p/fc44e668d04b/assistent/x"), null);
  for (const id of ["profil", "kanaele", "api"]) {
    assert.ok(APP_SECTIONS.includes(id), id);
    assert.equal(parseRoute(`#/einstellungen/${id}`).section, id);
  }
  assert.deepEqual(SECTIONS.map(s => s[0]), APP_SECTIONS);
});

// ---------------- voice

test("voice: recorder container, secure-context fallback and German messages", () => {
  assert.equal(pickMime(m => m === "audio/mp4"), "audio/mp4");                         // Safari
  assert.equal(pickMime(m => m.startsWith("audio/webm")), MIME_CANDIDATES[0]);         // Chrome
  assert.equal(pickMime(() => { throw new Error("old Safari"); }), "");
  assert.equal(pickMime(undefined), "");
  assert.deepEqual(micSupport({ secure: false, mediaDevices: true, recorder: true }), { mode: "file", reason: "insecure" });
  assert.deepEqual(micSupport({ secure: true, mediaDevices: false, recorder: true }), { mode: "file", reason: "unsupported" });
  assert.deepEqual(micSupport({ secure: true, mediaDevices: true, recorder: true }), { mode: "record", reason: null });
  assert.match(micHint("insecure"), /https/);
  assert.match(micHint("insecure", { ios: true }), /Sprachmemos/);
  assert.match(micError({ name: "NotAllowedError" }), /erlauben/);
  assert.match(micError({ name: "NotFoundError" }), /Kein Mikrofon/);
  assert.match(micError({ name: "NotReadableError" }), /andere/);
  assert.match(micError(new Error("x")), /tippen/);
  assert.deepEqual(["audio/webm;codecs=opus", "audio/ogg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/mpeg", ""].map(extFor),
    ["webm", "ogg", "m4a", "m4a", "wav", "mp3", "webm"]);
  assert.deepEqual([0, 7.9, 65, -3, "x"].map(clock), ["0:00", "0:07", "1:05", "0:00", "0:00"]);
  assert.equal(isIOS("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"), true);
  assert.equal(isIOS("Mozilla/5.0 (Linux; Android 14)"), false);
});

// ---------------- Verstanden card

const DOC = { id: "abc123", rev: 5, pieces: [
  { id: "p1", type: "schrank", w: 200, h: 240, d: 60, material: "eiche", handles: "push", placement: { mode: "none", pose: null, box: null } }] };
const RES = {
  status: "ok", summary_de: "Sideboard 180 × 75 × 45 cm; Wandregal; aus dem Foto entfernen: der alte Schrank",
  pieces: [
    { ref: null, id: "n1", type: "sideboard", w: 180, h: 75, d: 45, material: "eiche", handles: "push", text: "", where: "unter das Fenster", full_height: false },
    { ref: null, id: "n2", type: "wandregal", w: null, h: 40, d: 25, elev: 150, material: "weiss", handles: null, text: "", where: "", full_height: false },
    { ref: "p1", id: "p1", type: "schrank", w: 200, h: 255, d: 80, material: "eiche", handles: "push", text: "", where: "", full_height: true, changes: ["Tiefe 80 cm"] }],
  removes: [{ what: "der alte Schrank" }], drops: [],
  questions: [{ piece: 1, field: "w", question_de: "Wie breit soll das Wandregal sein?", default: 120 }],
  ops: [
    { op: "add_piece", value: { id: "n1", type: "sideboard", w: 180, h: 75, d: 45, material: "eiche", handles: "push", where: "unter das Fenster", placement: { mode: "none", pose: null, box: null } } },
    { op: "add_piece", value: { id: "n2", type: "wandregal", w: null, h: 40, d: 25, elev: 150, material: "weiss", handles: null, placement: { mode: "none", pose: null, box: null } } },
    { op: "set", path: "pieces.p1.d", value: 80 }],
  warnings: [], confidence: 0.76 };

test("Verstanden: one line per piece with dims, details, location and corrections", () => {
  const a = pieceLine(RES.pieces[0], CAT);
  assert.equal(a.title, "Sideboard");
  assert.equal(a.dims, "180 × 75 × 45 cm");
  assert.deepEqual(a.details, ["Eiche natur geölt", "grifflos (Push)"]);
  assert.equal(a.where, "unter das Fenster");
  assert.equal(pieceLine(RES.pieces[1], CAT).dims, "– × 40 × 25 cm");
  assert.ok(pieceLine(RES.pieces[1], CAT).details.includes("hängt 150 cm hoch"));
  const c = pieceLine(RES.pieces[2], CAT);
  assert.equal(c.dims, "200 × raumhoch (255) × 80 cm");
  assert.equal(c.correction, true);
  assert.deepEqual(c.changes, ["Tiefe 80 cm"]);
  assert.equal(pieceLine({ type: null, text: "Kratzbaum aus Eiche", material: "custom", material_custom: "Lärche" }, CAT).title, "Kratzbaum aus Eiche");
  assert.deepEqual(pieceLine({ type: null, material: "custom", material_custom: "Lärche" }, CAT).details, ["Lärche"]);
});

test("Verstanden: an answered size question lands in the add_piece op, a set op or a new set op", () => {
  const r = withAnswer(RES, RES.questions[0], 140);
  assert.equal(r.questions.length, 0);
  assert.equal(r.pieces[1].w, 140);
  assert.equal(r.ops[1].value.w, 140);
  assert.equal(RES.ops[1].value.w, null, "the original result is not mutated");
  const r2 = withAnswer(RES, { piece: 2, field: "d" }, 65);
  assert.deepEqual(r2.ops.find(o => o.path === "pieces.p1.d"), { op: "set", path: "pieces.p1.d", value: 65 });
  const r3 = withAnswer(RES, { piece: 2, field: "h" }, 250);
  assert.deepEqual(r3.ops.at(-1), { op: "set", path: "pieces.p1.h", value: 250 });
  const r4 = withAnswer(RES, { piece: null, field: "type" }, null);
  assert.deepEqual(r4.ops, RES.ops);
});

test("Verstanden: Übernehmen takes suggestions, refuses vague text and nothing-to-do", () => {
  const a = applicable(RES);
  assert.equal(a.ok, true);
  assert.equal(a.res.ops[1].value.w, 120);
  assert.equal(withDefaults({ ...RES, questions: [{ piece: null, field: "type", default: null }] }), null);
  const vague = applicable({ ...RES, questions: [{ piece: null, field: "type", question_de: "Was?", default: null }] });
  assert.equal(vague.ok, false);
  assert.match(vague.why, /genauer/);
  assert.equal(applicable({ ...RES, ops: [], questions: [], removes: [] }).why, "Nichts zu übernehmen.");
  assert.equal(applicable({ ...RES, ops: [], questions: [] }).why, null);     // only a photo removal: explained in the card
  assert.equal(applicable({ status: "unavailable" }).ok, false);
  assert.equal(focusOf(RES), "n1");
  assert.match(waitText("hear", false), /bis zu 1 Minute/);
  assert.match(waitText("hear", true), /läuft/);
  assert.match(waitText("understand"), /verstanden/);
});

test("Verstanden: undo restores the doc exactly (adds, sets, drops with replacement)", () => {
  const ops = applicable(RES).res.ops;
  const after = applyOps(DOC, ops);
  assert.equal(after.pieces.length, 3);
  const back = applyOps(after, undoOps(DOC, ops));
  assert.deepEqual(back.pieces, DOC.pieces);
  const drop = [{ op: "remove_piece", id: "p1", replacement: { id: "r9", type: null, material: "eiche", handles: "push", placement: { mode: "none", pose: null, box: null } } }];
  const gone = applyOps(DOC, drop);
  assert.deepEqual(gone.pieces.map(p => p.id), ["r9"]);
  assert.deepEqual(applyOps(gone, undoOps(DOC, drop)).pieces, DOC.pieces);
  assert.deepEqual(undoOps(DOC, [{ op: "set", path: "pieces.p1.where", value: "links" }]), [{ op: "set", path: "pieces.p1.where", value: null }]);
});

test("Verstanden: a spoken correction before Übernehmen joins the description", () => {
  assert.equal(joinText("", "Sideboard 180"), "Sideboard 180");
  assert.equal(joinText("Sideboard 180", "nein, 160 breit"), "Sideboard 180. nein, 160 breit");
  assert.equal(joinText("Sideboard 180.", " nein "), "Sideboard 180. nein");
  assert.equal(joinText("x", ""), "x");
});

// ---------------- Assistent

const RESULT_DATA = { variants: [{ id: "a", image: "20261018-1.png", score: 7.3, pass: false, status: "ok" }, { id: "b", image: "20261018-2.png", score: 8.5, pass: true, status: "ok" },
  { id: "c", image: "20261018-3.png", score: null, pass: null, status: "unavailable" }],
  offer_text: "Pos. 1 …", pdf: "/outputs/exports/workshop-fc44/entwurf/V.pdf", files: ["exports/workshop-fc44/assistent/zeichnung.dxf"],
  models: [{ piece: "r0", label: "Einbauschrank", glb: "exports/workshop-fc44/assistent/modell_r0.glb", stl: "/api/workshop/fc44/model/r0.stl" }] };

test("Assistent: server references become URLs under /outputs, absolute ones stay", () => {
  assert.equal(fileUrl("uploads/20261018-62985fd7fe.png"), "/outputs/uploads/20261018-62985fd7fe.png");
  assert.equal(fileUrl("exports/x/vorschau-0007.jpg"), "/outputs/exports/x/vorschau-0007.jpg");
  assert.equal(fileUrl("/outputs/exports/a.pdf"), "/outputs/exports/a.pdf");
  assert.equal(fileUrl("/api/workshop/x/model/y.stl"), "/api/workshop/x/model/y.stl");
  assert.equal(fileUrl("https://example.org/a.png"), "https://example.org/a.png");
  assert.equal(fileUrl(""), "");
  assert.equal(fileUrl(null), "");
});

test("Assistent: events merge by n without duplicates, in order", () => {
  const a = [{ n: 1, role: "user" }, { n: 2, role: "agent", kind: "status" }];
  const m = mergeEvents(a, [{ n: 2, role: "agent", kind: "status", text: "neu" }, { n: 4, role: "agent", kind: "preview" }, { n: 3, role: "agent", kind: "text" }, { n: "x" }, null]);
  assert.deepEqual(m.map(e => e.n), [1, 2, 3, 4]);
  assert.equal(m[1].text, "neu");
  assert.equal(lastAgent(m).n, 4);
  assert.equal(lastAgent(m, ["status"]).n, 2);
  assert.equal(lastAgent([], ["status"]), null);
});

test("Assistent: quick replies follow the conversation state", () => {
  const q = (state, events = [], busy = false) => quickReplies({ state, events, busy }, "fc44");
  assert.deepEqual(q("preview").map(x => x.send || x.action), ["ja", "edit"]);
  assert.equal(q("preview")[0].label, "Ja, so rechnen");
  assert.deepEqual(q("generating").map(x => x.send), ["stopp", "status"]);
  assert.deepEqual(q("done").map(x => x.send || x.action || x.href), ["nochmal", "edit", "#/p/fc44/ergebnis"]);
  assert.equal(q("need_photo")[0].action, "photo");
  assert.deepEqual(q("analyzing"), []);
  assert.deepEqual(q("preview", [], true), []);
  const ask = data => q("asking", [{ n: 1, role: "agent", kind: "question", data }]);
  assert.deepEqual(ask({ field: "w", default: 160 })[0], { label: "Vorschlag nehmen: 160 cm", send: "ja", kind: "primary" });
  assert.equal(ask({ field: "where" })[0].send, "egal");
  assert.equal(ask({ field: "photo" })[0].action, "photo");
  assert.equal(ask({ field: "type", default: null })[0].action, "edit");
  assert.equal(q("error")[0].send, "hilfe");
  const clash = q("preview", [{ n: 3, role: "agent", kind: "preview", data: { conflict: { fix: "An die freie Wand rechts neben den Fernseher setzen." } } }]);
  assert.deepEqual(clash.map(x => x.send || x.action), ["Vorschlag übernehmen", "ja, trotzdem rechnen", "edit"]);
  assert.equal(clash[0].kind, "primary");
  assert.equal(clash[0].label, "Vorschlag übernehmen");      // short: the suggestion itself stands in the message above
  assert.equal(clash[1].kind, undefined);
  assert.deepEqual(q("preview", [{ n: 3, role: "agent", kind: "preview", data: { conflict: null } }]).map(x => x.send || x.action), ["ja", "edit"]);
  assert.deepEqual(quickReplies(null, "x"), []);
  for (const s of ["need_photo", "analyzing", "understanding", "asking", "preview", "generating", "done", "error"]) assert.ok(STATE_DE[s], s);
});

test("Assistent: result links, scores and texts", () => {
  const links = resultLinks(RESULT_DATA, "fc44");
  assert.deepEqual(links.map(l => l.label), ["Kundenblatt (PDF)", "Zeichnung (DXF)", "3D: Einbauschrank (GLB)", "Angebotstext (TXT)"]);
  assert.deepEqual(links.map(l => l.href), ["/outputs/exports/workshop-fc44/entwurf/V.pdf", "/outputs/exports/workshop-fc44/assistent/zeichnung.dxf",
    "/outputs/exports/workshop-fc44/assistent/modell_r0.glb", "/api/workshop/fc44/export/angebot.txt"]);
  assert.deepEqual(resultLinks({}, "x"), []);
  assert.deepEqual(resultLinks({ files: [{ path: "exports/a/b.csv" }, {}] }, "x").map(l => l.label), ["b.csv"]);
  assert.deepEqual(RESULT_DATA.variants.map(scoreOf).map(s => [s.text, s.tone]), [["⚠ 7,3", "warn"], ["✓ 8,5", "ok"], ["ungeprüft", "muted"]]);
  assert.deepEqual(scoreOf({ status: "ok", score: 3, pass: false }), { text: "✗ 3,0", tone: "bad" });
  assert.deepEqual(splitText("So geplant – passt das?\n1. Einbauschrank\n2. Sideboard"), { head: "So geplant – passt das?", rest: "1. Einbauschrank\n2. Sideboard" });
  assert.deepEqual(splitText(null), { head: "", rest: "" });
  assert.equal(hhmm("2026-10-18T23:11:35"), "23:11");
  assert.equal(hhmm(""), "");
});

test("Assistent: polling speed and start validation", () => {
  assert.equal(pollDelay({ state: "analyzing", busy: true }), 1500);
  assert.equal(pollDelay({ state: "generating", busy: false }), 1500);
  assert.equal(pollDelay({ state: "preview", busy: false }), 5000);
  assert.equal(pollDelay({ state: "preview" }, true), 15000);
  assert.equal(pollDelay(null), 3000);
  assert.match(startProblem({}), /Raumfoto/);
  assert.equal(startProblem({ photo: {} }), null);
  assert.equal(startProblem({ audio: {} }), null);
  assert.equal(startProblem({ text: "Sideboard" }), null);
  assert.match(startProblem({ photo: {}, text: "x".repeat(2001) }), /zu lang/);
});

// ---------------- settings sections

test("Firmenprofil: type patch stores new values, emptied fields inherit again, unchanged ones are left out", () => {
  assert.deepEqual(typePatch({}, { w: null, h: null, d: 65, elev: null, full_height: null }), { d: 65 });
  assert.deepEqual(typePatch({ d: 65 }, { w: null, h: null, d: null, elev: null, full_height: null }), { d: null });
  assert.deepEqual(typePatch({ d: 65 }, { d: 65 }), {});
  assert.deepEqual(typePatch({}, { full_height: false }), { full_height: false });
  assert.deepEqual(typePatch({ full_height: false }, { full_height: false }), {});
  assert.deepEqual(typePatch({}, { doors: { per: 60 } }), { doors: { per: 60 } });
  assert.deepEqual(typePatch({ doors: { per: 60 } }, { doors: { per: null } }), { doors: null });
  assert.deepEqual(typePatch({ doors: { per: 60 } }, { doors: { per: 60 } }), {});
});

test("Firmenprofil: alias changes as a merge patch", () => {
  const before = { type: {}, material: { Schleiflack: "weiss", Altholz: "custom:Altholz" }, handles: {} };
  const after = { type: { Wandschrank: "schrank" }, material: { Schleiflack: "weiss" }, handles: { Knopf: "knopf" } };
  assert.deepEqual(aliasPatch(before, after), { type: { Wandschrank: "schrank" }, material: { Altholz: null }, handles: { Knopf: "knopf" } });
  assert.deepEqual(aliasPatch(before, before), {});
  assert.deepEqual(aliasPatch(undefined, { material: { a1: "eiche" } }), { material: { a1: "eiche" } });
});

test("Kanäle: secrets are write-only, lists and chat ids are parsed strictly, status lines are honest", () => {
  assert.deepEqual(secretValue(""), { set: false });
  assert.deepEqual(secretValue("  123:ABC  "), { set: true, value: "123:ABC" });
  assert.deepEqual(secretValue("x", true), { set: true, value: null });
  assert.deepEqual(parseList("a@b.de, @firma.de\n\na@b.de;c@d.de"), ["a@b.de", "@firma.de", "c@d.de"]);
  const { chats, bad } = parseChats("123456 Peter\n-100987654321 Gruppe Werkstatt\nhallo\n  42  ");
  assert.deepEqual(chats, [{ id: 123456, name: "Peter" }, { id: -100987654321, name: "Gruppe Werkstatt" }]);
  assert.deepEqual(bad, ["hallo", "42"]);
  assert.equal(chatsText(chats), "123456 Peter\n-100987654321 Gruppe Werkstatt");
  assert.deepEqual(channelLine({ enabled: false, status: {} }), { tone: "muted", text: "Aus – keine Verbindung ins Internet" });
  assert.equal(channelLine({ enabled: true, status: { running: true } }).tone, "ok");
  assert.equal(channelLine({ enabled: true, status: { running: false, fatal: "Token prüfen" } }).text, "Gestoppt: Token prüfen");
  assert.match(channelLine({ enabled: true, status: { running: false } }).text, /Neustart/);
  assert.equal(ENABLE_WORD, "einschalten");
});

test("API: curl example uses the page origin and never a real key by default", () => {
  const c = curlExample("http://192.168.178.111:7860");
  assert.match(c, /Bearer wk_…/);
  assert.match(c, /http:\/\/192\.168\.178\.111:7860\/api\/v1\/orders$/);
  assert.match(curlExample("http://x", "wk_abc"), /Bearer wk_abc/);
});
