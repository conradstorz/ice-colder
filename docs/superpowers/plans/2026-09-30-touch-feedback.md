# Touch Feedback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every tappable element and field on every page that extends `base.html` shows an instant press state, a busy state while its htmx request is in flight, a clear selected/focused state, and plays a click sound on press.

**Architecture:** Global element-level CSS in `@layer base` of `web_interface/tailwind.input.css` (never purged, so no per-template classes), keyed on `:active`, htmx's own `.htmx-request`, `:focus-visible` and `accent-color`; Tailwind's `hoverOnlyWhenSupported` flag stops sticky hover on touch. One vendored script `web_interface/static/feedback.js` plays a committed, generated `web_interface/static/click.wav` on delegated `pointerdown`. Spec: `docs/superpowers/specs/2026-09-30-touch-feedback-design.md`.

**Tech Stack:** Tailwind v3.4.17 standalone CLI, htmx 1.9.10 (vendored), plain ES5 JS, Python `wave` stdlib, pytest + FastAPI TestClient, headless Chrome via CDP through Node (existing `tests/browser/*.mjs` pattern).

## Global Constraints

- **No `&&` in shell commands** — run each command as its own Bash call (global CLAUDE.md).
- **Python via `uv`** only: `uv run pytest`, `uv run python …`.
- **Rebuild and commit `app.css`** after any change to `tailwind.input.css` or `tailwind.config.js`: `.tailwind/tailwindcss.exe -c web_interface/tailwind.config.js -i web_interface/tailwind.input.css -o web_interface/static/app.css --minify`. `tests/test_static_css.py` only checks classes templates use are present; it cannot detect a missing rebuild by itself, which is why Task 3 adds a rule-presence test.
- **`/screen` and `/screen/body` are out of scope** — `screen.html` does not extend `base.html`; do not touch it.
- **No per-element restyling**, no colour changes, no mute toggle, no config setting.
- **Sound always on**; the only mute is the tablet's volume.
- **Browser tests** skip only when `ICE_COLDER_BROWSER_TESTS` is not `"1"` or Chrome/node is missing; `tests/skip_policy.py` deliberately excludes them, so in CI any skip fails the guard. Follow `tests/test_dashboard_v2_home_browser.py` exactly.
- **Lint** before each commit: `ruff check --fix .` then `ruff format .`.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## File Structure

| Path | Responsibility |
|---|---|
| `scripts/make_click_wav.py` (create) | Deterministic generator for the click WAV; stdlib only; `main(out_path)` and `render_click() -> bytes`. |
| `web_interface/static/click.wav` (create, committed) | The generated asset. |
| `web_interface/static/feedback.js` (create) | iOS `:active` enabler + delegated `pointerdown` click-sound player. |
| `web_interface/templates/base.html` (modify, `<head>`) | Adds the `<audio id="tap-sound">` and the script tag. |
| `web_interface/tailwind.input.css` (modify) | `@layer base` press/busy/focus/checked rules and `@keyframes spin`. |
| `web_interface/tailwind.config.js` (modify) | `future: { hoverOnlyWhenSupported: true }`. |
| `web_interface/static/app.css` (rebuild, committed) | Compiled output. |
| `tests/test_feedback_assets.py` (create) | WAV validity, generator reproducibility, static serving, base.html wiring. |
| `tests/test_static_css.py` (modify) | Rule-presence test for the compiled CSS. |
| `tests/test_touch_feedback_browser.py` + `tests/browser/touch_feedback_check.mjs` (create) | Opt-in headless-Chrome check of press, busy, and sound. |
| `CLAUDE.md` (modify) | Browser-test command row and paragraph list the fourth browser test. |

---

### Task 1: Click sound asset and its generator

**Files:**
- Create: `scripts/make_click_wav.py`
- Create: `web_interface/static/click.wav`
- Test: `tests/test_feedback_assets.py`

