# Step-and-Repeat Source Watermark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single large local/remote watermark drawing with small simplified motifs tiled step-and-repeat across the viewport, and move the body tints to the colorblind-safe blue/orange pair (remote=blue, local=orange).

**Architecture:** `partials/source_watermark.html` stays an inline-SVG partial (so `currentColor` carries the Tailwind tint) but becomes a full-viewport SVG whose `<defs>` holds a ~10-stroke mini motif and a half-drop `<pattern>`; a full-size `<rect>` fills the viewport with it. `base.html` swaps the body background classes. Spec: `docs/superpowers/specs/2026-10-29-source-watermark-pattern-design.md`.

**Tech Stack:** Jinja2 templates, inline SVG, Tailwind v3.4.17 standalone CLI (vendored `app.css`), pytest.

## Global Constraints

- Never chain shell commands with `&&`; run each command as its own invocation.
- Python runs through `uv run ...` (e.g. `uv run pytest ...`); lint with `uv run ruff`, never the global ruff.
- `page_source` wiring (edge `template_context` → `"local"`, cloud `UiRegistry` env global → `"remote"`) is NOT changed.
- Bar colors, hover tones, LOCAL/REMOTE `#source-tag`, context badge, `/screen`: untouched.
- `tests/ui/golden/base_no_remote.html` is byte-exact (`-text` in .gitattributes): regenerate it by writing rendered bytes, never by hand-editing.
- Commit messages end with the trailer `Assisted-by: <the model actually writing the commit> (Claude Code)` and no `Co-Authored-By` line.

---

### Task 1: Pattern watermark + tints + tests

**Files:**
- Modify: `packages/ui/src/ice_ui/templates/partials/source_watermark.html` (full rewrite)
- Modify: `packages/ui/src/ice_ui/templates/base.html` (one line: body class)
- Modify: `tests/ui/test_badge.py:139-158` (`test_source_tag_defaults_to_local`, `test_source_tag_remote`)
- Modify: `tests/cloud/test_me.py` (`test_cloud_pages_are_marked_remote`)
- Regenerate: `tests/ui/golden/base_no_remote.html`
- Rebuild: `packages/ui/src/ice_ui/static/app.css`

**Interfaces:**
- Consumes: `page_source` template variable (`"local"`/`"remote"`, default `"local"`).
- Produces: stable test markers — local motif contains the literal `d="M11 50v4M29 50v4"`, remote motif contains `d="M14 26c-5-1-7-8-2-11c-1-6 6-10 11-7c3-5 12-4 13 2c6 0 8 8 3 11Z"`, both branches contain `<pattern id="step"`. Body classes: `bg-orange-50` (local) / `bg-sky-50` (remote).

- [ ] **Step 1: Update the two watermark tests to the new markers (failing first)**

In `tests/ui/test_badge.py`, replace `test_source_tag_defaults_to_local` and `test_source_tag_remote` with:

```python
LOCAL_MOTIF = 'd="M11 50v4M29 50v4"'  # vending-machine feet
REMOTE_MOTIF = 'd="M14 26c-5-1-7-8-2-11c-1-6 6-10 11-7c3-5 12-4 13 2c6 0 8 8 3 11Z"'


def test_source_tag_defaults_to_local():
    html = _render()
    assert 'id="source-tag"' in html and ">LOCAL</span>" in html
    assert "REMOTE" not in html
    assert "bg-slate-800" in html and "bg-orange-50" in html
    assert "bg-indigo-900" not in html and "bg-sky-50" not in html
    assert '<pattern id="step"' in html
    assert LOCAL_MOTIF in html  # machine watermark motif
    assert REMOTE_MOTIF not in html
    assert "text-orange-900/10" in html
    # rendered even with no badge, and before the badge when there is one
    assert "context-badge" not in html
    html = _render(badge=_badge())
    assert html.index("source-tag") < html.index("context-badge")


def test_source_tag_remote():
    html = _render(page_source="remote")
    assert ">REMOTE</span>" in html and "LOCAL" not in html
    assert "bg-indigo-900" in html and "bg-sky-50" in html
    assert "bg-slate-800" not in html and "bg-orange-50" not in html
    assert '<pattern id="step"' in html
    assert REMOTE_MOTIF in html  # cloud watermark motif
    assert LOCAL_MOTIF not in html
    assert "text-sky-900/10" in html
```

