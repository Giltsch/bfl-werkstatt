// Voice input for the Möbel step and the Assistent: one mic button that records with MediaRecorder where the browser
// allows it, and otherwise opens the phone's file chooser for audio (<input type=file accept=audio/*>). No `capture`:
// on Android Chrome audio+capture opens the video camera when no recorder app claims it.
// Browsers grant the microphone only in a secure context (https:// or localhost); the workshop runs on plain http in
// the LAN, so the file fallback is the normal case on phones there. Pure helpers are exported for tests/ws_system.test.mjs.
import { h, icon, toast, sheet } from "./ui.js";

export const MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4", "audio/aac"];
export const MIN_SECONDS = 0.6;

/** First container this browser can record (Safari: mp4, Chrome/Firefox: webm/ogg); "" = let the browser choose. */
export function pickMime(isTypeSupported) {
  if (typeof isTypeSupported !== "function") return "";
  for (const m of MIME_CANDIDATES) {
    try { if (isTypeSupported(m)) return m; } catch { /* old Safari throws on unknown types */ }
  }
  return "";
}

/** {mode: "record" | "file", reason: null | "insecure" | "unsupported"} for the given environment. */
export function micSupport({ secure, mediaDevices, recorder }) {
  if (!secure) return { mode: "file", reason: "insecure" };
  if (!mediaDevices || !recorder) return { mode: "file", reason: "unsupported" };
  return { mode: "record", reason: null };
}

export function currentSupport() {
  const w = typeof window !== "undefined" ? window : {};
  return micSupport({ secure: w.isSecureContext === true, mediaDevices: !!(w.navigator?.mediaDevices?.getUserMedia),
    recorder: typeof w.MediaRecorder === "function" });
}

export const isIOS = (ua = typeof navigator !== "undefined" ? navigator.userAgent : "") =>
  /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && typeof navigator !== "undefined" && navigator.maxTouchPoints > 1);

/** One short line under the mic button when the browser cannot record itself. */
export function micHint(reason, { ios = false } = {}) {
  if (reason === "insecure") return ios
    ? "Ohne sichere Verbindung (https) sperrt der Browser das Mikrofon. Tippen öffnet die Dateiauswahl: dort eine Aufnahme aus „Sprachmemos“ wählen – oder einfach tippen statt sprechen."
    : "Ohne sichere Verbindung (https) sperrt der Browser das Mikrofon. Am einfachsten: das Mikrofon der Handy-Tastatur zum Diktieren benutzen. Der Knopf hier öffnet die Auswahl für eine Sprachaufnahme.";
  if (reason === "unsupported") return "Dieser Browser kann nicht selbst aufnehmen. Tippen öffnet die Sprachaufnahme bzw. Dateiauswahl.";
  if (reason === "denied") return "Mikrofon nicht erlaubt. Im Browser neben der Adresse auf das Schloss bzw. ⓘ tippen und das Mikrofon erlauben – oder tippen statt sprechen.";
  return "";
}

/** German message for a getUserMedia / MediaRecorder failure. */
export function micError(e) {
  const n = e && e.name;
  if (n === "NotAllowedError" || n === "PermissionDeniedError" || n === "SecurityError") return micHint("denied");
  if (n === "NotFoundError" || n === "DevicesNotFoundError" || n === "OverconstrainedError") return "Kein Mikrofon gefunden.";
  if (n === "NotReadableError" || n === "TrackStartError" || n === "AbortError") return "Das Mikrofon wird gerade von einer anderen App benutzt.";
  return "Aufnahme nicht möglich – bitte tippen statt sprechen.";
}

export function extFor(mime) {
  const m = String(mime || "").toLowerCase();
  if (m.includes("webm")) return "webm";
  if (m.includes("ogg")) return "ogg";
  if (m.includes("mp4") || m.includes("aac") || m.includes("m4a")) return "m4a";
  if (m.includes("wav")) return "wav";
  if (m.includes("mpeg") || m.includes("mp3")) return "mp3";
  return "webm";
}

