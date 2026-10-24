// Pure logic of steps ④/⑤, Versand and Präsentation (no DOM). Run: node --test tests/ws_results.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { ratingOf, positionsText, shareOrFallback, SECURE, versionBadge } from "../static/ws/ui.js";
import B, { summary, protectState, queueLine, captionNotice, primaryOf as primary4, isAnalyzing, mins, activeJobs } from "../static/ws/step-berechnen.js";
import E, { placeholders, variantBadges, primaryOf as primary5, chosenIds, responseBanner, sortVariants, versionLine, personName, VSTATUS, bestOf } from "../static/ws/step-ergebnis.js";
import { sendWarnings, validateSelection, defaultNote, shareText, mailtoUrl, waUrl, isLocalLink, versionLinks, MAX_SEND, messageText, waNumber, greeting, LINK_INTRO } from "../static/ws/send.js";
import { beforeOf, aspectOf, clampPos, startPos, focusOf } from "../static/ws/present.js";

const CAT = {
  pieces: { sideboard: { de: "Sideboard" }, tisch: { de: "Esstisch" } },
  materials: { eiche: { de: "Eiche natur geölt" }, nuss: { de: "Nussbaum" }, custom: { de: "Eigene Angabe…" } },
  handles: { push: { de: "grifflos (Push)" }, leiste: { de: "Griffleiste" } },
};
const box = [100, 200, 500, 600];
const piece = (id, extra = {}) => ({ id, type: "sideboard", material: "eiche", handles: "push", w: 180, h: 75, d: 45,
  placement: { mode: "3d", pose: { x: 0, z: 300, yaw: 0 }, box }, ...extra });
const judge = (overall, pass) => ({ status: "ok", overall, pass, scores: { room_preserved: 9 } });
const doc = (extra = {}) => ({
  id: "p1", title: "Esszimmer", customer: "Familie Beispiel", ref: "2026-002", room: "uploads/room.png", room_size: [1344, 1008],
  protect: true, room_changes: "", count: 2, quality: "entwurf", judge: { enabled: true, auto_retry: false },
  pieces: [piece("a1"), piece("b2", { type: "tisch", material: "nuss", handles: "leiste" })],
  variants: [], versions: [], approved: null, trash: [], ...extra,
});

test("rating mapping (§10.4 ratingOf) used for chips and send warnings", () => {
  assert.equal(ratingOf(judge(8.5, true)).tone, "ok");
  assert.match(ratingOf(judge(8.5, true)).text, /^✓ Gut \(8,5\)$/);
  assert.equal(ratingOf(judge(5.7, false)).tone, "warn");
  assert.match(ratingOf(judge(5.7, false)).text, /Bitte prüfen \(5,7\)/);
  assert.equal(ratingOf(judge(3.2, false)).tone, "bad");
  assert.equal(ratingOf({ status: "pending" }).text, "Prüfung läuft …");
  assert.equal(ratingOf(null).text, "ohne Prüfung");
  assert.equal(ratingOf({ status: "error" }).text, "Prüfung nicht möglich");
});

test("step ④ summary, scope line and protect states", () => {
  const d = doc();
  const s = summary(d, CAT);
  assert.equal(s.line, "2 Möbel · Eiche natur geölt, Nussbaum · grifflos (Push), Griffleiste");
  assert.equal(s.scope, "Rest des Fotos bleibt unverändert");
  assert.equal(protectState(d).text, "Außerhalb der Möbel bleibt das Foto pixelgenau erhalten.");
  assert.equal(protectState({ ...d, protect: false }).text, "Aus: das ganze Bild darf neu gezeichnet werden.");
  assert.equal(summary({ ...d, protect: false }, CAT).scope, "Ganzes Bild wird neu gezeichnet");
  assert.equal(protectState({ ...d, room_changes: "Wand grün" }).text, "Wirkt nicht, solange „Raum selbst ändern“ ausgefüllt ist.");
  const unplaced = { ...d, pieces: [piece("a1"), piece("b2", { placement: { mode: "none", pose: null, box: null } })] };
  assert.equal(protectState(unplaced).text, "Wirkt erst, wenn alle Möbel platziert sind.");
  assert.equal(summary({ ...d, room: null }, CAT).scope, "Freie Visualisierung (ohne Raumfoto)");
  const custom = { ...d, pieces: [piece("a1", { material: "custom", material_custom: "Lack Salbeigrün" })] };
  assert.match(summary(custom, CAT).line, /^1 Möbel · Lack Salbeigrün · grifflos/);
  // a piece without type and text does not count
  assert.equal(summary({ ...d, pieces: [piece("a1", { type: null })] }, CAT).count, 0);
});