In `tests/cloud/test_me.py`, inside `test_cloud_pages_are_marked_remote`, replace the last two asserts of the loop:

```python
        assert "bg-indigo-900" in _bar(html) and "bg-sky-50" in html, path
        assert '<pattern id="step"' in html, path
```

(The `"M118 112c-4-26"` marker line is deleted; the import-free string constants above live only in `test_badge.py`, so `test_me.py` just checks the pattern id.)

- [ ] **Step 2: Run the updated tests to verify they fail**

Run: `uv run pytest tests/ui/test_badge.py::test_source_tag_defaults_to_local tests/ui/test_badge.py::test_source_tag_remote "tests/cloud/test_me.py::test_cloud_pages_are_marked_remote" -q`
Expected: 3 FAIL (old markup still rendered).

- [ ] **Step 3: Rewrite the watermark partial**

Replace the entire contents of `packages/ui/src/ice_ui/templates/partials/source_watermark.html` with:

```html
{# Decorative full-page watermark: which side of the wire this page is served
   from, as a step-and-repeat (half-drop) print pattern of small line motifs.
   Inline (not <img>/background) so currentColor takes the text-*/10 tint. #}
{% set source = page_source | default("local") %}
<div aria-hidden="true" class="fixed inset-0 z-0 pointer-events-none overflow-hidden {{ 'text-sky-900/10' if source == 'remote' else 'text-orange-900/10' }}">
{% if source == 'remote' %}
<svg class="w-full h-full" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <defs>
    <g id="motif">
      <path d="M14 26c-5-1-7-8-2-11c-1-6 6-10 11-7c3-5 12-4 13 2c6 0 8 8 3 11Z"/>
      <circle cx="10" cy="37" r="2"/>
      <circle cx="27" cy="39" r="2"/>
      <circle cx="44" cy="35" r="2"/>
      <path d="M12 35l7-6" stroke-dasharray="3 4"/>
      <path d="M27 37v-8" stroke-dasharray="3 4"/>
      <path d="M42 33l-6-5" stroke-dasharray="3 4"/>
    </g>
    <pattern id="step" patternUnits="userSpaceOnUse" width="128" height="112">
      <use href="#motif" x="16" y="10"/>
      <use href="#motif" x="80" y="66"/>
    </pattern>
  </defs>
  <rect width="100%" height="100%" fill="url(#step)" stroke="none"/>
</svg>
{% else %}
<svg class="w-full h-full" xmlns="http://www.w3.org/2000/svg" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <defs>
    <g id="motif">
      <rect x="4" y="2" width="32" height="48" rx="4"/>
      <rect x="8" y="8" width="17" height="24" rx="2"/>
      <path d="M8 20h17"/>
      <circle cx="13" cy="14" r="2.5"/>
      <circle cx="20" cy="14" r="2.5"/>
      <circle cx="13" cy="26" r="2.5"/>
      <circle cx="20" cy="26" r="2.5"/>
      <rect x="28" y="10" width="5" height="10" rx="1.5"/>
      <rect x="8" y="38" width="24" height="7" rx="2"/>
      <path d="M11 50v4M29 50v4"/>
    </g>
    <pattern id="step" patternUnits="userSpaceOnUse" width="112" height="128">
      <use href="#motif" x="16" y="12"/>
      <use href="#motif" x="72" y="76"/>
    </pattern>
  </defs>
  <rect width="100%" height="100%" fill="url(#step)" stroke="none"/>
</svg>
{% endif %}
</div>
```