**Interfaces:**
- Produces: `scripts.make_click_wav.render_click() -> bytes` (complete WAV file bytes, deterministic); `scripts.make_click_wav.main(out_path: Path) -> None` writes them; `python scripts/make_click_wav.py [out_path]` defaults to `web_interface/static/click.wav`. `scripts/` needs an empty `__init__.py` only if the test imports it as a package — instead the test should load it with `importlib.util.spec_from_file_location`, so no package is needed.
- Produces: the committed asset at `/static/click.wav` that Task 2 references.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_feedback_assets.py
"""Touch-feedback assets: the click sound, its generator, and (Task 2) the
script and base.html wiring. See docs/superpowers/specs/2026-09-30-touch-feedback-design.md §4.2–4.4."""
from __future__ import annotations

import importlib.util
import io
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "web_interface" / "static"
CLICK_WAV = STATIC / "click.wav"
GENERATOR = ROOT / "scripts" / "make_click_wav.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("make_click_wav", GENERATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _wav_params(data: bytes):
    with wave.open(io.BytesIO(data)) as w:
        return w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()


def test_click_wav_is_a_short_mono_16bit_click():
    assert CLICK_WAV.exists(), "run: uv run python scripts/make_click_wav.py"
    data = CLICK_WAV.read_bytes()
    channels, width, rate, frames = _wav_params(data)
    assert channels == 1
    assert width == 2
    assert rate == 22050
    duration_ms = frames * 1000 / rate
    assert 10 <= duration_ms <= 100, duration_ms
    assert len(data) < 5000, len(data)


def test_click_wav_is_not_silent():
    with wave.open(str(CLICK_WAV)) as w:
        raw = w.readframes(w.getnframes())
    peak = max(abs(int.from_bytes(raw[i : i + 2], "little", signed=True)) for i in range(0, len(raw), 2))
    assert peak > 8000, "click is inaudibly quiet"


def test_generator_is_reproducible(tmp_path):
    mod = _load_generator()
    assert mod.render_click() == mod.render_click()
    out = tmp_path / "click.wav"
    mod.main(out)
    assert out.read_bytes() == CLICK_WAV.read_bytes(), (
        "committed click.wav differs from the generator's output; regenerate it"
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_feedback_assets.py -v`
Expected: 3 failures — `click.wav` missing / generator missing.

- [ ] **Step 3: Write the generator**

`scripts/make_click_wav.py`, stdlib only (`wave`, `struct`, `random`, `math`, `pathlib`, `sys`):

- Constants: `RATE = 22050`, `DURATION_MS = 30`, `SEED = 20260930`.
- `render_click() -> bytes`: `random.Random(SEED)` noise in `[-1, 1]`, multiplied by an envelope with a 1 ms linear attack then exponential decay (`exp(-t / 6ms)`), scaled to `0.8 * 32767`, packed as little-endian `int16`, written through `wave.open` on a `BytesIO` with 1 channel / 2-byte width / `RATE`; returns the buffer's bytes.
- `main(out_path: Path) -> None`: writes `render_click()` to `out_path`.
- `if __name__ == "__main__":` takes an optional argv path, defaulting to `web_interface/static/click.wav` relative to the repo root (resolve from `__file__`), and prints the byte count written.

- [ ] **Step 4: Generate the asset**

Run: `uv run python scripts/make_click_wav.py`
Expected: prints a byte count between about 1,300 and 1,400 (30 ms × 22 050 Hz × 2 bytes + 44-byte header).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_feedback_assets.py -v`
Expected: 3 passed.

- [ ] **Step 6: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add scripts/make_click_wav.py web_interface/static/click.wav tests/test_feedback_assets.py
git commit -m "feat(ui): generated click sound asset for touch feedback"
```

---

### Task 2: feedback.js and base.html wiring

**Files:**
- Create: `web_interface/static/feedback.js`
- Modify: `web_interface/templates/base.html` (`<head>`, after the `htmx.min.js` script tag)
- Test: `tests/test_feedback_assets.py` (append)

**Interfaces:**
- Consumes: `/static/click.wav` from Task 1.
- Produces: `<audio id="tap-sound">` in every page extending `base.html`; `window.HTMLMediaElement.prototype.play` is what the browser test (Task 4) spies on, so the script must call `audio.play()` normally, not clone the element or use Web Audio.
- Produces: the tappable selector constant `TAPPABLE` in `feedback.js` — must equal, token for token, the selector list Task 3's CSS uses: `button, [type="submit"], [type="button"], a[href], label[for], summary, input:not([type="hidden"]), select, textarea`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_feedback_assets.py`)

