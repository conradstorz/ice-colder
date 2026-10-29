# Touch-UI review — TODO

Findings from the 2026-10-28 review of every edge (`packages/ui/src/ice_ui/templates/`)
and cloud (`packages/cloud/src/ice_cloud/templates/`) template, judged for clarity and
ease of use on phones (~360px), tablets and desktops. Paths below are relative to those
template roots unless prefixed. Severity: **H** high, **M** medium, **L** low.

Not in scope (already good, don't regress): global press/busy/focus feedback and the
24px checkbox default in `packages/ui/tailwind.input.css`, the 48px `.touch-target`
component, `overflow-x-auto` on report tables, label `for=`/`id` pairing across
settings forms, the two-tap `partials/confirm_button.html` pattern where it is used.

**Deferred — `/screen` guard:** `tests/edge/web/test_web_routes.py` pins
`screen.html` and `partials/screen_body.html` byte-identical to origin/main, so
every finding on those two files (tagged **[deferred: /screen pin]** below) is
blocked until the owner decides to lift that guard.

---

## 1. Phone-width blockers (content unreachable at 360px)

- [x] **H** `partials/status_fragment.html:36,130` — hero stats block
  (`flex gap-8 text-sm text-right ml-8`: Payment / State / Escrow / Last Payment)
  never wraps inside the card's `overflow-hidden`; Escrow and Last Payment are
  clipped invisible on phones. Stack below the title at small widths
  (`flex-col sm:flex-row` or `flex-wrap`, drop `ml-8` on phones).
- [x] **H** `reports_period.html:20-30`, `reports_product.html:17-27`,
  `reports_product_sku.html:40-50`, `reports_method.html:16-25` — the five range
  tabs (~440px total, `px-4 py-2.5`) sit in a non-wrapping flex row inside the
  card's `overflow-hidden` (e.g. `reports_period.html:17`); "12 Months" and
  "All Time" are unreachable on phones — not scrollable, just gone. Add
  `flex-wrap`, give the strip its own `overflow-x-auto`, or collapse to a
  `<select>` below `md`.
- [x] **H** `base.html:35-83` — every bar item except the breadcrumb is
  `shrink-0` (Home, Back, pill, badge up to `max-w-[10rem]`, role chip, Lock);
  the current-page title truncates to zero and the fixed items can exceed the
  viewport. Hide Home below `md` (Back suffices), cap the badge at ~`max-w-[6rem]`
  below `md`, or collapse the role chip.
- [x] **H** cloud `people.html:12` (and `:54` pending list) — the invite URL is
  unbroken `font-mono`; it overflows the card and forces horizontal page scroll,
  and "Send this link" has no copy affordance. Add `break-all` + a copy button.

## 2. Destructive actions missing the two-tap confirm

Route each through `partials/confirm_button.html` (or `hx-confirm` where noted),
with consequence wording in the confirm state.

- [x] **H** `product.html:63-66` — Delete product is one tap, 8px (`gap-touch`)
  from Copy. Confirm ("Confirm delete <name>") and/or separate it from Copy.
- [x] **H** `user.html:103-105` — Delete user is one tap, 8px from Disable.
  Confirm with "they will lose access" wording.
- [x] **M-H** cloud `admin_accounts.html:17-18` — Suspend is one tap at the row
  edge; it locks a real owner out of their fleet. Two-tap confirm.
- [x] **M** `partials/tests_hold_banner.html:42-45` — "Take over" seizes another
  tech's maintenance lease in one tap, while the milder "Take out of service" on
  the same card is two-tap. Confirm, and state what happens to the other session.
- [x] **M** `tests.html:35-37` — "Run all" is one tap: acquires the lease
  (SVC-102, payment withdrawn machine-wide) and runs self_test on every board.
  Two-tap confirm.
- [x] **M** `setup_codes.html:53-56` — "Done" permanently destroys the plaintext
  emergency codes in one tap, as a full-width bottom button where a scrolling
  thumb lands. Confirm with "codes can never be shown again".
- [x] **M** `devices.html:45-48` — "Forget" (forces re-enrollment / burns an
  emergency code) is one tap, 8px from "Mark shared/personal". At least `hx-confirm`.
- [x] **M** `partials/confirm_button.html:46-50` — red Confirm and Cancel sit
  `gap-2` (8px) apart. Widen to `gap-4`; keep Cancel left / Confirm right
  consistently.
- [x] **M** `partials/test_result_card.html:42-49` / `test_sale_result.html:37-44`
  — Pass (green) and Fail (red) are opposite verdicts 8px apart, recorded with no
  undo. Widen the gap or add undo on the recorded state.
- [x] **M** `inventory.html:8-17` — Collect Cash confirm says only "Confirm
  Collection": no expected amount, no "permanent record" consequence. Render
  "Confirm collection of ~$X expected cash — this records a permanent collection".
- [x] **M** cloud `admin_machines.html:56-58` — Unbind's confirm never states
  consequences (owner removed, techs and alert history deleted, claim hashes
  kept); "unbind" itself is jargon. One explanatory sentence near the control.
- [x] **L-M** cloud `admin_machines.html:12` — "Purge all unclaimed" confirm
  lacks consequence wording (gone forever?); the adjacent count helps.
- [x] **L** `partials/confirm_command.html:21-25` — shutdown/restart's first-tap
  button is the default `bg-slate-800`, identical to benign primaries; tone
  destructive commands' first-tap button.

## 3. Color-only / wrong-semantics state (colorblind safety)

- [x] **H** `partials/subsystem_live.html:16` — digital **off = `bg-red-600`**:
  a healthy idle board (agitator, heater, fan correctly off) shows a wall of red.
  Neutral slate/outline for off; keep red for fault states.
- [ ] **H** [deferred: /screen pin] `partials/screen_body.html:31-33` — kiosk subsystem health is 12px
  (`w-3 h-3`) color-only dots, invisible at distance. Large labeled tinted pills
  with a state word.
- [ ] **M** [deferred: /screen pin] `partials/screen_body.html:13-15` — an unavailable product kind uses
  `bg-slate-700`, identical to unknown; out-of-ice looks neutral at distance.
  Tint `ok == false` red/amber.
- [x] **M** `health_availability.html:26-28` — Ice/Water cards show state by dot
  color alone when ok. Print the word ("Available"/"Unavailable") like screen_body.
- [x] **M** `partials/status_fragment.html:19-22,113-116` — fault severity is
  chip background color only (red/orange/amber). Append the severity word or icon.
- [x] **M** cloud `fleet.html:10` — Online renders gray while
  `partials/admin_status_badge.html` renders Online green. Align on the admin badge.
- [x] **M** cloud `alerts.html:13` — owner-facing event-kind chips are always
  gray while `admin_alerts.html:21` colors the same kinds (raised=red,
  cleared=green, offline=amber). Reuse the admin coloring.

## 4. Hover-only information (`title=` is dead on touch)

- [x] **M** `base.html:79` — the context badge's full machine name lives only in
  `title=` past `max-w-[10rem]`. Surface the full label on the page it links to,
  or a tap state.
  _Done with no template change: /me already prints the untruncated `machine_label.display` ("role on name"), and the badge keeps its title._
- [x] **M** `health.html:18` — the cloud fingerprint's label exists only in
  `title="Cloud fingerprint"`; operators see an unlabeled mono string. Visible
  micro-label.
- [x] **M** `reports_period.html:43-46` — the disabled "Day" tab's explanation
  lives only in `title=`; on touch the gray tab just looks broken. Visible
  one-line note when range=all, or hide the tab there.
- [x] **M** cloud `admin_machines.html:50` — payment "Off" reason is only in
  `title=`; an admin on a phone can never see why. Render as a second line.
- [x] **L** `products.html:22-23` / `product.html:21-22` — lockout chip:
  `title="Locked out"` is hover-only and the fault code (e.g. CFG-101) is
  unexplained. Link the chip to `/health/faults`.
  _Done: product.html chip links; products.html chip is inside a card link (no nested anchors), so the dead title was dropped and the visible "locked · CODE" text kept._
- [x] **L** `health.html:14` — full commit only in `title=` (short form shown).
  _Left as is (task 3): short hash is the shown identity; full hash is non-essential._
- [ ] **L** cloud `partials/admin_overview.html:85` — full commit + build time
  only via `title=` (non-essential; short hash shown).
  _Left as is (task 3): non-essential, short hash shown._

## 5. Codes, fingerprints and share-links need copy/select affordances

One shared fix: a small copy-button partial (or `select-all` styling) used by all
of these. This is the biggest touch-flow gap across both apps.

- [x] **M** `setup.html:44`, `setup_codes.html:40`, `users_ownership.html:38` —
  the fingerprint must be transcribed into the cloud portal by eye. Copy button
  and/or `select-all`.
- [x] **M-H** `setup_codes.html:33-37` — 20 one-time codes, no copy-all, no
  print affordance; "Email these to me" only renders when email is configured
  (often not on a fresh machine). Copy-all button + print-friendly hint.
- [x] **M** `partials/codes_regenerate.html:36-42` — regenerated codes, same gap.
- [x] **M** `users_ownership.html:47,79` — link code / transfer code shown once
  as plain mono; the transfer code must be handed to another person. Copy button.
- [x] (Invite URL: covered in §1, `people.html`.)

## 6. Form ergonomics

- [x] **M** `product_catalog.html:25`, `product_form.html:38` — price inputs use
  `inputmode="numeric"`: iOS shows a keypad with **no decimal point**, so 2.50
  can't be typed. Use `inputmode="decimal"`, add `min="0"`.
- [x] **L-M** visible-PIN `type="text"` inputs (login/setup use `password`):
  `user.html:83-85`, `user_form.html:43`, `users_codes.html:22`,
  `users_ownership.html:51,60,83,95`. Switch to `type="password"` or document.
- [x] **M** `partials/keypad.html:51` — the only correction key is Clear (wipes
  the whole 4-8 digit PIN), 8px below the digits. Make it backspace (⌫), or add
  one.
- [x] **L-M** `partials/keypad.html:42-44` — tapping the visible PIN input pops
  the OS keyboard over the on-screen keypad. Consider `readonly` + keypad-only on
  coarse pointers; at minimum `maxlength="8"`.
- [x] **L** `partials/keypad.html:46-53` — digit keys are minimal 48px; a
  dedicated PIN pad reads better at 56-64px (`min-h-[56px]`). "Enter" sits 8px
  right of "0"; `gap-3` on the bottom row would help.
- [x] **M** cloud `claim.html:14,19` — both inputs are `px-3 py-2` ≈38px, below
  the 44px floor (claim is done on a phone at the machine). Bump to py-3/py-4.
  Add `autocomplete="one-time-code"` to the code (`:17-19`) and
  `autocapitalize="none" spellcheck="false"` to the fingerprint (`:12-14`);
  consider accepting input without dashes.
- [x] **M** shrunken `h-5 w-5` (20px) checkboxes overriding the global 24px
  default: `product_placement.html:30-31`, `product_form.html:72-73`,
  `settings_contacts.html:53-55,96-98`. Drop the size utilities; make the whole
  row a `<label>`.
- [x] **M** masked secrets never explained — `settings_payments.html:24-45`,
  `settings_comms.html:38,52-58`, `settings_mqtt.html:62`: nothing says "•••• =
  saved; leave as-is to keep, type to replace". One helper line per secret
  section.
- [x] **M** `product_catalog.html:40-42`, `product_form.html:60-62` — "Customer
  Button" is free-text `font-mono` with no help; a typo silently unmaps the
  product. `<select>` of channel ids from the vending board's capabilities
  (plus "None"), or at minimum helper text with valid ids.
- [x] **L** phone/number fields: `settings_contacts.html:35,78`,
  `settings_comms.html:62` → `type="tel"`; `settings_comms.html:28` (smtp_port),
  `settings_mqtt.html:44` (broker_port) → `min="1" max="65535"`.
- [x] **L** `settings_machine.html:41` — "Location notes" as single-line text;
  use a textarea like settings_web/settings_reports.
- [x] **L** `settings_web.html:34`, `settings_reports.html:55` — `touch-target`
  (display:flex) on a `<textarea>` with no vertical padding; drop it, add `py-2`.
- [x] **L** `setup.html:71-80` — PIN fields lack `maxlength="8"`/`minlength="4"`.
- [x] **L** `enroll.html:44-46` — add `maxlength` to the (otherwise exemplary)
  code input.
- [x] **L** `partials/test_result_card.html:39-40`,
  `test_sale_result.html:34-35` — note input has placeholder only, no label
  (placeholder vanishes on focus). Add visible or sr-only label with `for`.

## 7. Inert elements that look interactive / results that don't read

- [x] **M** `partials/subsystem_live.html:70-73` — command chips are inert
  `<span>`s in exact primary-button styling (`rounded-md bg-slate-800 text-white
  px-3 py-1`); techs tap them and get silence. Restyle as flat non-interactive
  chips, or link to `/tests/{name}`.
- [x] **M** `partials/command_result.html:1` — the whole partial is an unstyled
  `<p>`: success and failure look identical, unlike every other result card.
  Wrap in the shared toned-card pattern.
- [x] **L** `partials/inventory_row.html:22-24` — slot renders as a disabled
  mini `<input>` (invites dead taps; `inputmode` on a disabled input is dead
  weight). Plain text "Slot 3" like `products.html:26`.
- [x] **L** `partials/tile.html:30-41` — a `coming_soon` tile looks disabled but
  still navigates. Render as a non-anchor (or aria-disabled, no href).

## 8. Loader's phone flow (inventory)

- [x] **M** `partials/inventory_row.html:27-39` — the count + four `w-14` adjust
  buttons cluster is ~296px and doesn't wrap internally (borderline/overflow at
  360px inside card padding); `-1` and `+1` are adjacent at 8px so a restock tap
  can decrement. Split into −10/−1 … +1/+10 pairs with a wider center gap (count
  between them), allow 2×2 wrap on narrow screens.