(Half-drop: the second `<use>` sits half a cell right and half a cell down; the pattern cell is ~2.8× the motif in each axis, so motifs render ~40–56px with generous gutters. `stroke="none"` on the fill rect keeps the pattern cell edges invisible.)

- [ ] **Step 4: Swap the body background classes in base.html**

In `packages/ui/src/ice_ui/templates/base.html`, change the body line:

```html
{% set is_remote = (page_source | default("local")) == "remote" %}<body class="{{ 'bg-sky-50' if is_remote else 'bg-orange-50' }} text-gray-900 font-sans text-base min-h-screen"
```

(Only `bg-indigo-50`→`bg-sky-50` and `bg-slate-50`→`bg-orange-50` change; the rest of the line and file stay as they are.)

- [ ] **Step 5: Run the three tests again**

Run: `uv run pytest tests/ui/test_badge.py::test_source_tag_defaults_to_local tests/ui/test_badge.py::test_source_tag_remote "tests/cloud/test_me.py::test_cloud_pages_are_marked_remote" -q`
Expected: 3 PASS.

- [ ] **Step 6: Regenerate the golden file (byte-exact)**

Run:

```
uv run python -c "from ice_ui.levels import LEVEL_HEALTH_FAULTS; from ice_ui.templating import templates; from pathlib import Path; html = templates.get_template('base.html').render(level=LEVEL_HEALTH_FAULTS); Path('tests/ui/golden/base_no_remote.html').write_bytes(html.encode('utf-8'))"
```

Then run: `uv run pytest tests/ui/test_base_golden.py -q`
Expected: 2 PASS. Also run `git diff --stat tests/ui/golden/base_no_remote.html` and confirm the diff shows the new pattern markup and `bg-orange-50` body (sanity that the regeneration actually re-rendered).

- [ ] **Step 7: Rebuild the committed Tailwind bundle**

Run (repo root):

```
.tailwind/tailwindcss.exe -c packages/ui/tailwind.config.js -i packages/ui/tailwind.input.css -o packages/ui/src/ice_ui/static/app.css --minify
```

Then run: `uv run pytest tests/ui/test_static_css.py -q`
Expected: PASS (`bg-orange-50`, `bg-sky-50`, `text-orange-900`, `text-sky-900` now in the bundle).

- [ ] **Step 8: Lint and commit**

Run: `uv run ruff check --fix .` then `uv run ruff format .` (should touch only the two test files, if anything).

```bash
git add packages/ui/src/ice_ui/templates/partials/source_watermark.html packages/ui/src/ice_ui/templates/base.html packages/ui/src/ice_ui/static/app.css tests/ui/test_badge.py tests/ui/golden/base_no_remote.html tests/cloud/test_me.py
git commit -m "feat(ui): step-and-repeat source watermark with colorblind-safe tints"
```

(Commit message body may note remote=sky/local=orange per the spec; end with the attribution trailer from Global Constraints.)

---

### Task 2: Verification sweep

**Files:**
- No new changes expected; fix fallout only.

**Interfaces:**
- Consumes: Task 1's committed markers (`<pattern id="step"`, `bg-orange-50`, `bg-sky-50`).
- Produces: a green UI + cloud test run.

- [ ] **Step 1: Run the full UI and cloud suites plus parity**

Run: `uv run pytest tests/ui tests/cloud -q`
Expected: all pass (parity compares edge vs cloud rendering of the same templates, so it is unaffected by design; this run proves it). If any test fails on a stale `bg-slate-50`/`bg-indigo-50`/old-path assertion, update that assertion to the Task 1 markers and re-run.

- [ ] **Step 2: Run the edge web routes tests (they render base.html everywhere)**

Run: `uv run pytest tests/edge/web -q`
Expected: all pass. Same fallout rule as Step 1.

- [ ] **Step 3: Commit any fallout fixes**

Only if Steps 1–2 changed files:

```bash
git add -u tests
git commit -m "test(ui): align remaining assertions with pattern watermark"
```

(End with the attribution trailer from Global Constraints.)
