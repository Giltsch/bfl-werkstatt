// Screenshot + layout checks for the Werkstatt UI (spec §10.7). A tiny static server serves the tree (pages and
// /static/**); the mock (mock_api.mjs) answers /api, /outputs and /k inside the browser context.
//   const {context, base, close} = await launch({viewport: VIEWPORTS.phone, insecure: false});
//   await installMock(context, {scenario: "busy"}); const page = await context.newPage(); await page.goto(base + "/werkstatt");
//   const violations = await checkPage(page, "kit-phone-dashboard", {primary: "#ws-actions .btn.primary"});
import {createServer} from "node:http";
import {createRequire} from "node:module";
import {readFile, stat, mkdir} from "node:fs/promises";
import {join, extname, normalize} from "node:path";
import {ROOT} from "./mock_api.mjs";

export const SCRATCH = process.env.WS_SCRATCH || "/tmp/claude-1000/-home-peter-behninger-Dokumente-Projekte-bfl/341b84a3-3a86-44cb-9161-4eed0a3f35fc/scratchpad";
export const SHOTS = process.env.WS_SHOTS || join(SCRATCH, "shots-v3");
export const CHROME = process.env.WS_CHROME || "/home/peter_behninger/.cache/ms-playwright/chromium-1248/chrome-linux-arm64/chrome";
const {chromium} = createRequire(join(SCRATCH, "pw") + "/")("playwright-core");

export const VIEWPORTS = {
  phone: {name: "phone", width: 390, height: 844, touch: true, dpr: 3},
  phoneLandscape: {name: "phone-land", width: 844, height: 390, touch: true, dpr: 3},
  tablet: {name: "tablet", width: 820, height: 1180, touch: true, dpr: 2},
  tabletLandscape: {name: "tablet-land", width: 1180, height: 820, touch: true, dpr: 2},
  desktop: {name: "desktop", width: 1440, height: 900, touch: false, dpr: 1},
};

const TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg"};
let server = null, port = 0;