test("step ④ queue line and caption notice", () => {
  assert.equal(queueLine({ wait_s: 0 }, { jobs: [] }), "Rechner frei – Start sofort.");
  const q = { jobs: [{ id: "j1", state: "running", owner: "ben", owner_name: "Ben", project_title: "Küche Huber", label: "2 Vorschläge" }] };
  assert.equal(queueLine({ wait_s: 170 }, q, "anna"), "Vor Ihnen: 1 Auftrag (Ben – Küche Huber) – Start in ca. 3 Min.");
  assert.equal(queueLine({ wait_s: 0 }, { ...q, wait_s: 170 }, "anna"), "Vor Ihnen: 1 Auftrag (Ben – Küche Huber) – Start in ca. 3 Min.");  // live queue beats a stale dry run
  const q2 = { jobs: [...q.jobs, { id: "j2", state: "queued", owner: "anna", owner_name: "Anna", project_title: "Flur" }] };
  assert.match(queueLine({ wait_s: 300 }, q2, "anna"), /^Vor Ihnen: 2 Aufträge \(Ben – Küche Huber, Sie – Flur\) – Start in ca\. 5 Min\.$/);
  assert.equal(captionNotice({ caption: { backend: "vllm", state: "up" } }), null);
  assert.match(captionNotice({ caption: { backend: "template", state: "starting" } }), /^Beschreibungs-KI startet gerade\./);
  assert.match(captionNotice({ caption: { backend: "template", state: "down" } }), /^Beschreibungs-KI ist aus – die Bilder werden ohne sie berechnet/);
  assert.equal(captionNotice({ caption: { backend: "cache", state: "down" } }), null, "cache hit needs no caption");
  assert.equal(captionNotice({ caption: { backend: "template", state: "up" } }), null, "template by design");
  assert.match(captionNotice(null, { state: "down" }), /ist aus/);
});

test("step ④ primary: minutes, disabled reasons, Zum Ergebnis while running", () => {
  const d = doc();
  assert.equal(primary4({ doc: d, jobs: [], estimate: 125 }).label, "Bilder berechnen (ca. 2 Min.)");
  assert.equal(primary4({ doc: d, jobs: [], estimate: 206 }).label, "Bilder berechnen (ca. 3½ Min.)");
  assert.equal(primary4({ doc: d, jobs: [], estimate: 125 }).disabled, false);
  const none = primary4({ doc: { ...d, pieces: [piece("a1", { type: null, text: "" })] }, jobs: [] });
  assert.equal(none.disabled, true);
  assert.equal(none.reason, "Erst ein Möbel auswählen (Schritt 2).");
  const an = primary4({ doc: d, jobs: [], analyzing: true });
  assert.equal(an.reason, "Raum wird noch vermessen …");
  const run = primary4({ doc: d, jobs: [{ id: "j", state: "running" }] });
  assert.equal(run.label, "Zum Ergebnis");
  assert.equal(run.disabled, false);
  assert.equal(primary4({ doc: d, jobs: [{ id: "j", state: "done" }] }).label, "Bilder berechnen");
  assert.equal(mins(20), 1); assert.equal(mins(300), 5);
  assert.equal(activeJobs([{ state: "queued" }, { state: "error" }]).length, 1);
  assert.equal(isAnalyzing(d, {}, {}, { analysisOf: () => ({ phase: "analyzing" }) }), true);
  assert.equal(isAnalyzing(d, {}, {}, { analysisOf: () => ({ phase: "failed" }) }), false);
  assert.equal(isAnalyzing({ ...d, room: null }, { analyzing: true }), false);
  assert.equal(B.id, "berechnen");
  assert.equal(B.state(d, {}, {}).done, false);
  assert.equal(B.state({ ...d, variants: [{ id: "v", kind: "room" }] }, {}, {}).done, true);
});

