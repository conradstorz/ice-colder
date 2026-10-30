# Source watermark: step-and-repeat pattern + colorblind-safe tints

Date: 2026-10-29
Status: approved

## Goal

Two changes to the local-vs-remote page-source indicator shipped in PR #105:

1. Replace the single large centered watermark drawing with **small simplified
   line motifs repeated many times** across the whole viewport, in a
   step-and-repeat (half-drop staggered) print style.
2. Give each side a **distinct, colorblind-safe background tint**: light blue
   for remote, light warm orange for local (the blue/orange pair is distinct
   under deuteranopia, protanopia and tritanopia; both tints stay near-white
   so text contrast is unchanged).

## Current state

`packages/ui/src/ice_ui/templates/partials/source_watermark.html` renders one
`fixed inset-0` flex-centered inline SVG (~30rem): a detailed vending-machine
drawing for `page_source == "local"`, a detailed cloud cluster for
`"remote"`, tinted `text-slate-900/10` / `text-indigo-900/10` via
`currentColor`. `base.html` sets the body to `bg-slate-50` (local) /
`bg-indigo-50` (remote). `page_source` is injected by the edge's
`template_context` ("local") and by the cloud's `UiRegistry` template-env
global ("remote"); nothing about that wiring changes.

## Design

### Watermark partial (rewritten)

`source_watermark.html` stays an included partial rendering inline SVG (so
`currentColor` keeps carrying the Tailwind text tint — the reason the
original was inline, preserved here). New structure:

- One full-viewport SVG: `class="w-full h-full"` inside the existing
  `fixed inset-0 z-0 pointer-events-none overflow-hidden` wrapper (the flex
  centering classes go away).
- `<defs>` holds the mini motif as a `<g id="motif">` — a simplified
  ~10-stroke redraw of each drawing that stays legible at ~40–60px:
  - **local**: vending-machine outline (cabinet, window, two product bumps,
    dispense flap), ~`viewBox 0 0 40 56` worth of geometry;
  - **remote**: a single cloud outline with two or three dashed link stubs
    and node dots, ~`viewBox 0 0 56 40` worth.
- `<pattern id="step" patternUnits="userSpaceOnUse">` tiles a cell of
  roughly 112×128 (local) / 128×112 (remote) containing **two** `<use
  href="#motif">` placements, the second offset by half the cell in both
  axes — the half-drop stagger that makes it read as a print pattern rather
  than a grid.
- `<rect width="100%" height="100%" fill="url(#step)"/>` fills the viewport.
  Motif strokes use `stroke="currentColor"` (`fill="none"`), wrapper tint
  classes become `text-orange-900/10` (local) / `text-sky-900/10` (remote).
- `aria-hidden="true"` and the explanatory top comment stay.

### Background colors (base.html)

- Body: `bg-orange-50` (local) / `bg-sky-50` (remote), replacing
  `bg-slate-50` / `bg-indigo-50`.
- Everything else — bar colors (`bg-slate-800` / `bg-indigo-900`), hover
  tones, LOCAL/REMOTE tag, context badge — is unchanged. The tag + bar
  remain the primary signal; the tint and pattern are ambient reinforcement.

### CSS bundle

`bg-orange-50`, `bg-sky-50`, `text-orange-900`, `text-sky-900` (with `/10`)
are new to the templates; rebuild `packages/ui/src/ice_ui/static/app.css`
with the pinned Tailwind v3.4.17 CLI and commit it (`tests/ui/test_static_css.py`
fails CI if a used class is missing; stale leftover classes are harmless).

## Testing

- `tests/ui/golden/base_no_remote.html`: regenerate for the new body class
  and watermark markup.
- `tests/ui/test_badge.py`: update the watermark assertions (pattern id /
  motif presence per source, new tint classes) in place of the old
  single-drawing assertions.
- `tests/cloud/test_me.py` remote assertions: update if they match on the
  old classes.
- `tests/cloud/test_ui_parity.py`: unaffected — edge and cloud render the
  same template with different `page_source`.

## Non-goals

- No change to `page_source` wiring, the bar, tag, badge, `/screen`, or the
  login/setup pages beyond what they already inherit from `base.html`.
- No dark-mode variants (the dashboard has none today).
- No per-page density/size knobs.
