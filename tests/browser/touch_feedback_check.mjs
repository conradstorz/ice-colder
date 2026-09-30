// Real-headless-Chrome check for touch feedback: the CSS :active press
// state, htmx's `.htmx-request` busy indicator during a boosted GET, and
// the tap-sound `play()` call. Used by
// tests/test_touch_feedback_browser.py — see that file's docstring for
// why a browser is required here and why this is opt-in.
//
// TestClient-based tests only ever see the HTML *string* FastAPI returns.
// `:active` styling, `.htmx-request` timing during an in-flight boosted
// navigation, and whether HTMLMediaElement.play() was invoked are all
// client-side, browser-executed facts — only a real browser proves them.
//
// Invocation (from the pytest wrapper):
//   node touch_feedback_check.mjs <baseUrl> <sessionCookie> <deviceCookie>
// Prints one JSON line to stdout:
//   {ok, pressFilter, busyDuringFlight, busyAfterSwap, landedOnHealth, plays, error?}
// `ok` is true iff: pressFilter is not "none" (the :active brightness
// filter applied), the Health tile carries .htmx-request while its
// boosted GET is deliberately held in flight, no element other than #pill
// carries .htmx-request after the swap completes, the boosted navigation
// landed on /health, and the tap-sound play() spy was invoked at least
// once. #pill is excluded from the after-swap busy check because it is a
// known, pre-existing, unrelated defect (not introduced by this feature):
// partials/pill.html re-emits hx-get/hx-trigger="load, every 5s"/hx-target
// on every outerHTML swap of itself, so each swap's freshly inserted
// element immediately fires its own "load" trigger and starts a new
// request -- #pill is *always* mid-request-or-settle on a live page, even
// at baseline with no interaction at all. Confirmed by hand: right after
// the very first page load (before any click), #pill already carries
// "htmx-request"; a snapshot mid-cycle shows "htmx-request htmx-swapping
// htmx-added htmx-settling" together while its text already reads the
// resolved value ("OK"), i.e. it is perpetually re-triggering, not stuck.
// busyDuringFlight (read from the specific Health-tile anchor) is the
// positive control proving the class-presence check itself works; this
// after-swap check stays a real regression guard for every *other*
// element (the tile itself, any form) by excluding only the one element
// known to loop.
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
  const udd = mkdtempSync(join(tmpdir(), "cdp-touch-check-"));
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
    let pausedRequestId = null;
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.id && pending.has(m.id)) {
        const { resolve, reject } = pending.get(m.id);
        pending.delete(m.id);
        m.error ? reject(new Error(JSON.stringify(m.error))) : resolve(m.result);
      }
      if (m.method === "Fetch.requestPaused") {
        out.pausedRequestUrl = m.params.request.url;
        pausedRequestId = m.params.requestId;
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

    // A genuine top-level navigation.
    await cmd("Page.navigate", { url: `${baseUrl}/` });
    await sleep(2000); // let the load-triggered polls land and settle

    // 1. Spy on play() so we can prove the tap-sound handler fired.
    await evalJs(`
      window.__plays = 0;
      HTMLMediaElement.prototype.play = function () {
        window.__plays += 1;
        return Promise.resolve();
      };
    `);

    // 2. Locate the Health tile's centre point. The tile can start below
    // the (small headless) viewport fold, so scroll it into view first --
    // otherwise its rect centre falls outside window.innerHeight and
    // elementFromPoint (and CDP's own hit-testing) sees nothing there.
    await evalJs(`
      document.querySelector('main a[href="/health"]')
        .scrollIntoView({ block: "center" });
    `);
    const rect = await evalJs(`
      (() => {
        const el = document.querySelector('main a[href="/health"]');
        const r = el.getBoundingClientRect();
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
      })()
    `);
    out.tileRect = rect;

    // 3. Delay the boosted GET to /health so we can observe the busy state
    // while it is deliberately held in flight.
    await cmd("Fetch.enable", { patterns: [{ urlPattern: "*/health", requestStage: "Request" }] });

    // A mousemove before the press ensures headless Chrome has hit-tested
    // the target before the button-state events arrive.
    await cmd("Input.dispatchMouseEvent", {
      type: "mouseMoved", x: rect.x, y: rect.y,
    });
    await sleep(20);

    // 4. Press.
    await cmd("Input.dispatchMouseEvent", {
      type: "mousePressed", x: rect.x, y: rect.y, button: "left", buttons: 1, clickCount: 1,
    });
    await sleep(50);
    out.pressFilter = await evalJs(
      `getComputedStyle(document.querySelector('main a[href="/health"]')).filter`,
    );
    out.plays = await evalJs("window.__plays || 0");

    // 5. Release. Boosted navigation fires; wait for the paused fetch.
    await cmd("Input.dispatchMouseEvent", {
      type: "mouseReleased", x: rect.x, y: rect.y, button: "left", buttons: 0, clickCount: 1,
    });
    const pauseDeadline = Date.now() + 3000;
    while (!pausedRequestId && Date.now() < pauseDeadline) {
      await sleep(50);
    }
    out.busyDuringFlight = await evalJs(
      `document.querySelector('main a[href="/health"]').classList.contains("htmx-request")`,
    );

    // 6. Continue the held request and let the swap complete.
    if (pausedRequestId) {
      await cmd("Fetch.continueRequest", { requestId: pausedRequestId });
    }
    await sleep(1500);
    out.landedOnHealth = (await evalJs("location.pathname")) === "/health";
    // #pill excluded -- see the header comment: it re-emits its own
    // hx-trigger="load" on every self-swap and is perpetually
    // mid-request/settle on any live page, unrelated to this click. Every
    // other element is a real regression guard. Poll briefly rather than
    // trust a single fixed-delay snapshot, since settling is async.
    out.busyAfterSwap = await evalJs(`!!document.querySelector(".htmx-request:not(#pill)")`);
    const clearDeadline = Date.now() + 1000;
    while (out.busyAfterSwap && Date.now() < clearDeadline) {
      await sleep(100);
      out.busyAfterSwap = await evalJs(`!!document.querySelector(".htmx-request:not(#pill)")`);
    }
    out.debugBusyElements = await evalJs(
      `Array.from(document.querySelectorAll(".htmx-request")).map(e => e.id || e.className)`,
    );

    out.ok = out.pressFilter !== "none" && out.busyDuringFlight === true &&
      out.busyAfterSwap === false && out.landedOnHealth === true && out.plays >= 1;

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