test("placeholders from project jobs (count − done_images, colleague label, retry)", () => {
  const jobs = [
    { id: "j1", state: "running", owner: "anna", owner_name: "Anna", count: 3, done_images: 1, stage_text: "Bild 2 von 3 wird gerechnet … Schritt 4/8", dedupe: "design" },
    { id: "j2", state: "queued", owner: "ben", owner_name: "Ben", count: 1, done_images: 0, stage_text: "In der Warteschlange – Platz 2, Start in ca. 3 Min.", dedupe: "material:v1" },
    { id: "j3", state: "running", owner: "anna", count: 1, done_images: 0, retry_of: "j0", stage_text: "Bild wird gerechnet … Schritt 1/16" },
    { id: "j4", state: "running", owner: "anna", count: 1, done_images: 0, dedupe: "render:a1", stage_text: "x" },
    { id: "j5", state: "done", owner: "anna", count: 2, done_images: 2 },
    { id: "j6", state: "error", owner: "anna", count: 2, done_images: 1 },
  ];
  const ph = placeholders(jobs, "anna");
  assert.equal(ph.filter(p => p.job === "j1").length, 2);
  assert.equal(ph.find(p => p.job === "j1").text, "Bild 2 von 3 wird gerechnet …");  // one overall bar: no per-stage step count
  assert.equal(ph.find(p => p.job === "j2").title, "Ben berechnet …");
  assert.equal(ph.find(p => p.job === "j1").who, null);
  assert.equal(ph.find(p => p.job === "j3").title, "Neuer Versuch mit Korrektur …");
  assert.equal(ph.find(p => p.job === "j4").render, true);
  assert.equal(ph.some(p => p.job === "j5" || p.job === "j6"), false);
  assert.equal(ph.length, 5);
});

test("badges, chosen proposals and the ⑤ primary reason", () => {
  const v = { id: "v1", kind: "room", caption_backend: "template", steps: 8 };
  const d = doc({ variants: [v, { id: "v2", kind: "room", steps: 16, caption_backend: "vllm" }], approved: { variant_id: "v2", version: 1 } });
  const b1 = variantBadges(v, d).map(b => b.text);
  assert.deepEqual(b1, ["ohne Beschreibungs-KI", "Entwurfsqualität"]);
  assert.match(variantBadges(v, d)[0].explain, /ohne Beschreibungs-KI berechnet, weil sie gerade nicht lief/);
  assert.deepEqual(variantBadges(d.variants[1], d).map(b => b.text), ["Freigegebener Entwurf"]);
  const p = primary5(d);
  assert.equal(p.label, "An Kunden senden …");
  assert.equal(p.disabled, true);
  assert.equal(p.reason, "Erst einen Entwurf mit ★ für den Kunden auswählen.");
  d.variants[1].fav = true;
  assert.deepEqual(chosenIds(d), ["v2"]);
  assert.equal(primary5(d).disabled, false);
  assert.equal(primary5(d).reason, null);
  assert.equal(E.state(doc(), {}, { projectJobs: () => [] }).available, false);
  assert.equal(E.state(doc(), {}, { projectJobs: () => [{ state: "queued" }] }).available, true);
  assert.equal(E.state(d, {}, { projectJobs: () => [] }).available, true);
});

