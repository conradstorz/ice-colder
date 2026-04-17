# Dashboard Redesign — Design Spec
**Date:** 2026-04-17  
**Status:** Approved

---

## Context

The vending machine admin dashboard (`/`) is a monitoring tool used from a desktop browser. The primary use case is **monitoring at a glance** — open the page, confirm nothing is wrong, close it. The most important question to answer immediately is: **is anything wrong?**

The current dashboard is dark, flat, and unstructured — all sections have equal visual weight, the health signal is buried, and the layout is cramped.

---

## Goals

- Surface problems immediately without reading
- Clean modern aesthetic: light theme, card-based, generous whitespace (Vercel/Linear style)
- Keep all existing functionality — no features removed
- All HTMX polling behaviour preserved

---

## Theme & Typography

| Token | Value |
|-------|-------|
| Page background | `slate-50` |
| Card background | `white` |
| Card border | `border border-gray-200` |
| Card radius | `rounded-xl` |
| Card shadow | `shadow-sm` |
| Heading text | `gray-900` |
| Secondary text | `gray-500` |
| Accent / interactive | `blue-600` |
| Healthy status | `green-500` |
| Warning status | `amber-500` |
| Error status | `red-500` |

Font: existing `font-sans` system stack. Tailwind CDN unchanged.

---

## Layout (top to bottom)

### 1. Header

Full-width, above all cards. **Static** — rendered once on page load, no polling.

- **Left:** machine name (from `config.physical.name` or fallback "Vending Machine"), `text-lg font-semibold text-gray-900`
- **Right:** muted timestamp label `text-xs text-gray-400` — "Dashboard" or app version

The hero card (section 2) is the live health signal. No status pill in the header — that would require out-of-band HTMX swaps and adds fragility for little gain.

---

### 2. Hero Status Card

Full-width card. First thing the eye goes to.

**Healthy state:**
- White card, `border-l-4 border-green-500`
- Left section: large `● All Systems OK` in `green-600`, `text-xl font-semibold`
- Right section (inline grid): Machine State | Escrow | Last Payment Method

**Issue state:**
- Card background `bg-red-50`, `border-l-4 border-red-500`
- Left section: `⚠ Issues Detected` in `red-600`, `text-xl font-semibold`
- Below it: compact list of active issues (errors count, stale subsystems, temp exceedances)
- Right section: same status grid as healthy state

Refreshes every 1s via HTMX.

Health signal logic — the `/status` route must be updated to pass combined data:
- `issues = errors_24h > 0 OR any stale subsystem OR any temp exceedance`
- `errors_24h` comes from `event_recorder.get_summary(24)["errors"]` (already available in routes.py)
- `stale subsystems` and `temp exceedances` come from `health_monitor.get_summary()` (already available)
- If either `event_recorder` or `health_monitor` is `None`, those signals default to `False` (not an issue)
- The route passes a combined `health_signal: dict` with keys `is_healthy: bool`, `issues: list[str]`, plus the existing VMC status fields

---

### 3. KPI Stat Row

Four equal-width cards in a horizontal grid (`grid-cols-4 gap-4`).

| Card | Metric | Issue styling |
|------|--------|---------------|
| Money In | Sum of payments, last 24h | — |
| Products Out | Dispense count, last 24h | — |
| Errors | Error count, last 24h | `bg-red-50`, value in `red-600` if > 0 |
| Uptime | Uptime %, last 24h | value in `amber-600` if < 90% |

Each card layout:
- Top-right: small SVG icon (`text-gray-300`)
- Centre: `text-3xl font-bold text-gray-900` primary value
- Below: `text-sm text-gray-500` label
- Below that: average badge — `avg $8.20` in `text-xs text-gray-400`. If avg is `null`: `text-xs text-gray-300 italic` "no history yet"

Refreshes every 60s.

---

### 4. Activity Panel

A single card with a tab row at the top: `24 Hours | 7 Days | 30 Days`.

- Active tab: `border-b-2 border-blue-600 text-blue-600 font-medium`
- Inactive tabs: `text-gray-500 hover:text-gray-700`
- Tab switching is HTMX — each tab hits `/activity?period=24` (etc.) and swaps **only the table body** (`hx-target="#activity-table-body" hx-swap="innerHTML"`)
- The tab row and card chrome are static in `dashboard.html`; only the rows inside the table are swapped
- `/activity` route gains an optional `period` query param (int, default 24). Accepted values: 24, 168, 720

Each tab view is a clean two-column table:

| Row | Left | Right |
|-----|------|-------|
| Uptime | "Uptime" | `98.4%` · `avg 97.1%` |
| Money In | "Money In" | `$12.50` · `avg $8.20` |
| Products Out | "Products Out" | `7` · `avg 5.0` |
| Ice Cycles | "Ice Cycles" | `3` · `avg 2.5` |
| Errors | "Errors" | `2` · `avg 0.4` |
| Service Door | "Service Door Opens" | `1` · `avg 0.8` |
| Temp Issues | "Temp Issues" | `0` · `avg 0.1` |

- Rows with issues (errors > 0, temp_exceedances > 0): subtle `border-l-2 border-red-400 pl-2` accent on the row
- Average `null`: show `—` in `text-gray-300`
- "avg" values are muted inline, not stacked

**Route change:** `/activity` gains an optional `?period=` query param (24, 168, 720). If omitted, defaults to 24. The dashboard loads the 24h tab by default.

---

### 5. Content Panel (tabbed)

A card with three tabs: `Logs | Inventory | System Health`.

- Default tab: **Logs**
- Tab switching via HTMX, same targets as current implementation
- Each tab renders its existing partial, restyled to light theme

**Logs tab:** monospace block with dark inset background (`bg-gray-900 text-green-400 rounded-lg p-4 text-xs`) — terminal feel within the light page. Last 10 lines. Refreshes every 5s.

**Inventory tab:** existing inventory table partial, adapted to light-theme card rows.

**System Health tab:** existing health fragment partial, adapted to light theme.

---

### 6. Controls Bar

Slim bar at the very bottom, separated by a `border-t border-gray-200 mt-6 pt-4`.

- **Left:** `text-xs text-gray-400 uppercase tracking-wider` label "Machine Controls"
- **Right:** three buttons
  - `Restart` — `border border-blue-300 text-blue-600 hover:bg-blue-50` (outline)
  - `Reset` — `border border-amber-300 text-amber-600 hover:bg-amber-50` (outline)
  - `Shutdown` — `bg-red-600 text-white hover:bg-red-700` (solid, danger)
- Feedback message: appears inline to the left of the buttons on action

Controls are deliberately placed last — this is a monitoring dashboard.

---

## Files Changed

| File | Change |
|------|--------|
| `web_interface/templates/dashboard.html` | Full rewrite to new layout |
| `web_interface/templates/partials/status_fragment.html` | Rewrite for hero card (healthy/issue states) |
| `web_interface/templates/partials/activity_fragment.html` | Rewrite for tabbed layout |
| `web_interface/templates/partials/logs_fragment.html` | Restyle for terminal inset block |
| `web_interface/templates/partials/inventory_table.html` | Restyle for light theme |
| `web_interface/templates/partials/health_fragment.html` | Restyle for light theme |
| `web_interface/routes.py` | Add `period` query param to `/activity` endpoint |

---

## Out of Scope

- No new backend logic beyond the `/activity?period=` query param
- No changes to polling intervals
- No changes to HTMX swap behaviour
- No JS beyond what HTMX provides
- No changes to `event_recorder.py` or other services