```python
from fastapi.testclient import TestClient

from web_interface.server import app

FEEDBACK_JS = STATIC / "feedback.js"
BASE_HTML = ROOT / "web_interface" / "templates" / "base.html"


def test_feedback_js_exists_and_registers_pointerdown():
    src = FEEDBACK_JS.read_text(encoding="utf-8")
    assert "pointerdown" in src
    assert "touchstart" in src
    assert "tap-sound" in src
    assert ".play()" in src


def test_base_html_wires_audio_and_script():
    html = BASE_HTML.read_text(encoding="utf-8")
    assert 'id="tap-sound"' in html
    assert 'src="/static/click.wav"' in html
    assert 'preload="auto"' in html
    assert '<script src="/static/feedback.js" defer></script>' in html
    # the script must load after htmx so htmx-request styling and the sound
    # both exist by the time the first swap can happen
    assert html.index("htmx.min.js") < html.index("feedback.js")


def test_static_feedback_files_are_served():
    client = TestClient(app)
    wav = client.get("/static/click.wav")
    assert wav.status_code == 200
    assert wav.headers["content-type"].startswith("audio/")
    js = client.get("/static/feedback.js")
    assert js.status_code == 200
    assert "javascript" in js.headers["content-type"]
```

Note: `/static` is mounted without auth (`web_interface/server.py:11-14`), so no session fixture is needed.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_feedback_assets.py -v`
Expected: the 3 new tests fail (file missing, markup missing, 404).

- [ ] **Step 3: Write `feedback.js`**

An IIFE, ES5 syntax (`var`, `function`), no dependencies, with a header comment pointing at the spec §4.3 and at `tailwind.input.css` for the matching selector:

1. `document.addEventListener("touchstart", function () {}, { passive: true });` — iOS Safari only applies `:active` when a touchstart listener exists.
2. `var TAPPABLE = 'button, [type="submit"], [type="button"], a[href], label[for], summary, input:not([type="hidden"]), select, textarea';`
3. `document.addEventListener("pointerdown", handler, { passive: true })` where `handler`: finds `document.getElementById("tap-sound")` on each call (the element is static in `<head>` but looking it up per event costs nothing and survives any future OOB swap); returns if absent; returns if `!(event.target instanceof Element) || !event.target.closest(TAPPABLE)`; sets `audio.currentTime = 0`; `var p = audio.play(); if (p && p.catch) { p.catch(function () {}); }`.

No other behaviour. No press class management — `:active` in CSS handles the visual.

- [ ] **Step 4: Wire `base.html`**

Directly after `<script src="/static/htmx.min.js"></script>` in `<head>`, add, with a two-line Jinja comment citing the spec:

```html
<audio id="tap-sound" src="/static/click.wav" preload="auto" aria-hidden="true"></audio>
<script src="/static/feedback.js" defer></script>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_feedback_assets.py tests/test_static_css.py -v`
Expected: all pass (`test_static_css` still passes because no new class attributes were added).

- [ ] **Step 6: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface/static/feedback.js web_interface/templates/base.html tests/test_feedback_assets.py
git commit -m "feat(ui): play click sound on pointerdown for every tappable"
```

---

### Task 3: Press, busy, focus and checked CSS; hover only when supported

**Files:**
- Modify: `web_interface/tailwind.input.css` (new `@layer base` block after the `@tailwind` directives, before the existing `@layer components`)
- Modify: `web_interface/tailwind.config.js` (add `future` key at the top of the exported object)
- Rebuild: `web_interface/static/app.css`
- Test: `tests/test_static_css.py` (append)