test("send validation and the one warning list (failed / template / draft quality)", () => {
  assert.equal(validateSelection([]), "Bitte mindestens einen Entwurf wählen.");
  assert.equal(validateSelection(["a", "b", "c", "d", "e"]), "Höchstens 4 Entwürfe pro Version.");
  assert.equal(validateSelection(["a"]), null);
  assert.equal(MAX_SEND, 4);
  const sel = [
    { id: "a", judge: judge(8.5, true), caption_backend: "vllm", steps: 16 },
    { id: "b", judge: judge(5.1, false), caption_backend: "template", steps: 16 },
    { id: "c", judge: judge(2.0, false), steps: 8 },
    { id: "d", judge: null, steps: 16 },
  ];
  const w = sendWarnings(sel);
  assert.deepEqual(w.map(x => [x.n, x.kind]), [[2, "fail"], [2, "template"], [3, "fail"], [3, "draft"]]);
  assert.equal(w[0].text, "Entwurf 2 hat die Prüfung nicht bestanden – trotzdem senden?");
  assert.equal(w[1].text, "Entwurf 2 wurde ohne Beschreibungs-KI berechnet – trotzdem senden?");
  assert.equal(w[3].text, "Entwurf 3 ist in Entwurfsqualität – für den Kunden besser „Fein“ berechnen.");
  assert.deepEqual(sendWarnings([sel[0], sel[3]]), [], "pass and unrated proposals send without a dialog");
});

test("delivery texts: the customer link goes into WhatsApp/E-Mail with the note; recipients from the order", async () => {
  const note = "Guten Tag Herr Müller,\nanbei unsere Entwürfe für Flur.\nViele Grüße\nSchreinerei Muster";
  const link = "http://192.168.178.111:7860/k/tok";
  assert.equal(messageText(note, link), `Guten Tag Herr Müller,\nanbei unsere Entwürfe für Flur.\n\n${LINK_INTRO}\n${link}\n\nViele Grüße\nSchreinerei Muster`);
  assert.equal(messageText("", link, "Schreinerei Muster"), `Ihre Entwürfe von Schreinerei Muster: ${link}`);
  assert.match(waUrl(messageText(note, link), "0981 123456"), /^https:\/\/wa\.me\/49981123456\?text=Guten%20Tag%20Herr%20M%C3%BCller.*%2Fk%2Ftok/);
  assert.equal(waNumber("+49 171 / 123 45 67"), "491711234567");
  assert.equal(waNumber("0049 171 1234567"), "491711234567");
  assert.equal(waNumber("kein"), "");
  assert.equal(greeting({ customer: "Max Müller", contact: { salutation: "Herr" } }), "Guten Tag Herr Müller,");
  assert.equal(greeting({ customer: "TEST Kunde Abnahme", contact: { salutation: "Familie" } }), "Guten Tag Familie Abnahme,");
  assert.equal(greeting({ customer: "Familie Beispiel", contact: {} }), "Guten Tag Familie Beispiel,");
  assert.equal(greeting({ customer: "TEST Kunde Abnahme", contact: {} }), "Guten Tag,");
});

test("best proposal of a graded batch; the slider starts with the furniture on the Nachher side", () => {
  const j = (overall, pass) => ({ status: "ok", overall, pass });
  const a = { id: "a", job: "J", image: "2.png", judge: j(6.3, false) }, b = { id: "b", job: "J", image: "3.png", judge: j(7.0, false) };
  assert.equal(bestOf([a, b]).id, "b");
  assert.equal(bestOf([a, { ...b, judge: { status: "pending" } }]), null, "not before every image of the batch is graded");
  assert.equal(bestOf([a]), null, "a single image is no choice");
  const room = { id: "r", kind: "room", boxes: { p1: [100, 40, 900, 300] } }, edit = { id: "e", kind: "material", from: "r", boxes: {} };
  const d = { variants: [room, edit] };
  assert.deepEqual(focusOf(edit, d), [0.04, 0.3]);
  assert.equal(startPos(edit, d), 1);
  assert.equal(startPos({ id: "x", kind: "room" }, d), 10);
  assert.equal(startPos({ id: "y", focus: [0.6, 0.9] }, d), 57);
  assert.deepEqual(primary5({ status: "freigegeben", approved: { variant_id: "a" }, variants: [] }), { label: "Weiter: Angebot & Auftrag", disabled: false, reason: null, next: "auftrag" });
  assert.equal(primary5({ status: "freigegeben", approved: { variant_id: "a", outdated: true }, variants: [] }).label, "An Kunden senden …");
});

