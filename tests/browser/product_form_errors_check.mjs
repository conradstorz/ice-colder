// Real-headless-Chrome check that a rejected product form shows its error.
// Used by tests/edge/web/test_product_form_errors_browser.py. The product
// form posts with hx-post and the route answers 400 with the form re-rendered
// plus <p role="alert">. htmx 1.9 does not swap 4xx responses by default, so
// without the htmx:beforeSwap rule in ice_ui's feedback.js the error is never
// shown -- invisible to TestClient, which only sees the response body.
//
// Invocation:
//   node product_form_errors_check.mjs <baseUrl> <sessionCookie> <deviceCookie>
// Prints one JSON line: {ok, alertBeforeSubmit, alertText, alertVisible, nameKept, error?}
// `ok` is true iff, after submitting an over-long name, a visible
// <p role="alert"> mentioning the name limit appears and the form still
// holds the submitted name (and no alert existed before the submit).
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
const LONG_NAME = "N".repeat(150);
const FORM = 'form[hx-post="/products/new"]';

async function main() {
  const bin = findChrome();
  const udd = mkdtempSync(join(tmpdir(), "cdp-product-form-check-"));
  const port = 9400 + Math.floor(Math.random() * 500);
  // --no-sandbox / --disable-dev-shm-usage: needed on GitHub's hosted Linux
  // runners, harmless on a developer machine (see the other checkers).
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
    const waitFor = async (label, predicateJs, timeoutMs = 8000) => {
      const end = Date.now() + timeoutMs;
      while (Date.now() < end) {
        if (await evalJs(`Boolean(${predicateJs})`)) return;
        await sleep(50);
      }
      throw new Error(`timed out after ${timeoutMs}ms waiting for: ${label} (predicate: ${predicateJs})`);
    };

    await cmd("Page.enable");
    await cmd("Runtime.enable");
    await cmd("Network.enable");
    const url = new URL(baseUrl);
    await cmd("Network.setCookie", { name: "vmc_session", value: sessionCookie, domain: url.hostname, path: "/" });
    await cmd("Network.setCookie", { name: "vmc_device", value: deviceCookie, domain: url.hostname, path: "/" });

    await cmd("Page.navigate", { url: `${baseUrl}/products/new` });
    // Idle: no request in flight and nothing mid-swap/settle, so htmx has
    // processed the form's hx-post before the submit click.
    await waitFor("form loaded and htmx idle",
      `location.pathname === "/products/new" && document.readyState === "complete" && ` +
      `window.htmx && document.querySelector('${FORM} button[type=submit]') && ` +
      `!document.querySelector('.htmx-request, .htmx-swapping, .htmx-settling, .htmx-added')`, 10000);
    // Guard against a vacuous pass: no alert before the submit.
    out.alertBeforeSubmit = await evalJs(`!!document.querySelector('${FORM} p[role="alert"]')`);

    // maxlength stops typing, not a programmatic set; a stale or hostile
    // client can always post a longer value, which is what the server rejects.
    await evalJs(`document.getElementById("name").value = ${JSON.stringify(LONG_NAME)}`);
    await evalJs(`document.querySelector('${FORM} button[type=submit]').click()`);

    await waitFor("error alert swapped in", `document.querySelector('${FORM} p[role="alert"]')`, 8000);
    out.alertText = await evalJs(`document.querySelector('${FORM} p[role="alert"]').textContent.trim()`);
    out.alertVisible = await evalJs(`(() => {
      const el = document.querySelector('${FORM} p[role="alert"]');
      const r = el.getBoundingClientRect();
      return getComputedStyle(el).display !== "none" && r.width > 0 && r.height > 0;
    })()`);
    out.nameKept = await evalJs(`document.getElementById("name").value === ${JSON.stringify(LONG_NAME)}`);

    out.ok = out.alertBeforeSubmit === false && out.alertVisible === true &&
      out.alertText.includes("at most") && out.nameKept === true;

    try { ws.close(); } catch { /* already closing */ }
  } finally {
    proc.kill();
  }
}

main()
  .then(() => {
    console.log(JSON.stringify(out));
    if (!out.ok) process.exitCode = 1;
  })
  .catch((e) => {
    out.error = String((e && e.stack) || e);
    console.log(JSON.stringify(out));
    process.exitCode = 1;
  });