- [x] **M** `partials/inventory_row.html:28-29` — no low-stock signal despite
  `LOW_STOCK_THRESHOLD = 3`. Amber at ≤3, red "0 — empty" at 0.
- [x] **L** `partials/inventory_row.html:28-29` — the count is a bare number with
  no visible label, above another bare number (slot). Label it ("in stock: 7").

## 9. Feedback placement, staleness and layout shift

- [x] **M** "Email this report" result renders at the **top** of the page while
  the button is below a long table (`reports_period.html:14-15` vs `:115-123`,
  same on the other report pages); on a phone the tap appears to do nothing.
  Render the notice adjacent to the button (as settings_comms does with
  `#test-email-result`).
- [x] **M** `home.html:20` — `#status-panel` polls `/status` every 1s and its
  height varies with the fault list, shifting the tile grid under a finger
  mid-tap. Give it a `min-h-*` for the tallest common state, or move variable
  fault detail behind the Health tile.
- [x] **M** `base.html:87-90` — `#ui-notice` is `absolute ... top-14` and
  overlays the first ~30px of `<main>` instead of pushing content down. Make it
  a static in-flow strip.
- [x] **M** `health_subsystem.html:50-51` — the 2s-polled live window has no
  updated-at stamp; a dead poll is indistinguishable from live data. Add one
  inside the polled fragment.