test("delivery fallbacks without a secure context", async () => {
  assert.equal(SECURE, false);
  assert.equal(await shareOrFallback({ title: "x", text: "y", files: [] }), "fallback");
  assert.equal(shareText("Schreinerei Muster", null), "Ihre Entwürfe von Schreinerei Muster – das PDF sende ich Ihnen gleich mit.");
  assert.equal(shareText("Schreinerei Muster", "https://kunde.example.de/k/tok"), "Ihre Entwürfe von Schreinerei Muster: https://kunde.example.de/k/tok");
  assert.match(mailtoUrl("kunde@example.de", "Entwürfe: Esszimmer", "Hallo"), /^mailto:kunde@example\.de\?subject=Entw%C3%BCrfe%3A%20Esszimmer&body=Hallo$/);
  assert.match(waUrl("a b"), /^https:\/\/wa\.me\/\?text=a%20b$/);
  assert.equal(isLocalLink("http://127.0.0.1:7860/k/x"), true);
  assert.equal(isLocalLink("http://localhost:7860/k/x"), true);
  assert.equal(isLocalLink("http://192.168.178.111:7860/k/x"), false);
  const L = versionLinks("p1", { n: 2, token: "tok", status: "offen" }, { public_base_url: "https://kunde.example.de/" });
  assert.equal(L.public_link, "https://kunde.example.de/k/tok");
  assert.equal(L.pdf, "/api/workshop/p1/versions/2/pdf");
  assert.equal(versionLinks("p1", { n: 2, token: "tok", status: "offen" }, {}).public_link, null);
  assert.equal(versionLinks("p1", { n: 2, token: "tok", status: "offen", link: "http://192.168.178.111:7860/k/tok" }, {}).link, "http://192.168.178.111:7860/k/tok");
  assert.equal(versionLinks("p1", { n: 2, token: "tok", status: "zurueckgezogen" }, { public_base_url: "https://x.de" }).public_link, null);
});

test("default customer note", () => {
  assert.equal(defaultNote({ ...doc(), contact: { salutation: "Frau" }, customer: "Erika Mustermann" }, {}).split("\n")[0], "Guten Tag Frau Mustermann,");
  assert.equal(defaultNote(doc(), { name: "Schreinerei Muster" }),
    "Guten Tag Familie Beispiel,\nanbei unsere Entwürfe für Esszimmer. Sagen Sie uns gern, welcher Ihnen gefällt.\nViele Grüße\nSchreinerei Muster");
  assert.equal(defaultNote({ customer: "", title: "" }, {}),
    "Guten Tag,\nanbei unsere Entwürfe für Ihr Projekt. Sagen Sie uns gern, welcher Ihnen gefällt.\nViele Grüße");
});

test("positionsText for the approved proposal (§16.7)", () => {
  const v = { id: "dx6", pieces: [
    { id: "a1", label: "Esstisch", material_label: "Eiche natur geölt", handles_label: "grifflos (Push)", w: 180, h: 76, d: 90 },
    { id: "b2", label: "Sitzbank", material_label: "Eiche natur geölt", handles_label: "Griffleiste", w: 160, h: 45, d: 50 }] };
  const d = doc({ variants: [{ id: "x" }, v], versions: [{ n: 2, variant_ids: ["dx6", "x"] }],
                  approved: { version: 2, variant_id: "dx6", at: "2026-10-18T10:00:00" } });
  assert.equal(positionsText(d, "dx6", CAT), [
    "Esstisch, Eiche natur geölt, grifflos (Push), B 180 × H 76 × T 90 cm",
    "Sitzbank, Eiche natur geölt, Griffleiste, B 160 × H 45 × T 50 cm",
    "(Visualisierung Vorgang 2026-002, Version 2, Entwurf 1 – freigegeben am 18.10.2026)"].join("\n"));
});

