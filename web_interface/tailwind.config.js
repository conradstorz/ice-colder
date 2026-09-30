/**
 * Tailwind CSS v3.4.17 config for the ice-colder dashboard v2 shell.
 *
 * Built with the standalone Tailwind CLI (no Node project, no package.json):
 *   .tailwind/tailwindcss.exe -c web_interface/tailwind.config.js \
 *     -i web_interface/tailwind.input.css \
 *     -o web_interface/static/app.css --minify
 *
 * Light theme only (spec §3): slate-50 page background, white cards,
 * slate-800 top bar, green/amber/red status colors. `md` and `lg` are
 * overridden from Tailwind's defaults (768px / 1024px) to the tablet
 * breakpoints this shell targets: `md` at 600px (portrait tablet, 2x4 tile
 * grid) and `lg` at 900px (landscape tablet and desktop, 4x2 tile grid).
 */
module.exports = {
  // Wrap every generated `hover:` variant in @media (hover: hover) so a tap
  // on the tablet never leaves a sticky hover state behind; mouse users see
  // no change. Spec 2026-09-30 §4.1.
  future: { hoverOnlyWhenSupported: true },
  content: ["./web_interface/templates/**/*.html"],
  theme: {
    extend: {
      // md/lg are not Tailwind v3's defaults (768px / 1024px out of the
      // box) — §3 sets them to the tablet breakpoints this shell targets:
      // md at 600px (portrait tablet, 2x4 tile grid), lg at 900px
      // (landscape tablet and desktop, 4x2 tile grid). `extend` merges
      // these two into the default screens map rather than replacing it,
      // so sm/xl/2xl keep their defaults.
      screens: {
        md: "600px",
        lg: "900px",
      },
      // slate-50 page background / white cards / slate-800 bar and the
      // green/amber/red status colors (§3) are all Tailwind v3 defaults —
      // no color palette override needed here.
      fontSize: {
        // Body text is Tailwind's default `text-base` (16px / 1.5 line
        // height) already — listed here only as documentation of the §3
        // token, not redefined. Tile title and hero are new tokens §3 adds.
        "tile-title": ["20px", "1.4"],
        hero: ["24px", "1.3"],
      },
      spacing: {
        touch: "48px",
      },
      gap: {
        touch: "8px",
      },
    },
  },
  // Tailwind's JIT purge drops any @layer base/components/utilities
  // selector that isn't found literally in a scanned template —
  // `.touch-target` (web_interface/tailwind.input.css) is intentionally
  // defined ahead of any template using it (Task 4+ wires it into the
  // shell), so it is safelisted here to ship in the compiled app.css now
  // rather than silently disappearing until a future rebuild.
  // `.htmx-request` is the same failure mode from the other direction: htmx
  // adds that class at runtime (never in template source), so the scan
  // never finds it and both the busy-state opacity rule and its ::after
  // spinner in @layer base would be purged without this entry.
  safelist: ["touch-target", "htmx-request"],
  plugins: [],
};
