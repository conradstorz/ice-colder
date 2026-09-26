// Real-headless-Chrome check for Copilot review comment 4113241348 on PR 20
// (web_interface/templates/users_codes.html:25): the owner's PIN input sits
// inside the same <form> as the confirm/cancel hx-get buttons, so htmx
// serializes that form for the GET requests too, putting the PIN in the
// query string of /users/codes/regenerate/confirm (both the first tap and
// Cancel). This can only be proven by a real browser executing htmx's own
// request-building JS against the live DOM -- see
// tests/test_dashboard_v2_home_browser.py's docstring for why the project
// uses this pattern for htmx runtime behaviour that no TestClient-based
// string assertion can see.
//
// Invocation:
//   node users_codes_pin_check.mjs <baseUrl> <sessionCookie> <deviceCookie>
// Prints one JSON line to stdout:
//   {ok, pinValue, getRequests: [{url, hasPinInQuery}], postRequest: {url, hasPinInQuery, postData}, error?}
// `ok` is true iff: the first-tap GET and the Cancel GET both reach the
// server WITHOUT the pin in their query string, and the final Confirm POST
// still carries the pin (in its body, not the URL) -- i.e. the fix keeps
// the PIN out of every GET and still delivers it on the POST that actually
// regenerates the codes.
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
const PIN = "1379";

async function main() {
  const bin = findChrome();
  const udd = mkdtempSync(join(tmpdir(), "cdp-pin-check-"));
  const port = 9300 + Math.floor(Math.random() * 1000);
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
    const requests = []; // {url, method, postData}
    ws.addEventListener("message", (ev) => {
      const m = JSON.parse(ev.data);
      if (m.method === "Network.requestWillBeSent") {
        const req = m.params.request;
        requests.push({ url: req.url, method: req.method, postData: req.postData || null });
        return;
      }
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
    await cmd("Network.enable", { maxPostDataSize: 65536 });
    const url = new URL(baseUrl);
    await cmd("Network.setCookie", { name: "vmc_session", value: sessionCookie, domain: url.hostname, path: "/" });
    await cmd("Network.setCookie", { name: "vmc_device", value: deviceCookie, domain: url.hostname, path: "/" });

    await cmd("Page.navigate", { url: `${baseUrl}/users/codes` });
    await sleep(800);

    // Fill in the owner's PIN exactly like a real operator would.
    await evalJs(`(() => {
      const input = document.querySelector('input[name="pin"]');
      if (!input) throw new Error('pin input not found');
      input.value = ${JSON.stringify(PIN)};
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);

    requests.length = 0; // isolate what follows from page-load requests
    await evalJs(`document.querySelector('button[hx-get="/users/codes/regenerate/confirm"]').click()`);
    await sleep(500);
    const firstTapRequests = requests.filter((r) => r.url.includes("/users/codes/regenerate/confirm"));

    requests.length = 0;
    await evalJs(`(() => {
      const btns = [...document.querySelectorAll('button[hx-get="/users/codes/regenerate/confirm"]')];
      const cancel = btns.find((b) => b.getAttribute('hx-vals'));
      if (!cancel) throw new Error('cancel button not found');
      cancel.click();
    })()`);
    await sleep(500);
    const cancelRequests = requests.filter((r) => r.url.includes("/users/codes/regenerate/confirm"));

    // Re-open the confirm state and this time actually confirm, proving the
    // PIN still reaches the POST that performs the regeneration.
    await evalJs(`document.querySelector('button[hx-get="/users/codes/regenerate/confirm"]').click()`);
    await sleep(500);
    requests.length = 0;
    await evalJs(`document.querySelector('button[hx-post="/users/codes/regenerate"]').click()`);
    await sleep(500);
    const postRequests = requests.filter((r) => r.url.includes("/users/codes/regenerate") && r.method === "POST");

    out.pinValue = PIN;
    out.getRequests = [...firstTapRequests, ...cancelRequests].map((r) => ({
      url: r.url,
      hasPinInQuery: new URL(r.url).searchParams.has("pin"),
    }));
    const post = postRequests[0];
    out.postRequest = post
      ? {
          url: post.url,
          hasPinInQuery: new URL(post.url).searchParams.has("pin"),
          postData: post.postData || null,
          bodyHasPin: !!(post.postData && post.postData.includes(`pin=${PIN}`)),
        }
      : null;

    out.ok =
      out.getRequests.length === 2 &&
      out.getRequests.every((r) => !r.hasPinInQuery) &&
      !!out.postRequest &&
      !out.postRequest.hasPinInQuery &&
      out.postRequest.bodyHasPin;

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
