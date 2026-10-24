/* Shared helpers for the Book and Werkstatt pages: API calls, a job runner with a progress card, status pills. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const snap = v => Math.max(256, Math.min(4096, Math.round(v / 16) * 16));
const dims = (aspect, side) => { const h = Math.sqrt(side * side / aspect); return [snap(h * aspect), snap(h)]; };
const ORD = ["the first image", "the second image", "the third image", "the fourth image", "the fifth image",
  "the sixth image", "the seventh image", "the eighth image", "the ninth image", "the tenth image"];
const UPSCALE_PROMPT = "Reproduce the first image exactly: identical composition, characters, objects, colors and lighting, " +
  "rendered at higher resolution with crisp fine detail and clean textures.";
const img = rel => `/outputs/${rel}`;

function toast(msg, err) {
  let t = $("#toast");
  if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; document.body.appendChild(t); }
  t.textContent = msg; t.className = "toast" + (err ? " err" : ""); t.style.display = "block";
  clearTimeout(toast.t); toast.t = setTimeout(() => t.style.display = "none", err ? 7000 : 2600);
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  const r = await fetch(url, opt);
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch {}
    throw new Error(msg);
  }
  return r.json();
}

/* opts.maxSide: the server downscales so the long side fits (phone photos over WLAN). */
async function uploadFile(file, opts = {}) {
  const fd = new FormData(); fd.append("file", file);
  return api("POST", "/api/upload" + (opts.maxSide ? `?max_side=${opts.maxSide}` : ""), fd);
}

/* Jobs go through the studio's single GPU queue. runJob resolves with the finished job (results[].id → `${id}.png`).
   Every running job shows in one small card bottom right. */
const Jobs = {
  active: new Map(),
  render() {
    let el = $("#jobbar");
    if (!this.active.size) { if (el) el.hidden = true; return; }
    if (!el) { el = document.createElement("div"); el.id = "jobbar"; el.className = "jobbar"; document.body.appendChild(el); }
    el.hidden = false;
    const all = [...this.active.values()];
    const run = all.find(j => j.state === "running") || all[0];
    const label = run.state === "queued" ? "Waiting in queue…" : run.state === "done" ? run.judge_stage || "Prüfung…" : run.stage || "Working…";
    const pct = run.steps ? 100 * run.step / run.steps : null;
    const secs = run.started ? `${Math.round(Date.now() / 1000 - run.started)}s` : "";
    el.innerHTML = `<div class="t"><span>${esc(run.label ? run.label + " · " : "")}${esc(label)}</span><span>${run.steps ? `${run.step}/${run.steps}` : secs}</span></div>
      <div class="bar ${pct == null ? "indet" : ""}"><i style="width:${pct ?? 0}%"></i></div>
      ${all.length > 1 ? `<div class="q">${all.length - 1} more waiting</div>` : ""}`;
  },
};

async function runJob(params, refs = [], label = "") {
  const fd = new FormData();
  fd.append("params", JSON.stringify(params));
  for (const [i, r] of refs.entries()) {
    const blob = r instanceof Blob ? r : await (await fetch(img(r))).blob();
    fd.append("refs", blob, `ref${i}.png`);
  }
  return trackJob(await api("POST", "/api/jobs", fd), label);
}

/* Polls a queued job until it is done and (if graded) judged; onUpdate(job) after every poll. Transient network errors
   are retried; a job the server no longer knows (restart) rejects with err.gone = true. */
async function trackJob(job, label = "", onUpdate) {
  const key = job.id;
  let j = job;
  Jobs.active.set(key, { ...j, label }); Jobs.render();
  try {
    while (true) {
      await new Promise(r => setTimeout(r, 900));
      let r;
      try { r = await fetch(`/api/jobs/${key}`); } catch { continue; }
      if (r.status === 404) throw Object.assign(new Error("Job no longer exists (server restarted?)"), { gone: true });
      if (!r.ok) continue;
      try { j = await r.json(); } catch { continue; }
      Jobs.active.set(key, { ...j, label }); Jobs.render();
      if (onUpdate) try { onUpdate(j); } catch (e) { console.error(e); }
      if (j.state === "done" && [undefined, null, "off", "done"].includes(j.judge_state)) return j;
      if (j.state === "error") throw new Error(j.error || "Generation failed");
      if (j.state === "cancelled") throw new Error("Cancelled");
    }
  } finally { Jobs.active.delete(key); Jobs.render(); }
}
const resultPaths = job => job.results.map(r => `${r.id}.png`);

function imageSize(src) {
  return new Promise((ok, bad) => { const i = new Image(); i.onload = () => ok([i.naturalWidth, i.naturalHeight]); i.onerror = bad; i.src = src; });
}

/* onStatus (optional) gets the /api/status JSON after each poll, or null when the server is offline. */
async function pollStatus(onStatus) {
  const el = $("#status");
  let s = null;
  try {
    s = await api("GET", "/api/status");
    const pill = (k, label) => `<span class="pill ${s.loaded[k] ? "on" : ""}" title="${s.loaded[k] ? "loaded" : "loads on first use"}">${label}</span>`;
    el.innerHTML = (s.engine !== "idle" ? `<span class="pill">${esc(s.engine)}</span>` : "") + pill("distilled", "Distilled") + pill("pup", "Upsampler")
      + (s.queue ? `<span class="pill">${s.queue} in queue</span>` : "") + `<span class="pill">${s.mem_free_gb} GB free</span>`;
  } catch { s = null; el.innerHTML = `<span class="pill" style="color:var(--danger)">server offline</span>`; }
  if (typeof onStatus === "function") try { onStatus(s); } catch (e) { console.error(e); }
  setTimeout(() => pollStatus(onStatus), 5000);
}

function lightbox(src) {
  let lb = $("#lightbox");
  if (!lb) { lb = document.createElement("div"); lb.id = "lightbox"; lb.className = "lightbox"; lb.innerHTML = "<img alt=''>"; lb.onclick = () => lb.classList.remove("open"); document.body.appendChild(lb); }
  lb.firstChild.src = src; lb.classList.add("open");
}
document.addEventListener("keydown", e => { if (e.key === "Escape") { $("#lightbox")?.classList.remove("open"); $$(".modal.open").forEach(m => m.classList.remove("open")); } });

/* Calls fn at most once per `ms` after the last call. */
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

function dropZone(el, onFiles) {
  el.addEventListener("dragover", e => { e.preventDefault(); el.classList.add("over"); });
  el.addEventListener("dragleave", () => el.classList.remove("over"));
  el.addEventListener("drop", e => { e.preventDefault(); e.stopPropagation(); el.classList.remove("over"); const f = [...e.dataTransfer.files].filter(f => f.type.startsWith("image/")); if (f.length) onFiles(f); });
  el.addEventListener("click", () => { const i = document.createElement("input"); i.type = "file"; i.accept = "image/*"; i.onchange = () => i.files.length && onFiles([...i.files]); i.click(); });
}