**Interfaces:**
- Consumes: the `TAPPABLE` selector from Task 2 (same list, split into tappables and fields below).
- Produces: compiled rules the browser test asserts on by computed style: an `:active` tappable has `filter` ≠ `none`; an `.htmx-request` anchor has `opacity` `0.6`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_static_css.py`)

```python
def test_app_css_carries_touch_feedback_rules():
    """The touch-feedback rules live in @layer base of tailwind.input.css
    (spec 2026-09-30 §4.1). They are element selectors, not classes, so the
    coverage test above cannot see them; this catches a source edit that
    was never rebuilt into the committed app.css."""
    css = APP_CSS_PATH.read_text(encoding="utf-8")
    for needle in (
        ":active",
        ".htmx-request",
        ":focus-visible",
        "accent-color:",
        "-webkit-tap-highlight-color:transparent",
        "touch-action:manipulation",
        "@keyframes spin",
        "@media (hover:hover)",
    ):
        assert needle in css, f"{needle!r} missing from app.css — rebuild it"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_static_css.py::test_app_css_carries_touch_feedback_rules -v`
Expected: FAIL on `.htmx-request` (or `:active`).

- [ ] **Step 3: Add the Tailwind future flag**

In `web_interface/tailwind.config.js`, first property of `module.exports`:

```js
  // Wrap every generated `hover:` variant in @media (hover: hover) so a tap
  // on the tablet never leaves a sticky hover state behind; mouse users see
  // no change. Spec 2026-09-30 §4.1.
  future: { hoverOnlyWhenSupported: true },
```

- [ ] **Step 4: Add the base-layer rules**

In `web_interface/tailwind.input.css`, insert after the three `@tailwind` lines. These rules are the deliverable, so they are given in full:

```css
@layer base {
  /* Touch feedback for every tappable and field on pages that extend
     base.html — spec docs/superpowers/specs/2026-09-30-touch-feedback-design.md
     §4.1. Element selectors in @layer base are never tree-shaken, so no
     template needs a class for any of this. The selector list must match
     TAPPABLE in web_interface/static/feedback.js. */

  button, [type="submit"], [type="button"], a[href], label[for], summary,
  input:not([type="hidden"]), select, textarea {
    -webkit-tap-highlight-color: transparent;
    touch-action: manipulation;
  }

  /* Press: immediate on :active, 60ms ease back on release. Colour-agnostic
     so it reads on white tiles, slate bar buttons, red confirms and grey
     keypad keys alike. */
  button, [type="submit"], [type="button"], a[href], label[for], summary {
    transition: filter 60ms ease-out, transform 60ms ease-out;
  }
  button:active, [type="submit"]:active, [type="button"]:active,
  a[href]:active, label[for]:active, summary:active {
    filter: brightness(0.85);
    transform: scale(0.97);
  }
  input:not([type="hidden"]):active, select:active, textarea:active {
    filter: brightness(0.97);
  }

  /* Busy: htmx adds .htmx-request to the element that issued the request
     (the <form> for a submitted form, the <a> for a boosted link). */
  button.htmx-request, [type="submit"].htmx-request, [type="button"].htmx-request,
  form.htmx-request [type="submit"], form.htmx-request button:not([type="button"]),
  a[href].htmx-request {
    opacity: 0.6;
    cursor: progress;
    pointer-events: none;
  }
  button.htmx-request::after, [type="submit"].htmx-request::after,
  [type="button"].htmx-request::after,
  form.htmx-request [type="submit"]::after,
  form.htmx-request button:not([type="button"])::after {
    content: "";
    display: inline-block;
    width: 1em;
    height: 1em;
    margin-left: 0.5em;
    border: 2px solid;
    border-color: currentColor transparent currentColor transparent;
    border-radius: 9999px;
    animation: spin 0.8s linear infinite;
    vertical-align: -0.15em;
  }
  @keyframes spin {
    to { transform: rotate(360deg); }
  }

  /* Focus: keyboard focus and a tap into a field; mouse clicks stay quiet.
     Per-element focus:ring-* utilities in templates still apply on top. */
  button:focus-visible, [type="submit"]:focus-visible, [type="button"]:focus-visible,
  a[href]:focus-visible, summary:focus-visible,
  input:not([type="hidden"]):focus-visible, select:focus-visible, textarea:focus-visible {
    outline: 2px solid theme(colors.blue.500);
    outline-offset: 2px;
  }

  /* Checked / chosen: native controls, brand colour, finger-sized boxes. */
  input[type="checkbox"], input[type="radio"], select {
    accent-color: theme(colors.blue.600);
  }
  input[type="checkbox"], input[type="radio"] {
    width: 1.5rem;
    height: 1.5rem;
  }
}
```

Note: the existing checkbox markup carries `h-5 w-5` utilities; utilities beat base, so those boxes stay 20 px unless the utilities are removed. Do **not** remove them in this task (no per-element restyling) — the 24 px default applies to any future checkbox without explicit size utilities, which is what the spec's "rendered at 1.5rem" means for new markup. Leave a one-line comment saying so.

- [ ] **Step 5: Rebuild `app.css`**

Run: `.tailwind/tailwindcss.exe -c web_interface/tailwind.config.js -i web_interface/tailwind.input.css -o web_interface/static/app.css --minify`
Expected: "Done in …ms". If `.tailwind/tailwindcss.exe` is absent, download the v3.4.17 standalone asset named in `CLAUDE.md` into `.tailwind/` first.

- [ ] **Step 6: Run the CSS tests**

Run: `uv run pytest tests/test_static_css.py -v`
Expected: all pass, including the new rule-presence test. If `@media (hover:hover)` is absent, confirm the `future` key is at the top level of `module.exports` (not under `theme`).

- [ ] **Step 7: Visual smoke check (optional but recommended)**

Run the app (`uv run python main.py`) and, on `/login`, confirm a keypad key dims and shrinks while held and that a tap plays a click. Do not commit any change from this step.

- [ ] **Step 8: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add web_interface/tailwind.input.css web_interface/tailwind.config.js web_interface/static/app.css tests/test_static_css.py
git commit -m "feat(ui): global press, busy, focus and checked states; hover only when supported"
```