export function clock(s) {
  s = Math.max(0, Math.floor(Number(s) || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

function ensureCss() {
  if (typeof document === "undefined" || document.getElementById("ws-voice-css")) return;
  document.head.append(h("style", { id: "ws-voice-css" }, `
.vc{display:flex;flex-direction:column;gap:6px;min-width:0}
.vc-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.vc-mic{display:inline-flex;align-items:center;justify-content:center;gap:10px;min-height:56px;min-width:56px;padding:8px 20px 8px 14px;border-radius:999px;
 border:2px solid var(--accent);background:var(--accent);color:var(--accent-ink);font:inherit;font-weight:650;font-size:17px;cursor:pointer;touch-action:manipulation}
.vc-mic .icon{width:28px;height:28px}
.vc-mic.small{min-height:48px;min-width:48px;padding:0;width:48px;border-radius:50%}
.vc-mic.small .vc-lab{display:none}
.vc-mic[disabled]{opacity:.5;cursor:not-allowed}
.vc-mic.rec{background:var(--danger);border-color:var(--danger);color:#fff;animation:vcpulse 1.4s ease-in-out infinite}
.vc-mic.rec.small{width:auto;padding:0 14px;border-radius:999px}.vc-mic.rec.small .vc-lab{display:inline}
@keyframes vcpulse{50%{box-shadow:0 0 0 8px color-mix(in srgb,var(--danger) 25%,transparent)}}
.vc-hint{font-size:14px;color:var(--muted);margin:0}
.vc-hint.warn{color:var(--warn)}
`));
}

/**
 * The mic button. onAudio(blob, filename) is awaited (the button is disabled meanwhile); throw to show nothing extra.
 * opts: {label, small, maxSeconds, hint: true (show the fallback line), onRecording(bool)}
 * Returns {el, button, busy(bool), stop(), cancel(), mode}.
 */
export function voiceButton({ onAudio, label = "Sprechen", small = false, maxSeconds = 120, hint = true, onRecording } = {}) {
  ensureCss();
  const sup = currentSupport();
  const lab = h("span", { class: "vc-lab" }, label);
  const btn = h("button", { class: "vc-mic" + (small ? " small" : ""), type: "button", "aria-label": label, html: icon("mic") });
  btn.append(lab);
  const cancelBtn = h("button", { class: "btn ghost", type: "button", hidden: true }, "Abbrechen");
  const hintEl = h("p", { class: "vc-hint", role: "status", hidden: !(hint && sup.mode === "file") }, micHint(sup.reason, { ios: isIOS() }));
  const el = h("div", { class: "vc" }, h("div", { class: "vc-row" }, btn, cancelBtn), hintEl);
  let rec = null, stream = null, chunks = [], t0 = 0, timer = null, discard = false, busyFlag = false;

  const setRecording = on => {
    btn.classList.toggle("rec", on);
    cancelBtn.hidden = !on;
    btn.setAttribute("aria-label", on ? "Aufnahme beenden und senden" : label);
    lab.textContent = on ? `${clock(0)} · Fertig` : label;
    onRecording?.(on);
  };
  const release = () => {
    clearInterval(timer); timer = null;
    try { stream?.getTracks().forEach(t => t.stop()); } catch { /* gone */ }
    stream = null;
  };
  const deliver = async (blob, name) => {
    busyFlag = true; btn.disabled = true;
    try { await onAudio?.(blob, name); }
    catch (e) { if (e?.message) toast(e.message, { kind: "err" }); }
    finally { busyFlag = false; btn.disabled = false; }
  };

  async function start() {
    hintEl.classList.remove("warn");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 } });
    } catch (e) {
      hintEl.textContent = micError(e); hintEl.classList.add("warn"); hintEl.hidden = false;
      return;
    }
    const mime = pickMime(MediaRecorder.isTypeSupported?.bind(MediaRecorder));
    try { rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined); }
    catch { rec = new MediaRecorder(stream); }
    chunks = []; discard = false; t0 = Date.now();
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      const secs = (Date.now() - t0) / 1000, type = rec?.mimeType || mime || "audio/webm";
      release(); setRecording(false); rec = null;
      if (discard) return;
      if (secs < MIN_SECONDS || !chunks.length) { toast("Zu kurz – tippen, sprechen und zum Beenden nochmal tippen."); return; }
      deliver(new Blob(chunks, { type }), `aufnahme.${extFor(type)}`);
    };
    rec.start(250);
    setRecording(true);
    timer = setInterval(() => {
      const s = (Date.now() - t0) / 1000;
      lab.textContent = `${clock(s)} · Fertig`;
      if (s >= maxSeconds) stop();
    }, 250);
  }
  function stop() { if (rec && rec.state !== "inactive") rec.stop(); }
  function cancel() { discard = true; stop(); release(); setRecording(false); }

  function pickFile() {   // created inside the tap: iOS only opens pickers from a user gesture
    const inp = h("input", { type: "file", accept: "audio/*", style: "position:fixed;left:-1000px;top:0;opacity:0" });
    let done = false;
    const finish = f => { if (done) return; done = true; inp.remove(); if (f) deliver(f, f.name || "aufnahme"); };
    inp.addEventListener("change", () => finish(inp.files && inp.files[0] || null));
    inp.addEventListener("cancel", () => finish(null));
    document.body.append(inp);
    inp.click();
  }

  btn.addEventListener("click", () => {
    if (busyFlag) return;
    if (rec) return stop();
    if (sup.mode === "file") return pickFile();
    start();
  });
  cancelBtn.addEventListener("click", cancel);
  return { el, button: btn, mode: sup.mode, stop, cancel, busy(on) { busyFlag = !!on; btn.disabled = !!on; }, get recording() { return !!rec; } };
}

/** Explanation sheet behind „Warum kein Mikrofon?“. */
export function micHelp() {
  sheet({ title: "Mikrofon im Browser", body: h("div", {},
    h("p", {}, "Browser geben das Mikrofon nur über eine sichere Verbindung frei (Adresse beginnt mit https://) oder direkt am Werkstatt-Rechner. Die Werkstatt läuft im WLAN über http:// – deshalb:"),
    h("ul", {}, h("li", {}, "Am einfachsten: ins Textfeld tippen und das Mikrofon der Handy-Tastatur benutzen (Diktieren) – das geht sofort."),
      h("li", {}, "Android: Der Mikrofon-Knopf öffnet die Auswahl – dort „Rekorder“ bzw. eine vorhandene Sprachaufnahme wählen."),
      h("li", {}, "iPhone/iPad: Mit der App „Sprachmemos“ aufnehmen, dann beim Mikrofon-Knopf die Aufnahme als Datei wählen."),
),
    h("p", { class: "muted" }, "Eine sichere Verbindung im WLAN bräuchte ein Zertifikat; das richtet bei Bedarf der IT-Betreuer ein.")),
  actions: [{ label: "Verstanden", kind: "primary" }] });
}

/** POST /api/voice/transcribe → text ("" when nothing was heard). Throws ApiError with the server's German message. */
export async function transcribe(api, blob, name = "aufnahme.webm") {
  const fd = new FormData();
  fd.append("audio", blob, name);
  fd.append("lang", "de");
  const r = await api("POST", "/api/voice/transcribe", fd);
  return { text: String(r?.text || "").trim(), message: r?.message || "", seconds: r?.seconds };
}