async function startServer() {
  if (server) return port;
  server = createServer(async (req, res) => {
    const u = new URL(req.url, "http://x");
    let p = decodeURIComponent(u.pathname);
    if (p === "/" ) { res.writeHead(302, {Location: "/werkstatt"}); return res.end(); }
    if (p === "/werkstatt") p = "/static/ws/index.html";
    if (!p.startsWith("/static/")) { res.writeHead(404); return res.end("not found"); }
    const f = normalize(join(ROOT, p));
    if (!f.startsWith(join(ROOT, "static"))) { res.writeHead(403); return res.end(); }
    try {
      if (!(await stat(f)).isFile()) throw new Error("dir");
      res.writeHead(200, {"Content-Type": TYPES[extname(f)] || "application/octet-stream", "Cache-Control": "no-store"});
      res.end(await readFile(f));
    } catch { res.writeHead(404); res.end("not found"); }
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  port = server.address().port;
  return port;
}

export async function stopServer() { if (server) { await new Promise(r => server.close(r)); server = null; } }

// insecure: page origin http://werkstatt.lan:<port> (not a secure context, like a phone on http://192.168.x.x:7860);
// navigator.share / navigator.clipboard / Notification are stubbed to record and throw, so checkSecureFallbacks() can
// prove the UI never calls them there.
export async function launch({viewport = VIEWPORTS.desktop, touch, dpr, insecure = false, colorScheme = "light", args = []} = {}) {
  const p = await startServer();
  const browser = await chromium.launch({executablePath: CHROME, headless: true,
    args: [...(insecure ? ["--host-resolver-rules=MAP werkstatt.lan 127.0.0.1"] : []), ...args]});
  const hasTouch = touch ?? viewport.touch ?? false;
  const context = await browser.newContext({viewport: {width: viewport.width, height: viewport.height}, deviceScaleFactor: dpr ?? viewport.dpr ?? 1,
    hasTouch, isMobile: hasTouch && viewport.width < 1100, colorScheme, locale: "de-DE", timezoneId: "Europe/Berlin"});
  if (insecure) await context.addInitScript(() => {
    window.__secureCalls = [];
    const trap = name => function () { window.__secureCalls.push(name); throw new Error(name + " blocked (insecure origin)"); };
    try { Object.defineProperty(navigator, "share", {value: trap("navigator.share"), configurable: true}); } catch { /* ignore */ }
    try { Object.defineProperty(navigator, "canShare", {value: trap("navigator.canShare"), configurable: true}); } catch { /* ignore */ }
    try { Object.defineProperty(navigator, "clipboard", {value: {writeText: trap("clipboard.writeText"), write: trap("clipboard.write")}, configurable: true}); } catch { /* ignore */ }
    window.Notification = trap("Notification");
    window.Notification.requestPermission = trap("Notification.requestPermission");
  });
  const base = insecure ? `http://werkstatt.lan:${p}` : `http://localhost:${p}`;
  return {browser, context, base, async close() { await browser.close(); }};
}

// Records every request whose origin differs from the page's (the app must not load anything third-party).
export function trackRequests(page, base) {
  const foreign = [];
  const origin = new URL(base).origin;
  page.on("request", r => { const u = r.url(); if (!u.startsWith("data:") && !u.startsWith("blob:") && new URL(u).origin !== origin) foreign.push(u); });
  return foreign;
}

export async function checkPage(page, name, {primary = null, minTap = 44, minFont = 14, shot = true, fullPage = false} = {}) {
  await page.evaluate(() => Promise.race([new Promise(r => setTimeout(r, 1500)), Promise.all([document.fonts && document.fonts.ready,
    ...document.getAnimations().filter(a => a.effect && a.effect.getTiming().iterations !== Infinity).map(a => a.finished.catch(() => {}))])]));
  if (shot) {
    await mkdir(SHOTS, {recursive: true});
    await page.screenshot({path: join(SHOTS, `${name}.png`), fullPage});
  }
  const v = await page.evaluate(({primary, minTap, minFont}) => {
    const out = [];
    const desc = el => {
      const t = (el.getAttribute("aria-label") || el.textContent || el.value || "").trim().replace(/\s+/g, " ").slice(0, 40);
      return `${el.tagName.toLowerCase()}${el.id ? "#" + el.id : ""}${el.className && typeof el.className === "string" ? "." + el.className.trim().split(/\s+/).join(".") : ""} „${t}“`;
    };
    const visible = el => {
      if (el.checkVisibility && !el.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    };
    if (document.documentElement.scrollWidth > innerWidth) out.push(`horizontal scroll: scrollWidth ${document.documentElement.scrollWidth} > ${innerWidth}`);
    for (const el of document.querySelectorAll('button, a.btn, input, select, textarea, [role=button], .chip, .tile')) {
      if (el.type === "hidden" || el.type === "file" || !visible(el) || el.closest("[data-check-skip]")) continue;
      // a checkbox/radio inside its <label> is tapped through the label, so the label's box is the target
      const lab = el.tagName === "INPUT" && /^(checkbox|radio)$/.test(el.type) && el.closest("label");
      const r = (lab || el).getBoundingClientRect();
      if (r.width < minTap - 0.5 || r.height < minTap - 0.5) out.push(`small target ${Math.round(r.width)}×${Math.round(r.height)}: ${desc(el)}`);
    }
    for (const el of document.querySelectorAll("p, li, label, .field input")) {
      if (!visible(el) || !el.textContent.trim() && el.tagName !== "INPUT") continue;
      const fs = parseFloat(getComputedStyle(el).fontSize);
      if (fs < minFont - 0.01) out.push(`small text ${fs}px: ${desc(el)}`);
    }
    if (primary) {
      const el = typeof primary === "string" ? document.querySelector(primary) : null;
      if (!el) out.push(`primary ${primary} not found`);
      else {
        const r = el.getBoundingClientRect();
        if (r.top < 0 || r.left < 0 || r.bottom > innerHeight + 0.5 || r.right > innerWidth + 0.5) out.push(`primary outside viewport: ${desc(el)} ${JSON.stringify([r.left, r.top, r.right, r.bottom].map(Math.round))}`);
        else {
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          if (!hit || !(hit === el || el.contains(hit))) out.push(`primary covered by ${hit ? desc(hit) : "nothing"}`);
        }
      }
    }
    return out;
  }, {primary, minTap, minFont});
  return v.map(x => `${name}: ${x}`);
}

export async function checkSecureFallbacks(page) {
  const r = await page.evaluate(() => ({secure: window.isSecureContext, calls: window.__secureCalls || null}));
  const out = [];
  if (r.secure !== false) out.push("isSecureContext is not false on the insecure origin");
  if (r.calls === null) out.push("secure-API traps missing (launch with insecure: true)");
  else if (r.calls.length) out.push(`secure-only APIs called on HTTP: ${r.calls.join(", ")}`);
  return out;
}
