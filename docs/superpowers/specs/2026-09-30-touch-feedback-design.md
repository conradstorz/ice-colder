# Touch feedback for the dashboard v2 shell — design

**Date:** 2026-09-30
**Scope:** every page that extends `web_interface/templates/base.html`
(the operator dashboard, login, setup, enroll, error). `/screen` and
`/screen/body` (customer-facing) are out of scope and stay untouched.

## 1. Problem

The v2 shell targets a 7-inch tablet. Today every interactive element gives
feedback only through `hover:` classes (which fire late, or stick, on
touch) and a `focus:ring` on text inputs. There is no touch-down state, no
in-flight indication while an htmx request is outstanding, and the login
keypad keys give no feedback at all. A tap on a slow response looks like a
missed tap, so operators tap twice.

## 2. Requirement

Every element that accepts input must indicate its status instantly:

1. **Press** — the instant a finger lands on a button, link, tile, keypad
   key, checkbox, select or text field, it visibly changes.
2. **Busy** — an element whose htmx request is in flight stays visibly busy
   until the response is swapped in.
3. **Selected** — a chosen option (a checked checkbox, the selected tab or
   filter, the focused field) stays clearly highlighted while chosen.
4. **Sound** — every press plays a short audible click.

The sound is always on; the tablet's own volume control is the only mute.

## 3. Approach

Global rules, not per-element classes. All press/busy/focus/checked styling
lives in `web_interface/tailwind.input.css` inside `@layer base`, keyed on
element selectors and htmx's own `.htmx-request` class. `@layer base` is
never tree-shaken by Tailwind's content scan, so the rules ship regardless
of which templates reference them, and any future template gets the
behaviour for free with no per-element classes to remember.

The sound and the one browser quirk that needs JS live in a new vendored
script, `web_interface/static/feedback.js`, loaded from `base.html`.

The alternative — adding `active:`/`[&.htmx-request]:` utilities to each of
the ~130 tappable elements across 42 templates — was rejected as brittle:
one missed element breaks the guarantee, and every new template must
remember it.

## 4. Components

### 4.1 CSS (`web_interface/tailwind.input.css`, `@layer base`)

Selectors below are written once; "tappable" means
`button, [type="submit"], [type="button"], a[href], label[for], summary`
and "field" means `input:not([type="hidden"]), select, textarea`.

| State | Rule |
|---|---|
| Baseline | Tappables and fields get `-webkit-tap-highlight-color: transparent` (the browser's grey flash is replaced by ours) and `touch-action: manipulation`. Tappables get `transition: filter 60ms, transform 60ms` so the release is smooth; the press itself is immediate. |
| Press | `:active` on a tappable: `filter: brightness(0.85)` and `transform: scale(0.97)`. Colour-agnostic, so it works on white tiles, slate bar buttons, red confirm buttons and grey keypad keys alike. `:active` on a field: `filter: brightness(0.97)`. |
| Busy | `.htmx-request` on a tappable: `opacity: 0.6; cursor: progress; pointer-events: none` (a second tap during flight is ignored, which also prevents the double-submit that `POST /tests/sale` currently refuses server-side) plus an inline spinner via `::after` (a 1em bordered circle with `animation: spin 0.8s linear infinite`, drawn with `border-color: currentColor transparent currentColor transparent`). `form.htmx-request [type="submit"]` gets the same rule, because htmx puts the class on the *form* for a submitted form, not the button. `.htmx-request` on a boosted `<a>` (tiles, breadcrumbs, Back, Home) gets the opacity and cursor but no spinner, so the bar does not jump. |
| Focus | `:focus-visible` on a tappable or field: `outline: 2px solid theme(colors.blue.500); outline-offset: 2px`. Mouse clicks do not show it; taps on fields and keyboard focus do. The existing per-element `focus:ring-*` utilities stay and win where present. |
| Checked | `input[type="checkbox"], input[type="radio"]`: `accent-color: theme(colors.blue.600)`; checkboxes are rendered at `1.5rem` square (24 px). `select`: `accent-color` likewise. |
| Hover | `future.hoverOnlyWhenSupported: true` in `tailwind.config.js` wraps every generated `hover:` variant in `@media (hover: hover)`, so the existing hover classes no longer stick after a tap on the tablet and behave as before with a mouse. |

The spinner keyframes (`@keyframes spin`) are defined in the same file;
Tailwind's own `animate-spin` is not used because it is a utility class
that would be purged without a template reference.

Persistent selected state for tabs and filters already exists
(`border-b-2 border-blue-600 text-blue-600` on the current tab in the
Reports levels) and is unchanged.

### 4.2 Sound asset (`web_interface/static/click.wav`)

A committed, generated 16-bit PCM mono WAV at 22 050 Hz, about 30 ms long
(a short decaying noise burst with a 1 ms attack), under 5 KB. Generated once
by `scripts/make_click_wav.py` using only the standard library (`wave`,
`struct`, `random` with a fixed seed so the file is reproducible). The script
is committed so the asset can be regenerated; it is not run at build time.

### 4.3 Script (`web_interface/static/feedback.js`)

Plain ES5-compatible script, no dependencies, wrapped in an IIFE:

1. Registers an empty passive `touchstart` listener on `document`. iOS
   Safari only applies `:active` styles when such a listener exists.
2. Looks up `#tap-sound` (the `<audio>` element `base.html` adds). If it is
   absent, does nothing further.
3. Delegates `pointerdown` on `document`: if `event.target.closest(...)`
   matches the tappable-or-field selector from §4.1 (the selector string
   is duplicated here as a constant with a comment pointing at the CSS),
   sets `audio.currentTime = 0` and calls `audio.play()`, swallowing the
   returned promise's rejection (autoplay policy, or a device with no audio
   output, must never throw into the console).