- [x] **M** `health_faults.html` — the fault list is static with no refresh
  button or timestamp, yet COM-10x faults auto-clear; an operator can act on a
  stale row. Add refresh/timestamp like /health/logs. (refresh only; no stamp — a live timestamp would break edge/cloud parity)
- [ ] **M** [deferred: /screen pin] `screen.html:12` — the kiosk's 5s poll has no clock/updated stamp; a
  frozen screen keeps showing stale "Payment Enabled". Render a server timestamp
  or an htmx-error offline banner.
- [x] **M** `partials/subsystem_live.html` — when `window.alive` is false,
  everything degrades to gray "—" chips with no "board offline" notice. Say it.
- [x] **L** `health_logs.html` — no fetched-at stamp next to Refresh.
- [x] **L** `tests.html:50` — `#run-all-result` can land below the fold during a
  long sequential run.

## 10. Plain-language and legibility

- [x] **M** `controls.html:16-24` — Restart/Reset/Shutdown have zero
  descriptions and the confirm just repeats the word; no hover-discovery exists
  on touch. One-line description per button.
- [x] **M** `health_faults.html:26` — fault age as raw minutes ("2880m ago").
  Use the `humanize_seconds` filter used elsewhere.
- [x] **M** `health.html:14-19` — build strip is `text-xs text-gray-400/500`
  mono (≈2.8:1 contrast) with unbroken `machine_id`/fingerprint tokens that can
  overflow 360px. `break-all`, bump size/contrast.