test("customer response banner keeps hostile strings as plain text", () => {
  const xss = "<img src=x onerror=alert(1)>";
  const d = doc({ versions: [{ n: 1, variant_ids: ["v1"], status: "aenderung",
    response: { variant_id: "v1", action: "aenderung", name: "<b>Herr X</b>", comment: xss, at: "2026-10-18T10:00:00" } }] });
  const b = responseBanner(d);
  assert.equal(b.kind, "change");
  assert.equal(b.text, `<b>Herr X</b> wünscht eine Änderung: „${xss}“`);
  assert.equal(b.comment, xss);
  const a = responseBanner(doc({ versions: [{ n: 1, variant_ids: ["v0", "v1"] }],
    approved: { version: 1, variant_id: "v1", name: "Herr Beispiel", at: "2026-10-18T10:00:00", via: "telefon" } }));
  assert.equal(a.text, "Herr Beispiel hat Entwurf 2 freigegeben ✓ (18.10.2026)");
  assert.equal(responseBanner(doc()), null);
  // a withdrawn version's change request is not shown
  assert.equal(responseBanner(doc({ versions: [{ n: 1, variant_ids: ["v1"], status: "zurueckgezogen", response: { action: "aenderung" } }] })), null);
});

test("versions, people and sorting", () => {
  const team = [{ id: "anna", name: "Anna" }];
  assert.equal(versionLine({ n: 2, created: "2026-10-18T10:12:00", by: "anna" }, team), "Version 2 · gesendet 18.10.2026 von Anna");
  const ready = { n: 3, created: "2026-10-18T10:12:00", by: "anna", status: "offen", sent_at: null };
  assert.equal(versionLine(ready, team), "Version 3 · bereitgestellt 18.10.2026 von Anna, noch nicht gesendet");
  assert.deepEqual(versionBadge(ready), ["noch nicht gesendet", "warn"]);
  const out = { ...ready, sent_at: "2026-10-19T08:00:00", sent_by: "anna" };
  assert.equal(versionLine(out, team), "Version 3 · gesendet 19.10.2026 von Anna");
  assert.deepEqual(versionBadge(out), ["gesendet", "info"]);
  assert.equal(personName("dev:8f3a1c2e", team), "Gerät 8f3a");
  assert.equal(VSTATUS.aenderung[0], "Änderungswunsch");
  assert.equal(VSTATUS.zurueckgezogen[0], "zurückgezogen");
  const list = [{ id: "old", judge: judge(9, true) }, { id: "mid", judge: judge(4, false) }, { id: "new" }];
  assert.deepEqual(sortVariants(list, "neu").map(v => v.id), ["new", "mid", "old"]);
  assert.deepEqual(sortVariants(list, "gut").map(v => v.id), ["old", "mid", "new"]);
});

test("slider: Vorher on the left, renders show Skizze | Entwurf, edits use their parent", () => {
  const d = doc({ variants: [{ id: "p", image: "parent.png", kind: "room", room: "uploads/room.png", size: [1344, 1008] }] });
  assert.deepEqual(beforeOf(d.variants[0], d), { src: "uploads/room.png", labels: ["Vorher", "Nachher"] });
  assert.deepEqual(beforeOf({ id: "e", kind: "edit", from: "p", image: "e.png" }, d), { src: "parent.png", labels: ["Vorher", "Nachher"] });
  assert.deepEqual(beforeOf({ id: "r", kind: "render", sketch: "uploads/sk.png" }, d), { src: "uploads/sk.png", labels: ["Skizze", "Entwurf"] });
  assert.equal(beforeOf({ id: "f", kind: "free", image: "f.png" }, d), null);
  // old room variant without its photo: the current room only when the aspect fits
  assert.equal(beforeOf({ id: "o", kind: "room", size: [1344, 1008] }, d).src, "uploads/room.png");
  assert.equal(beforeOf({ id: "o", kind: "room", size: [1008, 1344] }, d), null);
  // an edit whose parent was deleted (in trash) still has its Vorher
  const t = doc({ trash: [{ id: "gone", image: "gone.png" }] });
  assert.equal(beforeOf({ id: "e", from: "gone", kind: "edit" }, t).src, "gone.png");
  assert.equal(aspectOf({ size: [1328, 1776] }), 1328 / 1776);
  assert.equal(clampPos(140), 100); assert.equal(clampPos("x"), 50);
});
