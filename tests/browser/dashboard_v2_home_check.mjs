// Real-headless-Chrome check for the Dashboard v2 Home landing page (spec
// §1.1). Used by tests/test_dashboard_v2_home_browser.py — see that file's
// docstring for why a browser is required here and why this is opt-in.
//
// TestClient-based tests only ever see the HTML *string* FastAPI returns.
// The bug this guards against was entirely client-side: the served HTML
// was always well-formed (one <main>, one #pill), but htmx 1.9.10 resolves
// an element's default swap target by walking up the DOM for an inherited
// hx-target when the element declares none of its own — and base.html's
// <body hx-boost="true" hx-target="main" ...> means any self-polling
// element that omits its own hx-target inherits "main". #status-panel,
// #kpi-panel and #pill (base.html's placeholder) all fire an
// hx-trigger="load" request the instant the page loads (the swapped-in
// partials/pill.html fragment then re-polls on "every 5s" alone); without an
// explicit hx-target="this" on each, their responses land on the page's
// <main> instead of themselves — the first to land (an innerHTML swap)
// wipes out main's real content, and the next (the pill's outerHTML swap)
// destroys <main> outright, replacing it with a bare <span>. No
// TestClient assertion on the response body can see this, because the
// response body was never wrong — only a real browser executing htmx's
// JS reproduces it, which is what this script does.
//
// Invocation (from the pytest wrapper):
//   node dashboard_v2_home_check.mjs <baseUrl> <sessionCookie> <deviceCookie>
// Prints one JSON line to stdout:
//   {ok, mains, pills, tileAnchors, pillTextSamples, pillResolved, error?}
// `ok` is true iff: exactly one <main>, exactly one #pill, at least one
// tile anchor inside <main>, and the pill's text moves off its "…"
// placeholder within ~6s of polling (proving /pill's periodic re-poll
// still finds a live target after the first swap, per spec's `every 5s`).
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
const out = { ok: false };

async function main() {
  const bin = findChrome();
  const udd = mkdtempSync(join(tmpdir(), "cdp-home-check-"));
  const port = 9200 + Math.floor(Math.random() * 1000);
  // --no-sandbox / --disable-dev-shm-usage: on GitHub's hosted Linux runners
  // Chrome's own sandbox generally cannot start (no privileged setuid helper,
  // restricted user namespaces) and exits before the DevTools port opens;
  // /dev/shm on those runners is also small enough to crash Chrome on some
  // pages. Both flags are harmless on a developer machine, so they stay in
  // the one shared launch list rather than being conditioned on CI.
  const proc = spawn(bin, [
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
    "--hide-scrollbars", "--no-sandbox", "--disable-dev-shm-usage",
    `--remote-debugging-port=${port}`, `--user-data-dir=${udd}`,
    "about:blank",
  ], { stdio: ["ignore", "ignore", "pipe"] });
  let chromeStderr = "";
  proc.stderr.on("data", (d) => { chromeStderr += d.toString(); });

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
    if (!page) {
      throw new Error(
        "Chrome DevTools endpoint never came up" +
        (chromeStderr.trim() ? `; Chrome stderr:\n${chromeStderr.trim()}` : " (Chrome produced no stderr)"),
      );
    }

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

    // A genuine top-level navigation — exactly what a bookmark, a reload,
    // or (per the original bug report) an HX-Redirect's location.href
    // assignment produces.
    await cmd("Page.navigate", { url: `${baseUrl}/` });
    await sleep(2000); // let the three "load"-triggered polls land and settle

    out.mains = await evalJs(`document.querySelectorAll('main').length`);
    out.pills = await evalJs(`document.querySelectorAll('#pill').length`);
    out.tileAnchors = await evalJs(`document.querySelectorAll("main a[href]").length`);

    const samples = [];
    for (let i = 0; i < 5; i++) {
      samples.push(
        await evalJs(`(() => { const p = document.querySelector('#pill'); return p ? p.textContent.trim() : 'MISSING'; })()`),
      );
      await sleep(1200);
    }
    out.pillTextSamples = samples;
    out.pillResolved = samples.some((s) => s !== "…" && s !== "MISSING");
    // Re-sample after ~8s: the swapped-in /pill fragment (partials/pill.html)
    // carries only "every 5s" — no "load" — so its first *own* self-swap
    // happens ~5s after the placeholder's load-triggered fetch, well after
    // the 2s snapshot above. Only this late read proves the fragment's
    // hx-target="this" (not just the placeholder's) keeps <main> intact.
    out.mainsAfterFragmentPoll = await evalJs(`document.querySelectorAll('main').length`);
    out.pillsAfterFragmentPoll = await evalJs(`document.querySelectorAll('#pill').length`);
    out.ok = out.mains === 1 && out.pills === 1 && out.tileAnchors > 0 && out.pillResolved
      && out.mainsAfterFragmentPoll === 1 && out.pillsAfterFragmentPoll === 1;

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
