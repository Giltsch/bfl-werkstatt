// node --test tests/ws_orders.test.mjs — pure logic of the Aufträge dashboard, Kunde & Auftrag and Einstellungen.
import test from "node:test";
import assert from "node:assert/strict";
import { filterList, counts, sortList, attentionBadge, dueCount, running, matches, nextAppt } from "../static/ws/dashboard.js";
import { icsText, icsName, isBackward, positionsVariant, telHref, mailHref, routeHref, nameOf, PIPELINE } from "../static/ws/auftrag.js";
import { legacyCompany, migrateCompany, fmtBytes, validBaseUrl, LEGACY_KEY, MIGRATED_KEY } from "../static/ws/settings.js";
import { positionsText } from "../static/ws/ui.js";

const P = (id, o = {}) => ({id, title: id, customer: "", status: "entwurf", ref: "", ext_ref: "", city: "", appointment: null,
  archived_at: null, test: false, retention_due: false, updated: "2026-10-10T10:00:00", attention: null, variants: 0, ...o,
  q: (o.q || [id, o.customer, o.ref, o.city].filter(Boolean).join(" ")).toLowerCase()});

const LIST = [
  P("kueche", {customer: "Müller", status: "anfrage", ref: "2026-001", city: "Ansbach"}),
  P("flur", {customer: "Huber", status: "kunde", ref: "2026-002", attention: "aenderung"}),
  P("bad", {status: "fertig", ref: "2026-003", retention_due: true}),
  P("alt", {status: "abgesagt", archived_at: "2026-01-01T00:00:00", ref: "2025-010", retention_due: true}),
  P("archiv", {status: "entwurf", archived_at: "2026-05-01T00:00:00", customer: "Schmidt"}),
  P("TEST e2e", {status: "entwurf", test: true, retention_due: true}),
  P("wohnen", {status: "termin", appointment: {at: "2026-10-17T15:00", what: "Aufmaß"}, attention: "termin_heute"}),
];
const F = o => ({q: "", chip: "aktiv", archive: false, test: false, ...o});
const ids = l => l.map(p => p.id).sort();

test("Alle aktiven hides finished, cancelled, archived and TEST orders", () => {
  assert.deepEqual(ids(filterList(LIST, F())), ["flur", "kueche", "wohnen"]);
  assert.deepEqual(ids(filterList(LIST, F({test: true}))), ["TEST e2e", "flur", "kueche", "wohnen"]);
});

test("status chips, archive toggle and due filter", () => {
  assert.deepEqual(ids(filterList(LIST, F({chip: "fertig"}))), ["bad"]);
  assert.deepEqual(ids(filterList(LIST, F({chip: "entwurf"}))), []);
  assert.deepEqual(ids(filterList(LIST, F({chip: "entwurf", archive: true}))), ["archiv"]);
  assert.deepEqual(ids(filterList(LIST, F({archive: true}))), ["alt", "archiv"]);
  // „Zur Löschung fällig“ covers archived ones too, TEST only with the toggle
  assert.deepEqual(ids(filterList(LIST, F({chip: "due"}))), ["alt", "bad"]);
  assert.equal(dueCount(LIST, F()), 2);
  assert.equal(dueCount(LIST, F({test: true})), 3);
  const n = counts(LIST, F());
  assert.equal(n.aktiv, 3); assert.equal(n.kunde, 1); assert.equal(n.fertig, 1); assert.equal(n.due, 2); assert.equal(n.entwurf, 0);
});

test("search: all tokens, over q, and under „Alle aktiven“ through everything incl. archive", () => {
  assert.ok(matches(LIST[0], "müller ansbach"));
  assert.ok(!matches(LIST[0], "müller huber"));
  assert.deepEqual(ids(filterList(LIST, F({q: "2026-00"}))), ["bad", "flur", "kueche"]);
  assert.deepEqual(ids(filterList(LIST, F({q: "schmidt"}))), ["archiv"]);
  assert.deepEqual(ids(filterList(LIST, F({q: "schmidt", chip: "anfrage"}))), []);
  assert.deepEqual(ids(filterList(LIST, F({q: "  HUBER "}))), ["flur"]);
});