---

### Task 4: Opt-in browser test for press, busy and sound; CLAUDE.md

**Files:**
- Create: `tests/browser/touch_feedback_check.mjs`
- Create: `tests/test_touch_feedback_browser.py`
- Modify: `CLAUDE.md` (Commands table "Run the opt-in browser tests locally" row; the **Browser tests** paragraph beneath it)

**Interfaces:**
- Consumes: the `live_server` fixture pattern, `_find_chrome`, `_free_port`, `web_auth_backoff_reset` from `tests/test_dashboard_v2_home_browser.py` — copy them verbatim into the new test file (each browser test file is self-contained today; keep that pattern).
- Consumes: the CDP bootstrap from `tests/browser/dashboard_v2_home_check.mjs` lines 1–113 (Chrome launch, WebSocket, `cmd`, `evalJs`, cookie setup) — copy verbatim, changing only the temp-dir prefix to `cdp-touch-check-`.
- Produces: one JSON line on stdout `{ok, pressFilter, busyDuringFlight, busyAfterSwap, landedOnHealth, plays, error?}`.

- [ ] **Step 1: Write the pytest wrapper**

`tests/test_touch_feedback_browser.py`: same docstring shape as the home browser test (why a browser is needed: `:active`, `.htmx-request` timing and `play()` are all client-side and invisible to TestClient), same skip marker, same fixture, and:

```python
def test_press_busy_and_click_sound_in_a_real_browser(live_server):
    chrome = _find_chrome()
    node = shutil.which("node")
    if not chrome or not node:
        pytest.skip("Chrome or Node not found on this machine")
    env = dict(os.environ)
    env["CHROME_PATH"] = chrome
    result = subprocess.run(
        [node, str(_NODE_SCRIPT), live_server["base_url"],
         live_server["session_cookie"], live_server["device_cookie"]],
        capture_output=True, text=True, timeout=90, env=env,
    )
    assert result.stdout.strip(), f"browser check produced no output; stderr: {result.stderr}"
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload.get("ok") is True, json.dumps(payload, indent=2)
    assert payload["pressFilter"] != "none"
    assert payload["busyDuringFlight"] is True
    assert payload["busyAfterSwap"] is False
    assert payload["landedOnHealth"] is True
    assert payload["plays"] >= 1
```

- [ ] **Step 2: Run it to verify it fails**

Run: `$env:ICE_COLDER_BROWSER_TESTS="1"` then `uv run pytest tests/test_touch_feedback_browser.py -v`
Expected: FAIL — the node script does not exist yet.

- [ ] **Step 3: Write the checker**

`tests/browser/touch_feedback_check.mjs`, after the copied bootstrap and `Page.navigate` to `${baseUrl}/` plus a 2000 ms settle:

