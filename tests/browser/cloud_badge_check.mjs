// Real-headless-Chrome check that the cloud portal's context badge never
// stacks on boosted navigation: it lives inside the out-of-band #bar, so each
// swap replaces it. It reads "Cloud" on /fleet and the machine label on machine
// pages, and the old bottom #remote-banner must not exist anywhere.
// Steps: /fleet -> select -> / -> /health -> /health/faults -> /.
// Invocation: node cloud_badge_check.mjs <baseUrl> <session cookie name=value> [ignored]
// Prints one JSON line {ok, steps, error?}.
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
if (!baseUrl) {
  console.log(JSON.stringify({ ok: false, error: "usage: <baseUrl> <sessionCookie> <deviceCookie>" }));
  process.exit(1);
}
const out = { ok: false, steps: [] };

async function main() {
  const bin = findChrome();
  const udd = mkdtempSync(join(tmpdir(), "cdp-boosted-nav-check-"));
  const port = 9400 + Math.floor(Math.random() * 1000);
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

    // Bounded poll: re-evaluate a page-side JS predicate every 50 ms until it
    // is truthy. A fixed sleep proves nothing about whether a navigation or
    // htmx swap actually happened (a slow swap would let the snapshot read the
    // OLD page and pass vacuously), so every post-action wait goes through
    // this and fails loudly, naming what never happened, on timeout.
    const waitFor = async (label, predicateJs, timeoutMs = 8000) => {
      const end = Date.now() + timeoutMs;
      while (Date.now() < end) {
        if (await evalJs(`Boolean(${predicateJs})`)) return;
        await sleep(50);
      }
      throw new Error(`timed out after ${timeoutMs}ms waiting for: ${label} (predicate: ${predicateJs})`);
    };
    // Proves a navigation landed on `path` and its document finished loading.
    const loadedAt = (path) =>
      `location.pathname === ${JSON.stringify(path)} && document.readyState === 'complete'`;
    // htmx adds .htmx-request while a request is in flight, and marks swapped-in
    // content .htmx-added / .htmx-settling until its settle phase. That phase
    // is when htmx processes the NEW nodes' hx-* attributes (settle tasks run
    // ~20 ms after the swap), so a click on a freshly swapped-in button before
    // then hits a button with no handler and sends no request. "Idle" therefore
    // means no request in flight AND nothing mid-swap/settle.
    const IDLE = `!document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')`;

    await cmd("Page.enable");
    await cmd("Runtime.enable");
    await cmd("Network.enable");

    const snapshot = async (label) => {
      const s = {
        label,
        badges: await evalJs(`document.querySelectorAll('#context-badge').length`),
        badgesInBar: await evalJs(`document.querySelectorAll('#bar #context-badge').length`),
        badgeText: await evalJs(`(document.querySelector('#context-badge')?.textContent || '').trim()`),
        oldBanners: await evalJs(`document.querySelectorAll('#remote-banner').length`),
        mains: await evalJs(`document.querySelectorAll('main').length`),
        bars: await evalJs(`document.querySelectorAll('#bar').length`),
      };
      out.steps.push(s);
      return s;
    };

    // The portal needs a signed-in account: the harness passes the signed
    // session cookie as "name=value" (a lone "-" means none).
    if (sessionCookie && sessionCookie !== "-") {
      const eq = sessionCookie.indexOf("=");
      await cmd("Network.setCookie", {
        name: sessionCookie.slice(0, eq), value: sessionCookie.slice(eq + 1), url: baseUrl,
      });
    }

    // /fleet has no selected machine: the badge reads "Cloud".
    await cmd("Page.navigate", { url: `${baseUrl}/fleet` });
    await waitFor("initial load of /fleet", `${loadedAt("/fleet")} && ${IDLE} && document.querySelector('form[action^="/fleet/select/"] button')`);
    await snapshot("fleet");

    // Boosted form POST -> 303 -> / : the badge must appear exactly once, in the bar, with the machine label.
    await evalJs(`document.querySelector('form[action^="/fleet/select/"] button').click()`);
    await waitFor("select swapped to /", `${loadedAt("/")} && ${IDLE} && document.querySelector('a[href="/health"]')`);
    await snapshot("after-select");

    await evalJs(`document.querySelector('a[href="/health"]').click()`);
    await waitFor("boosted swap to /health", `${loadedAt("/health")} && ${IDLE} && document.querySelector('a[href="/health/faults"]')`);
    await snapshot("after-health");

    await evalJs(`document.querySelector('a[href="/health/faults"]').click()`);
    await waitFor("boosted swap to /health/faults", `${loadedAt("/health/faults")} && ${IDLE}`);
    await snapshot("after-faults");

    await evalJs(`document.querySelector('a[href="/"]').click()`);
    await waitFor("boosted swap back to /", `${loadedAt("/")} && ${IDLE} && document.querySelector('a[href="/health"]')`);
    await snapshot("after-home");

    out.ok = out.steps.every(
      (s) => s.mains === 1 && s.bars === 1 && s.badges === 1 && s.badgesInBar === 1 &&
        s.oldBanners === 0 && s.badgeText === (s.label === "fleet" ? "Cloud" : "m-0042"),
    );

    try { ws.close(); } catch { /* already closing */ }
  } finally {
    proc.kill();
  }
}

main()
  .then(() => {
    console.log(JSON.stringify(out));
    // Non-zero on a failed check too, not only on a thrown error, so a
    // wrapper that looks only at the exit status cannot mistake it for a pass.
    if (!out.ok) process.exitCode = 1;
  })
  .catch((e) => {
    out.error = String((e && e.stack) || e);
    console.log(JSON.stringify(out));
    process.exitCode = 1;
  });