test("sort: Wichtig zuerst = attention, then next appointment, then updated", () => {
  const now = new Date("2026-10-17T08:00:00");
  const l = [
    P("a", {updated: "2026-10-16T10:00:00"}),
    P("b", {updated: "2026-10-17T07:00:00"}),
    P("c", {appointment: {at: "2026-10-25T09:00", what: "Montage"}}),
    P("d", {appointment: {at: "2026-10-19T09:00", what: "Aufmaß"}}),
    P("e", {attention: "termin_heute", appointment: {at: "2026-10-17T14:00", what: "Aufmaß"}}),
    P("f", {attention: "freigegeben"}),
    P("g", {attention: "ueberfaellig", appointment: {at: "2026-10-10T09:00", what: "Aufmaß"}}),
    P("h", {attention: "aenderung"}),
    P("i", {appointment: {at: "2026-09-01T09:00", what: "Montage"}, updated: "2026-10-01T10:00:00"}),
  ];
  assert.deepEqual(sortList(l, "wichtig", now).map(p => p.id), ["h", "g", "f", "e", "d", "c", "b", "a", "i"]);
  assert.equal(nextAppt(l[8], now), null);   // a past appointment without „überfällig“ no longer counts
  const byUpd = sortList(l, "updated", now).map(p => p.id);
  assert.deepEqual(byUpd.slice(0, 2), ["b", "a"]); assert.equal(byUpd.at(-1), "i");
  assert.deepEqual(sortList(l, "termin", now).map(p => p.id).slice(0, 5), ["i", "g", "e", "d", "c"]);
  const r = [P("x", {ref: "2026-002"}), P("y", {ref: ""}), P("z", {ref: "2026-010"}), P("w", {ref: "2025-099"})];
  assert.deepEqual(sortList(r, "nummer", now).map(p => p.id), ["z", "x", "w", "y"]);
  assert.deepEqual(sortList([P("o", {updated: "2026-01-01T00:00:00"}), P("n", {updated: "2026-02-01T00:00:00"})], "updated").map(p => p.id), ["n", "o"]);
});

test("attention badges and running marker", () => {
  assert.deepEqual(attentionBadge("aenderung"), {text: "Änderungswunsch", tone: "warn"});
  assert.equal(attentionBadge("freigegeben").text, "Freigegeben ✓");
  assert.equal(attentionBadge("ueberfaellig").text, "überfällig");
  assert.equal(attentionBadge("termin_heute").text, "heute");
  assert.equal(attentionBadge(null), null);
  const q = {jobs: [{id: "j1", project: "flur", state: "running"}, {id: "j2", project: "bad", state: "queued"}]};
  assert.ok(running(q, "flur")); assert.ok(running(q, "bad")); assert.ok(!running(q, "kueche")); assert.ok(!running({}, "flur"));
});

const DOC = {id: "bb505aeddc04", title: "Einbauschrank Flur", customer: "Familie Müller, Ansbach", ref: "2026-002", ext_ref: "A-123",
  contact: {phone: "0981 1234", email: "m@example.de", street: "Hauptstr. 1; Hinterhaus", zip: "91522", city: "Ansbach"},
  appointment: {at: "2026-10-20T09:00", what: "Montage"}, internal_notes: "GEHEIM Rabatt 20 %"};

test("ICS: valid calendar text, local time, escapes, folding, never internal notes", () => {
  const t = icsText(DOC, {company: {name: "Schreinerei Muster"}, now: new Date("2026-10-17T10:00:00Z")});
  assert.ok(t.startsWith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"));
  assert.ok(t.endsWith("END:VCALENDAR\r\n"));
  assert.ok(!/[^\r]\n/.test(t), "CRLF only");
  const un = t.replace(/\r\n /g, "");
  assert.match(un, /\r\nDTSTART:20261020T090000\r\n/);
  assert.match(un, /\r\nDTEND:20261020T130000\r\n/);   // Montage = 4 h
  assert.match(un, /\r\nSUMMARY:Montage: Familie Müller\\, Ansbach\r\n/);
  assert.match(un, /\r\nLOCATION:Hauptstr\. 1\\; Hinterhaus\\, 91522 Ansbach\r\n/);
  assert.match(un, /DESCRIPTION:Projekt: Einbauschrank Flur\\nVorgang 2026-002\\nAngebot Nr\. A-123\\nTelefon: 0981 1234/);
  assert.match(un, /\r\nDTSTAMP:20261017T100000Z\r\n/);
  assert.ok(!t.includes("GEHEIM"));
  const enc = new TextEncoder();
  for (const line of t.split("\r\n")) assert.ok(enc.encode(line).length <= 75, `line too long: ${line}`);
  const aufmass = icsText({...DOC, appointment: {at: "2026-10-20T09:00", what: "Aufmaß"}});
  assert.match(aufmass, /DTEND:20261020T100000/);
  assert.equal(icsText({...DOC, appointment: null}), null);
  assert.equal(icsName(DOC), "Termin_Familie_Müller_Ansbach.ics");
});

test("backward status moves ask, forward and Abgesagt do not", () => {
  assert.deepEqual(PIPELINE, ["anfrage", "termin", "entwurf", "kunde", "freigegeben", "auftrag", "fertig"]);
  assert.ok(isBackward("kunde", "entwurf"));
  assert.ok(isBackward("fertig", "anfrage"));
  assert.ok(!isBackward("entwurf", "kunde"));
  assert.ok(!isBackward("entwurf", "entwurf"));
  assert.ok(!isBackward("kunde", "abgesagt"));
  assert.ok(!isBackward("abgesagt", "anfrage"));
});

test("Positionstext uses the approved proposal, else the first ★ one, never a deleted one", () => {
  const v = (id, o = {}) => ({id, fav: false, piece_ids: ["p1"], pieces: [{id: "p1", label: "Esstisch", material_label: "Eiche natur geölt",
    handles_label: "grifflos (Push)", w: 180, h: 76, d: 90}], ...o});
  const doc = {title: "Esszimmer", ref: "2026-002", pieces: [{id: "p1"}], variants: [v("a"), v("b", {fav: true}), v("c", {fav: true})],
    deleted_variants: ["b"], versions: [], approved: null};
  assert.equal(positionsVariant(doc), "c");
  assert.equal(positionsVariant({...doc, variants: [v("a")]}), null);
  const appr = {...doc, versions: [{n: 2, variant_ids: ["c", "a"]}], approved: {version: 2, variant_id: "a", at: "2026-10-18T10:00:00"}};
  assert.equal(positionsVariant(appr), "a");
  const text = positionsText(appr, positionsVariant(appr), null);
  assert.equal(text, "Esstisch, Eiche natur geölt, grifflos (Push), B 180 × H 76 × T 90 cm\n" +
    "(Visualisierung Vorgang 2026-002, Version 2, Entwurf 2 – freigegeben am 18.10.2026)");
});

test("contact links", () => {
  assert.equal(telHref("0981 / 12 34"), "tel:09811234");
  assert.equal(telHref("+49 981 1234"), "tel:+499811234");
  assert.equal(telHref(""), null);
  assert.equal(mailHref("m@example.de", "Küche"), "mailto:m@example.de?subject=K%C3%BCche");
  assert.equal(mailHref("kein mail"), null);
  assert.equal(mailHref("a@b\"><img>.de"), null);
  assert.equal(routeHref({street: "Hauptstr. 1", zip: "91522", city: "Ansbach"}),
    "https://www.openstreetmap.org/search?query=Hauptstr.%201%2C%2091522%20Ansbach");
  assert.equal(routeHref({}), null);
  assert.equal(nameOf("anna", [{id: "anna", name: "Anna"}]), "Anna");
  assert.equal(nameOf("dev:8f3a1c2e"), "Gerät 8f3a");
  assert.equal(nameOf("kunde"), "Kunde");
});

function memStorage(init = {}) {
  const m = new Map(Object.entries(init));
  return {getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), map: m};
}