1. **Spy on play** — `evalJs`: replace `HTMLMediaElement.prototype.play` with a function that increments `window.__plays` and returns `Promise.resolve()`.
2. **Locate the Health tile** — `evalJs` returning the centre `{x, y}` of `document.querySelector('main a[href="/health"]').getBoundingClientRect()`.
3. **Delay `/health`** — `await cmd("Fetch.enable", { patterns: [{ urlPattern: "*/health", requestStage: "Request" }] })`. Register a `ws` message listener for `Fetch.requestPaused` that stores the `requestId` in a variable and does **not** continue it yet.
4. **Press** — `cmd("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 })`; `await sleep(50)`; `out.pressFilter = await evalJs('getComputedStyle(document.querySelector(\'main a[href="/health"]\')).filter')` (expect `brightness(0.85)`); `out.plays = await evalJs("window.__plays || 0")`.
5. **Release** — `mouseReleased` with the same params. Boosted navigation fires; wait until the `Fetch.requestPaused` variable is set (poll up to 3 s). Then `out.busyDuringFlight = await evalJs('document.querySelector(\'main a[href="/health"]\').classList.contains("htmx-request")')`.
6. **Continue** — `cmd("Fetch.continueRequest", { requestId })`; `await sleep(1500)`; `out.landedOnHealth = await evalJs("location.pathname") === "/health"`; `out.busyAfterSwap = await evalJs('!!document.querySelector(".htmx-request")')`.
7. `out.ok = out.pressFilter !== "none" && out.busyDuringFlight && !out.busyAfterSwap && out.landedOnHealth && out.plays >= 1;`

Keep the same `main().then(...).catch(...)` tail as the home checker.

- [ ] **Step 4: Run it to verify it passes**

Run: `uv run pytest tests/test_touch_feedback_browser.py -v` (with `ICE_COLDER_BROWSER_TESTS=1` still set)
Expected: 1 passed. If `pressFilter` is `none`, check that `mousePressed` hit inside the tile (log the rect in `out`); if `busyDuringFlight` is false, confirm the pattern `*/health` matched the boosted GET (log `request.url` from the paused event).

- [ ] **Step 5: Run the whole suite without the flag**

Run: `Remove-Item Env:ICE_COLDER_BROWSER_TESTS` then `uv run pytest -q`
Expected: all pass, the four browser tests reported as skipped with the opt-in reason.

- [ ] **Step 6: Update `CLAUDE.md`**

- Commands table row "Run the opt-in browser tests locally": append ` tests/test_touch_feedback_browser.py` to the command.
- **Browser tests** paragraph: list the fourth file, and change "these three" / "any of them" wording to four.
- In the Web Dashboard section, after the sentence on vendored Tailwind/HTMX, add two sentences: every page extending `base.html` gets global press (`:active`), busy (`.htmx-request`), focus-visible and checked styling from `@layer base` in `tailwind.input.css` with no per-element classes, and a click sound from `static/feedback.js` playing `static/click.wav` (generated by `scripts/make_click_wav.py`; regenerate rather than hand-edit). Note `future.hoverOnlyWhenSupported` in the Tailwind config.

- [ ] **Step 7: Lint and commit**

```bash
ruff check --fix .
ruff format .
git add tests/browser/touch_feedback_check.mjs tests/test_touch_feedback_browser.py CLAUDE.md
git commit -m "test(ui): headless-Chrome check for press, busy and click sound; docs"
```

---

## Self-review

- **Spec coverage:** §4.1 CSS → Task 3; §4.2 WAV → Task 1; §4.3 script → Task 2; §4.4 base.html → Task 2; §4.5 rebuild → Task 3 step 5; §5 tests → Tasks 1–4; CI guard needs no list edit (skip policy already fails any browser skip); CLAUDE.md → Task 4.
- **Consistency:** the tappable selector list is identical in Task 2 (`TAPPABLE`) and Task 3 (CSS); `#tap-sound` id, `/static/click.wav` and `/static/feedback.js` paths agree across Tasks 1, 2, 4; `render_click`/`main` names agree between Task 1's test and generator.
- **Known judgement call:** existing checkboxes keep their `h-5 w-5` utilities (Task 3 note), honouring "no per-element restyling" over the spec's 24 px figure for new markup.