- [x] **M** `health_subsystem.html:25` — `font-mono` hardware id, no `break-all`
  → overflow at 360px.
- [x] **M** `product.html:25` — sku (up to 64 chars) has no `break-all` →
  horizontal page scroll. Add `break-all` (identity — don't truncate).
- [x] **M** `health_logs.html:16` — `<pre>` with 200-char `text-xs` lines and
  only `overflow-y-auto` → two-axis nested scrolling on phones. `whitespace-pre-wrap
  break-all` on phones.
- [x] **L** raw FSM states shown to operators (`interacting_with_user`):
  `partials/status_fragment.html:50,144`. Map to plain words.
  (`partials/screen_body.html:22` [deferred: /screen pin].)
- [x] **L** `partials/status_fragment.html:45,139` — payment-blocking reasons
  (the most actionable fact on the hero) are `text-xs text-gray-500`. Promote to
  `text-sm`, normal contrast.
- [x] **L** `partials/status_fragment.html:2-33` — "All Systems OK" can render
  above a non-empty Active-faults list (alert-class). Word it "OK — N warnings".
  (Wording added, but `views/health.py` currently marks the machine unhealthy
  whenever any fault is active, so the branch is defensive — it renders only if
  `is_healthy` ever tolerates alert-class faults.)
- [x] **L** key instructions at `text-xs`: "Watch the machine." + failure detail
  (`test_result_card.html:20,35`, `test_sale_result.html:30`), "Compare this
  amount" (`cash_collection_result.html`), "Contract mismatch" (`tests.html:23-26`),
  firmware/contract line (`health_subsystems.html:17`), availability explainer +
  snake_case row names (`health_availability.html:36-41`), "N failed"
  (`kpi_fragment.html`). Promote to `text-sm` where the info is essential.
- [x] **L** `health_faults.html:20-23` — safety/fulfillment/alert gate chips
  with no legend on this page (legend only on /health/availability).
- [x] **L** `tests_log.html:41` — failed run's fault_code shown raw with no
  FAULT_TABLE description.
- [ ] **L** [deferred: /screen pin] `partials/screen_body.html:38-44` — kiosk
  "Active faults" at 16px, code-first; bump size, lead with the description.
- [x] **L** raw ISO timestamps edge-wide: `devices.html:36` (last_seen),
  `users.html:27` / `users_ownership.html:42,74` (pending transfer), cloud
  `alerts.html` (`e.at`). Humanize. (Cloud alert timestamps were already
  pre-formatted "YYYY-MM-DD HH:MM UTC" by the routes; `friendly_time` passes
  them through unchanged, so only the edge pages visibly changed.)
- [x] **L** `setup.html:37-38` — `docker logs` jargon on an owner-facing
  first-run page.
- [x] **L** cloud `admin_accounts.html:5` — "Email: log-only" is insider jargon;
  say "not configured — emails are only logged".
- [x] **L** cloud `claim.html` — say *where* on the machine the fingerprint is
  shown (Health page / setup screen).
- [x] **L** `error.html:10-11` — button labeled "Back" goes to `/` (Home).
- [x] **L** `base.html:82` vs `me.html:42` — "Lock" vs dynamic logout label for
  the same action; "Lock" reads as "lock the machine". Pick one explicit label.

## 11. Undersized / indistinguishable tap targets

- [ ] **H** cloud `fleet.html:16,19` — "N alerts / 24 h" and "People" are bare
  `text-xs` links (~16px tall) with no underline, indistinguishable from the
  adjacent non-link spans. `touch-target` padding + link affordance.
- [ ] **M** cloud `fleet.html:6-25` — the machine row is a flex-wrap soup at
  360px and only the small "Select" button navigates. Make the whole card
  tappable (like edge `users.html` rows), metadata on a second line.
- [ ] **M** cloud `fleet.html:30` — "Claim a machine" (the primary action for a
  new user) is a bare text-sm gray link. Promote to a button, especially when
  the machine list is empty.
- [ ] **M** `base.html:54-56` — breadcrumb links are bare `text-sm` anchors
  (~20px tall) packed at `gap-1`. Give crumbs `py-2 px-1` / touch-target height.
- [ ] **M** `base.html:79` — `#context-badge` is `text-xs px-2 py-1` (~24px)
  with no `touch-target`, unlike every other bar control. Add it.
- [ ] **M** report range tabs `px-4 py-2.5` ≈40px tall (all reports pages, incl.
  `reports.html:15-25`) — below the shell's 44-48px convention. `py-3` /
  `min-h-[44px]`. (Wrapping: §1.)
- [ ] **M** `reports_product.html:43-45` — only the product-name text (~20px) is
  the link into the per-sku report. Make the whole row/first cell the tap target.
- [ ] **M** `me.html:17,21` — "Choose one" (/fleet) and "Admin dashboard"
  (/admin) are bare underline links — the page's only navigation in those
  states. Style as padded buttons/chips.

## 12. Consistency and small cleanups

- [ ] **M** `partials/tile.html:25,28` — Controls and Settings share a
  byte-identical gear icon on the primary nav. Distinct glyph for Controls.
- [ ] **L** Pass/Fail verdict form duplicated verbatim in
  `partials/test_result_card.html` and `partials/test_sale_result.html` —
  extract a shared partial.
- [ ] **L** error-notice roles: reports pages use `role="status"/"alert"`
  but settings/product `{% if error %}` paragraphs have none (e.g.
  `settings_machine.html:17-19`, `product_catalog.html:7-9`).
- [ ] **L** `partials/keypad.html:59-63,75-83` — back-off countdown stops at
  "Try again in 0 s"; swap the text at 0 ("You can try again now").
- [ ] **L** `settings_payments.html:47-49` — "PayPal is not configured" is a
  dead end (no inputs render when unconfigured). Render empty fields or say
  where configuration happens.
- [ ] **L** `settings_contacts.html:121` — tech email/phone line has no
  truncate/break; long email overflows at 360px.
- [ ] **L** `partials/tests_hold_banner.html:34` — `holder_name` `font-mono`
  with no `break-all`; a raw session id fallback overflows at 360px.
- [ ] **L** "Email this report" label doesn't say where it goes; rename to
  "Email me this report (CSV)".
- [ ] **L** `tests_subsystem.html:46-49` — actuator Run (one-tap, defensible
  behind the lease) is visually identical to harmless Automatic commands;
  differentiate.
- [ ] **L** `partials/test_run_all_table.html:12-33` — `px-5 py-3` padding
  guarantees horizontal scroll at 360px; `px-3` + hide the (often "—") Detail
  column on phones.
- [ ] **L** cloud `admin_machines.html:18-67` — 9-column table: consider a card
  layout under `md` or a sticky first column (overflow-x-auto already present).
- [ ] **L** cloud `partials/admin_overview.html:44-63` — Owners panel table has
  no overflow wrapper and no email truncation → horizontal overflow at 360px.
  `overflow-x-auto` or `truncate max-w-[12ch]` on the email cell.
- [ ] **L** cloud `fleet.html:18` — role as bare `text-xs` word where the edge
  uses role chips.
- [ ] **L** cloud `claim.html:21-22` — Claim (the page's only action) styled as
  a secondary border button where edge primaries are solid.
- [ ] **L** cloud `no_report.html` / `not_remote.html` — dead-end sentence; add
  a "Back to fleet" link.

---

Suggested order: §1 + the price `inputmode` fix (§6, first item) are small and
high-payoff; then §2 (confirms — mostly reusing `confirm_button.html`); then one
shared copy-button partial for §5; then §3/§4; the rest as cleanup passes.
Remember: any template change requires rebuilding and committing
`packages/ui/src/ice_ui/static/app.css` (Tailwind standalone CLI), and edge/cloud
parity pages must keep `tests/cloud/test_ui_parity.py` green.