test("Firmendaten: the old per-browser data is taken over once, only when the server has none", async () => {
  const old = JSON.stringify({name: " Schreinerei Alt ", contact: "Hauptstr. 1 · Ansbach", color: "#3D5A4A", logo: "uploads/20261016-ab12.png"});
  assert.deepEqual(legacyCompany(old), {name: "Schreinerei Alt", contact: "Hauptstr. 1 · Ansbach", color: "#3d5a4a", logo: "uploads/20261016-ab12.png"});
  assert.deepEqual(legacyCompany(JSON.stringify({name: "X", color: "red", logo: "../etc/passwd"})), {name: "X"});
  assert.equal(legacyCompany("{kaputt"), null);
  assert.equal(legacyCompany(JSON.stringify({name: ""})), null);

  const calls = [];
  const put = async body => { calls.push(body); return {company: body.company}; };
  const st = memStorage({[LEGACY_KEY]: old});
  const r1 = await migrateCompany({settings: {company: {name: ""}}, storage: st, put});
  assert.equal(calls.length, 1);
  assert.equal(calls[0].company.name, "Schreinerei Alt");
  assert.ok(r1);
  assert.equal(st.getItem(MIGRATED_KEY), "1");
  assert.equal(await migrateCompany({settings: {company: {name: ""}}, storage: st, put}), null);
  assert.equal(calls.length, 1, "only once");

  const st2 = memStorage({[LEGACY_KEY]: old});
  assert.equal(await migrateCompany({settings: {company: {name: "Schreinerei Neu"}}, storage: st2, put}), null);
  assert.equal(calls.length, 1, "server data wins");
  assert.equal(st2.getItem(MIGRATED_KEY), "1");

  assert.equal(await migrateCompany({settings: {company: {}}, storage: memStorage(), put}), null);
  assert.equal(await migrateCompany({settings: {company: {}}, storage: null, put}), null);
  const failing = {getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); }};
  assert.equal(await migrateCompany({settings: {company: {}}, storage: failing, put}), null);
  assert.equal(calls.length, 1);
});

test("settings helpers", () => {
  assert.equal(fmtBytes(812345678), "812 MB");
  assert.equal(fmtBytes(1234567890), "1,2 GB");
  assert.equal(fmtBytes(0), "0 MB");
  assert.ok(validBaseUrl(""));
  assert.ok(validBaseUrl("https://werkstatt.example.de"));
  assert.ok(!validBaseUrl("http://192.168.178.111:7860"));
  assert.ok(!validBaseUrl("javascript:alert(1)"));
});
