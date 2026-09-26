// Real-headless-Chrome check for the *second* instance of the htmx
// ambient-hx-target defect (dashboard_v2_home_check.mjs / TestHomeSelfPollTargets
// cover the first: self-polling widgets). See
// tests/test_dashboard_v2_boosted_nav_browser.py for why a browser is
// required here and why this is opt-in, and
// tests/test_web_routes.py::TestBoostedSwapDoesNotNestMain for the
// CI-running half of this regression's coverage.
//
// base.html's <body hx-boost="true" hx-target="main" hx-swap="..."> is the
// *default* hx-target/hx-swap for every boosted link, form, or bare
// hx-get/hx-post element that declares none of its own -- ordinary
// navigation (tile links, breadcrumbs, Home/Back) included, since none of
// those <a> tags carry an explicit hx-target either. Every level response
// is a full HTML document (every level extends base.html), so htmx's
// boosted-request handling parses the response, peels off the OOB #bar,
// and is left with the child level's own <main> element as the fragment to
// swap in. hx-swap="innerHTML" would insert that whole <main> element as a
// *child* of the page's live <main> instead of replacing it -- nesting
// <main><main>...</main></main> -- on every boosted navigation and on
// health_logs.html's Refresh button (a bare hx-get with no target of its
// own). No TestClient assertion on a response string can see this, because
// each individual response is always well-formed; only a real browser
// executing htmx's JS reproduces the client-side nesting.
//
// Invocation (from the pytest wrapper):
//   node dashboard_v2_boosted_nav_check.mjs <baseUrl> <sessionCookie> <deviceCookie>
// Prints one JSON line to stdout: {ok, steps: [...], error?}
// `ok` is true iff every step shows exactly one <main>, zero nested
// <main main>, and exactly one #bar.
import { spawn } from "node:child_process";
import { mkdtempSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function findChrome() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  ].filter(Boolean);
  for (const c of candidates) if (existsSync(c)) return c;
  throw new Error("Chrome/Chromium not found");
}

const [, , baseUrl, sessionCookie, deviceCookie] = process.argv;
if (!baseUrl || !sessionCookie || !deviceCookie) {
  console.log(JSON.stringify({ ok: false, error: "usage: <baseUrl> <sessionCookie> <deviceCookie>" }));
  process.exit(1);
}
const out = { ok: false, steps: [] };

async function main() {
  const bin = findChrome();
  const udd = mkdtempSync(join(tmpdir(), "cdp-boosted-nav-check-"));
  const port = 9400 + Math.floor(Math.random() * 1000);
  const proc = spawn(bin, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", `--remote-debugging-port=${port}`, `--user-data-dir=${udd}`,
    "about:blank",
  ], { stdio: "ignore" });

  try {
    let page = null;
    const deadline = Date.now() + 10000;
    while (Date.now() < deadline) {
      try {
        const r = await fetch(`http://127.0.0.1:${port}/json`);
        page = (await r.json()).find((t) => t.type === "page" && t.webSocketDebuggerUrl);
        if (page) break;
      } catch { /* debugger not up yet */ }
      await sleep(150);
    }
    if (!page) throw new Error("Chrome DevTools endpoint never came up");

    const ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => {
      ws.addEventListener("open", res, { once: true });
      ws.addEventListener("error", rej, { once: true });
    });
    let id = 0;
    const pending = new Map();
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { resolve, reject } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
    });
    const cmd = (method, params = {}) => new Promise((resolve, reject) => {
      const myId = ++id;
      pending.set(myId, { resolve, reject });
      ws.send(JSON.stringify({ id: myId, method, params }));
    });
    const evalJs = async (expression) => {
      const r = await cmd("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true });
      if (r.exceptionDetails) {
        throw new Error("eval threw: " + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
      }
      return r.result.value;
    };

    await cmd("Page.enable");
    await cmd("Runtime.enable");
    await cmd("Network.enable");
    const url = new URL(baseUrl);
    await cmd("Network.setCookie", { name: "vmc_session", value: sessionCookie, domain: url.hostname, path: "/" });
    await cmd("Network.setCookie", { name: "vmc_device", value: deviceCookie, domain: url.hostname, path: "/" });

    const snapshot = async (label) => {
      const s = {
        label,
        mains: await evalJs(`document.querySelectorAll('main').length`),
        nestedMain: await evalJs(`document.querySelectorAll('main main').length`),
        bars: await evalJs(`document.querySelectorAll('#bar').length`),
      };
      out.steps.push(s);
      return s;
    };

    // A real top-level navigation, exactly like a bookmark or reload.
    await cmd("Page.navigate", { url: `${baseUrl}/` });
    await sleep(1200);
    await snapshot("initial-home-load");

    // A boosted <a> click -- ordinary navigation, not a form and not a
    // self-polling widget. None of Home's tile links carry an explicit
    // hx-target; they rely entirely on <body>'s ambient default.
    await evalJs(`document.querySelector('a[href="/health"]').click()`);
    await sleep(600);
    await snapshot("after-click-health-tile");

    // base.html's own Home link (in the bar) -- same ambient mechanism.
    await evalJs(`document.querySelector('a[href="/"]').click()`);
    await sleep(600);
    await snapshot("after-click-home-link");

    // health_logs.html's Refresh button: a bare hx-get with no target of
    // its own, clicked twice to confirm it doesn't accumulate a fresh
    // nested <main> on every tap.
    await cmd("Page.navigate", { url: `${baseUrl}/health/logs` });
    await sleep(800);
    await snapshot("logs-initial-load");
    await evalJs(`document.querySelector('button[hx-get="/health/logs"]').click()`);
    await sleep(600);
    await snapshot("after-refresh-click-1");
    await evalJs(`document.querySelector('button[hx-get="/health/logs"]').click()`);
    await sleep(600);
    await snapshot("after-refresh-click-2");

    out.ok = out.steps.every((s) => s.mains === 1 && s.nestedMain === 0 && s.bars === 1);

    try { ws.close(); } catch { /* already closing */ }
  } finally {
    proc.kill();
  }
}

main()
  .then(() => console.log(JSON.stringify(out)))
  .catch((e) => {
    out.error = String((e && e.stack) || e);
    console.log(JSON.stringify(out));
    process.exitCode = 1;
  });