Delegation on `document` means elements swapped in by htmx later are
covered with no re-binding.

### 4.4 `base.html`

In `<head>`, after `htmx.min.js`:

```html
<audio id="tap-sound" src="/static/click.wav" preload="auto" aria-hidden="true"></audio>
<script src="/static/feedback.js" defer></script>
```

`preload="auto"` fetches the WAV at page load so the first tap has no
network latency; the file is served from `/static` like `app.css`, which is
mounted without auth, so the login page gets it too.

### 4.5 Rebuild

`web_interface/static/app.css` is rebuilt with the standard command from
`CLAUDE.md` and committed alongside the source changes, as the existing
workflow requires.

## 5. Testing

| Test | Kind | What it proves |
|---|---|---|
| `tests/test_static_css.py::test_app_css_carries_touch_feedback_rules` | unit, CI | The committed `app.css` contains `:active`, `.htmx-request`, `:focus-visible`, `accent-color` and `@media (hover: hover)` — catches a source edit without a rebuild. |
| `tests/test_feedback_assets.py` | unit, CI | `click.wav` opens with `wave`, is mono 16-bit, 10–100 ms long, under 5 KB; `feedback.js` exists and is non-empty; `base.html` references both `/static/click.wav` and `/static/feedback.js`; `GET /static/click.wav` and `GET /static/feedback.js` return 200 via TestClient with the right content types. |
| `tests/test_feedback_assets.py::test_generator_is_reproducible` | unit, CI | Running `scripts/make_click_wav.py` into a temp path produces bytes identical to the committed file. |
| `tests/test_touch_feedback_browser.py` + `tests/browser/touch_feedback_check.mjs` | opt-in browser, CI | In headless Chrome on the Home page: (a) a `pointerdown` on a tile changes its computed `filter` from `none`; (b) with the test server's `/health` delayed 500 ms, a click on the Health tile puts `htmx-request` on the anchor while in flight and it is gone after the swap; (c) `pointerdown` on the Lock button fires `play()` on `#tap-sound` (spied by replacing `HTMLMediaElement.prototype.play` before the tap). |

The browser test follows the existing pattern exactly (skip unless
`ICE_COLDER_BROWSER_TESTS=1` plus Chrome and node are found) and is added to
the run-and-guard lists in `.github/workflows/ci.yml` and the CLAUDE.md
commands table.

## 6. Out of scope

- `/screen` and `/screen/body`.
- Any per-element restyling or colour changes.
- A mute toggle or a config setting for the sound.
- Haptic feedback.
