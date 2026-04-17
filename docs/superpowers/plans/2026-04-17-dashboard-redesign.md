# Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Redesign the vending machine admin dashboard to a clean, light-theme, monitoring-first layout that immediately surfaces health issues.

**Architecture:** A static `dashboard.html` skeleton holds the page chrome (header, card wrappers, tab rows, controls bar). HTMX targets are named `#status-panel`, `#kpi-panel`, `#activity-panel`, and `#content-body`. Each region is populated by a dedicated FastAPI endpoint returning an HTML partial. The `/status` endpoint is enhanced to derive a health signal from `event_recorder` + `health_monitor`. A new `/kpi` endpoint serves four 24h stat cards. The `/activity` endpoint gains a `?period=` query param and returns a full self-contained card (including its own tab buttons) so active tab state is rendered server-side.

**Tech Stack:** FastAPI, Jinja2, HTMX 1.9.10, Tailwind CSS 3.4.1 (CDN), SQLite (via EventRecorder), pytest + FastAPI TestClient

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Modify | `web_interface/routes.py` | Add `/kpi`, update `/activity` (period param), update `/status` (health signal) |
| Rewrite | `web_interface/templates/dashboard.html` | New page skeleton |
| Rewrite | `web_interface/templates/partials/status_fragment.html` | Hero health card |
| Create  | `web_interface/templates/partials/kpi_fragment.html` | 4 KPI stat cards |
| Rewrite | `web_interface/templates/partials/activity_fragment.html` | Tabbed activity card |
| Rewrite | `web_interface/templates/partials/logs_fragment.html` | Self-refreshing terminal block |
| Modify  | `web_interface/templates/partials/inventory_table.html` | Light theme + fix target ID |
| Modify  | `web_interface/templates/partials/inventory_edit_form.html` | Fix target ID |
| Modify  | `web_interface/templates/partials/inventory_add_form.html` | Fix target ID |
| Modify  | `web_interface/templates/partials/health_fragment.html` | Light theme |
| Modify  | `web_interface/templates/partials/machine_info.html` | Light theme |
| Modify  | `tests/test_web_routes.py` | Tests for new endpoints and behaviour |

---

## Task 1: Update routes.py

**Files:**
- Modify: `web_interface/routes.py`

The three route changes are:
1. `/status` — compute `is_healthy` + `issues` list from `event_recorder` and `health_monitor`, pass to template
2. `/activity` — accept `period: int` query param (default 24, accepted: 24/168/720), pass single `summary` + `average` instead of dicts
3. `/kpi` — new endpoint, returns `kpi_fragment.html` with 24h summary + average

- [ ] **Step 1: Add `Query` import and update the `/status` route**

Open `web_interface/routes.py`. The existing import line is:
```python
from fastapi import APIRouter, FastAPI, Form, Request
```
Change it to:
```python
from fastapi import APIRouter, FastAPI, Form, Query, Request
```

Then replace the entire `status_fragment` function (currently lines ~114–122) with:
```python
@router.get("/status", response_class=HTMLResponse)
async def status_fragment(request: Request):
    if not vmc_instance:
        return HTMLResponse(
            '<div class="bg-red-50 rounded-xl border border-red-200 shadow-sm p-5">'
            '<p class="text-red-600 font-semibold">VMC not initialized</p></div>'
        )

    status = vmc_instance.get_status()
    issues: list[str] = []

    if event_recorder:
        errors_24h = event_recorder.get_summary(24)["errors"]
        if errors_24h > 0:
            issues.append(f"{errors_24h} error{'s' if errors_24h != 1 else ''} in last 24h")

    if health_monitor:
        health = health_monitor.get_summary()
        stale = [name for name, sub in health["subsystems"].items() if sub["stale"]]
        if stale:
            issues.append(f"Stale subsystems: {', '.join(stale)}")
        out_of_range = [loc for loc, temp in health["temperatures"].items() if not temp["in_range"]]
        if out_of_range:
            issues.append(f"Temp issues: {', '.join(out_of_range)}")

    return templates.TemplateResponse("partials/status_fragment.html", {
        "request": request,
        "status": status,
        "is_healthy": len(issues) == 0,
        "issues": issues,
    })
```

- [ ] **Step 2: Update the `/activity` route**

Replace the entire `activity_fragment` function with:
```python
@router.get("/activity", response_class=HTMLResponse)
async def activity_fragment(request: Request, period: int = Query(default=24)):
    if not event_recorder:
        return HTMLResponse(
            '<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">'
            '<p class="text-gray-400 text-sm">Activity data not available yet.</p></div>'
        )
    if period not in (24, 168, 720):
        period = 24
    summary = event_recorder.get_summary(period)
    average = event_recorder.get_historical_average(period)
    return templates.TemplateResponse("partials/activity_fragment.html", {
        "request": request,
        "period": period,
        "summary": summary,
        "average": average,
    })
```

- [ ] **Step 3: Add the `/kpi` route**

Add this function directly after the updated `activity_fragment` function:
```python
@router.get("/kpi", response_class=HTMLResponse)
async def kpi_fragment(request: Request):
    summary = event_recorder.get_summary(24) if event_recorder else None
    average = event_recorder.get_historical_average(24) if event_recorder else None
    return templates.TemplateResponse("partials/kpi_fragment.html", {
        "request": request,
        "summary": summary,
        "average": average,
    })
```

- [ ] **Step 4: Commit**

```bash
git add web_interface/routes.py
git commit -m "feat: update /status health signal, /activity period param, add /kpi endpoint"
```

---

## Task 2: Rewrite dashboard.html

**Files:**
- Rewrite: `web_interface/templates/dashboard.html`

The new skeleton is entirely static HTML — no Jinja2 variables. HTMX does all dynamic loading. Key IDs: `#status-panel`, `#kpi-panel`, `#activity-panel`, `#content-body`, `#feedback`.

The content panel uses static tab buttons (no active-state tracking needed — the activity panel handles its own active state via full-card swaps). Logs auto-refresh is handled inside `logs_fragment.html` itself (self-refreshing pattern), not via a trigger on `#content-body`.

- [ ] **Step 1: Replace the entire file**

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Vending Machine Admin</title>
    <script src="https://unpkg.com/htmx.org@1.9.10"></script>
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@3.4.1/dist/tailwind.min.css" rel="stylesheet">
</head>
<body class="bg-slate-50 text-gray-900 font-sans min-h-screen">
<div class="max-w-5xl mx-auto px-6 py-8 space-y-4">

    <!-- Header -->
    <div class="flex items-center justify-between">
        <h1 class="text-xl font-semibold text-gray-900">Vending Machine Admin</h1>
        <span class="text-xs text-gray-400">Dashboard</span>
    </div>

    <!-- Hero Status (1s refresh) -->
    <div id="status-panel"
         hx-get="/status"
         hx-trigger="load, every 1s"
         hx-swap="innerHTML">
        <div class="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
            <div class="flex">
                <div class="w-1 bg-gray-200 flex-shrink-0"></div>
                <div class="p-5 text-gray-400 text-sm">Loading status...</div>
            </div>
        </div>
    </div>

    <!-- KPI Stat Row (60s refresh) — grid lives here, cards are injected -->
    <div id="kpi-panel"
         class="grid grid-cols-4 gap-4"
         hx-get="/kpi"
         hx-trigger="load, every 60s"
         hx-swap="innerHTML">
        <div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5 h-28 animate-pulse"></div>
        <div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5 h-28 animate-pulse"></div>
        <div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5 h-28 animate-pulse"></div>
        <div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5 h-28 animate-pulse"></div>
    </div>

    <!-- Activity Panel (60s refresh, full-card swap) -->
    <div id="activity-panel"
         hx-get="/activity?period=24"
         hx-trigger="load, every 60s"
         hx-swap="innerHTML">
        <div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5 text-gray-400 text-sm">
            Loading activity...
        </div>
    </div>

    <!-- Content Panel — static chrome, dynamic body -->
    <div class="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        <div class="flex border-b border-gray-200 px-1 pt-1">
            <button
                hx-get="/logs"
                hx-target="#content-body"
                hx-swap="innerHTML"
                class="px-4 py-2.5 text-sm font-medium text-gray-500 hover:text-gray-700 border-b-2 border-transparent hover:border-gray-300 -mb-px transition-colors">
                Logs
            </button>
            <button
                hx-get="/inventory"
                hx-target="#content-body"
                hx-swap="innerHTML"
                class="px-4 py-2.5 text-sm font-medium text-gray-500 hover:text-gray-700 border-b-2 border-transparent hover:border-gray-300 -mb-px transition-colors">
                Inventory
            </button>
            <button
                hx-get="/health"
                hx-target="#content-body"
                hx-swap="innerHTML"
                class="px-4 py-2.5 text-sm font-medium text-gray-500 hover:text-gray-700 border-b-2 border-transparent hover:border-gray-300 -mb-px transition-colors">
                System Health
            </button>
            <button
                hx-get="/config/machine"
                hx-target="#content-body"
                hx-swap="innerHTML"
                class="px-4 py-2.5 text-sm font-medium text-gray-500 hover:text-gray-700 border-b-2 border-transparent hover:border-gray-300 -mb-px transition-colors">
                Machine Info
            </button>
        </div>
        <div id="content-body"
             class="p-5"
             hx-get="/logs"
             hx-trigger="load"
             hx-swap="innerHTML">
            <div class="text-gray-400 text-sm">Loading...</div>
        </div>
    </div>

    <!-- Controls Bar -->
    <div class="flex items-center justify-between pt-2 pb-4 border-t border-gray-200">
        <span class="text-xs text-gray-400 uppercase tracking-wider font-medium">Machine Controls</span>
        <div class="flex items-center gap-3">
            <span id="feedback" class="text-sm text-gray-600"></span>
            <button
                hx-post="/action/restart"
                hx-target="#feedback"
                hx-swap="innerHTML"
                class="border border-blue-200 text-blue-600 hover:bg-blue-50 px-4 py-1.5 rounded-lg text-sm font-medium transition-colors">
                Restart
            </button>
            <button
                hx-post="/action/reset"
                hx-target="#feedback"
                hx-swap="innerHTML"
                class="border border-amber-200 text-amber-600 hover:bg-amber-50 px-4 py-1.5 rounded-lg text-sm font-medium transition-colors">
                Reset
            </button>
            <button
                hx-post="/action/shutdown"
                hx-target="#feedback"
                hx-swap="innerHTML"
                class="bg-red-600 hover:bg-red-700 text-white px-4 py-1.5 rounded-lg text-sm font-medium transition-colors">
                Shutdown
            </button>
        </div>
    </div>

</div>
</body>
</html>
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/dashboard.html
git commit -m "feat: new dashboard.html skeleton — light theme, hero/kpi/activity/content/controls layout"
```

---

## Task 3: Rewrite status_fragment.html (Hero Card)

**Files:**
- Rewrite: `web_interface/templates/partials/status_fragment.html`

Uses a colored left bar (a 1-unit wide `div` inside a `flex`) for the accent — this is more reliable than Tailwind's `border-l-{color}` in all browsers.

- [ ] **Step 1: Replace the entire file**

```html
{# web_interface/templates/partials/status_fragment.html #}
{% if is_healthy %}
<div class="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
  <div class="flex">
    <div class="w-1 bg-green-500 flex-shrink-0"></div>
    <div class="flex items-start justify-between flex-1 p-5">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-green-500 flex-shrink-0"></span>
          <span class="text-base font-semibold text-green-700">All Systems OK</span>
        </div>
        <p class="text-sm text-gray-400 mt-1">Machine is operating normally</p>
      </div>
      <div class="flex gap-8 text-sm text-right ml-8">
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">State</div>
          <div class="font-medium text-gray-900">{{ status.state }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">Escrow</div>
          <div class="font-medium text-gray-900">${{ "%.2f"|format(status.credit_escrow) }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">Last Payment</div>
          <div class="font-medium text-gray-900">{{ status.last_payment_method or "—" }}</div>
        </div>
      </div>
    </div>
  </div>
</div>
{% else %}
<div class="bg-red-50 rounded-xl border border-red-200 shadow-sm overflow-hidden">
  <div class="flex">
    <div class="w-1 bg-red-500 flex-shrink-0"></div>
    <div class="flex items-start justify-between flex-1 p-5">
      <div>
        <div class="flex items-center gap-2">
          <span class="w-2.5 h-2.5 rounded-full bg-red-500 flex-shrink-0"></span>
          <span class="text-base font-semibold text-red-700">Issues Detected</span>
        </div>
        <ul class="mt-2 space-y-0.5">
          {% for issue in issues %}
          <li class="text-sm text-red-600">{{ issue }}</li>
          {% endfor %}
        </ul>
      </div>
      <div class="flex gap-8 text-sm text-right ml-8">
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">State</div>
          <div class="font-medium text-gray-900">{{ status.state }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">Escrow</div>
          <div class="font-medium text-gray-900">${{ "%.2f"|format(status.credit_escrow) }}</div>
        </div>
        <div>
          <div class="text-xs text-gray-400 uppercase tracking-wide mb-0.5">Last Payment</div>
          <div class="font-medium text-gray-900">{{ status.last_payment_method or "—" }}</div>
        </div>
      </div>
    </div>
  </div>
</div>
{% endif %}
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/status_fragment.html
git commit -m "feat: rewrite status_fragment as hero health card with green/red signal"
```

---

## Task 4: Create kpi_fragment.html

**Files:**
- Create: `web_interface/templates/partials/kpi_fragment.html`

The fragment returns 4 sibling `<div>` elements — no wrapping element. The parent `#kpi-panel` has `grid grid-cols-4 gap-4` so these become grid cells automatically.

Each card uses an SVG icon in the top-right corner. Icons are inline SVG using `currentColor` so they inherit text color.

- [ ] **Step 1: Create the file**

```html
{# web_interface/templates/partials/kpi_fragment.html #}
{# Returns 4 sibling divs — parent #kpi-panel provides the grid wrapper #}

{% if summary is none %}
  {# No recorder: show 4 muted placeholder cards #}
  {% for label in ["Money In (24h)", "Products Out (24h)", "Errors (24h)", "Uptime (24h)"] %}
  <div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
    <div class="text-xs text-gray-400 uppercase tracking-wide mb-1">{{ label }}</div>
    <div class="text-3xl font-bold text-gray-200">—</div>
    <div class="text-xs text-gray-300 mt-1">no data</div>
  </div>
  {% endfor %}
{% else %}

{# Money In #}
<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
  <div class="flex items-start justify-between">
    <div class="text-xs text-gray-400 uppercase tracking-wide">Money In (24h)</div>
    <svg class="w-4 h-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
    </svg>
  </div>
  <div class="text-3xl font-bold text-gray-900 mt-2">${{ "%.2f"|format(summary.money_in) }}</div>
  <div class="text-xs text-gray-400 mt-1">
    {% if average and average.money_in is not none %}
      avg ${{ "%.2f"|format(average.money_in) }}
    {% else %}
      <span class="text-gray-300">no history yet</span>
    {% endif %}
  </div>
</div>

{# Products Out #}
<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
  <div class="flex items-start justify-between">
    <div class="text-xs text-gray-400 uppercase tracking-wide">Products Out (24h)</div>
    <svg class="w-4 h-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/>
    </svg>
  </div>
  <div class="text-3xl font-bold text-gray-900 mt-2">{{ summary.products_out }}</div>
  <div class="text-xs text-gray-400 mt-1">
    {% if average and average.products_out is not none %}
      avg {{ average.products_out }}
    {% else %}
      <span class="text-gray-300">no history yet</span>
    {% endif %}
  </div>
</div>

{# Errors #}
{% set has_errors = summary.errors > 0 %}
<div class="{{ 'bg-red-50 border-red-200' if has_errors else 'bg-white border-gray-200' }} rounded-xl border shadow-sm p-5">
  <div class="flex items-start justify-between">
    <div class="text-xs text-gray-400 uppercase tracking-wide">Errors (24h)</div>
    <svg class="w-4 h-4 {{ 'text-red-400' if has_errors else 'text-gray-300' }}" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"/>
    </svg>
  </div>
  <div class="text-3xl font-bold {{ 'text-red-600' if has_errors else 'text-gray-900' }} mt-2">{{ summary.errors }}</div>
  <div class="text-xs text-gray-400 mt-1">
    {% if average and average.errors is not none %}
      avg {{ average.errors }}
    {% else %}
      <span class="text-gray-300">no history yet</span>
    {% endif %}
  </div>
</div>

{# Uptime #}
{% set low_uptime = summary.uptime_pct < 90 %}
<div class="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
  <div class="flex items-start justify-between">
    <div class="text-xs text-gray-400 uppercase tracking-wide">Uptime (24h)</div>
    <svg class="w-4 h-4 text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2"
            d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/>
    </svg>
  </div>
  <div class="text-3xl font-bold {{ 'text-amber-600' if low_uptime else 'text-gray-900' }} mt-2">
    {{ "%.1f"|format(summary.uptime_pct) }}%
  </div>
  <div class="text-xs text-gray-400 mt-1">
    {% if average and average.uptime_pct is not none %}
      avg {{ "%.1f"|format(average.uptime_pct) }}%
    {% else %}
      <span class="text-gray-300">no history yet</span>
    {% endif %}
  </div>
</div>

{% endif %}
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/kpi_fragment.html
git commit -m "feat: add kpi_fragment.html — 4 stat cards for /kpi endpoint"
```

---

## Task 5: Rewrite activity_fragment.html

**Files:**
- Rewrite: `web_interface/templates/partials/activity_fragment.html`

Returns the full activity card including tab buttons. Tab buttons target `#activity-panel` so clicking a tab swaps the entire card (enabling server-side active tab rendering). The auto-refresh on `#activity-panel` always uses `?period=24` as default.

- [ ] **Step 1: Replace the entire file**

```html
{# web_interface/templates/partials/activity_fragment.html #}
{# Full card including tab row — tab buttons target #activity-panel for server-side active state #}
{% set period_labels = {24: "24 Hours", 168: "7 Days", 720: "30 Days"} %}
{% set s = summary %}
{% set a = average %}

<div class="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">

  {# Tab row #}
  <div class="flex border-b border-gray-200 px-1 pt-1">
    {% for p, label in [(24, "24 Hours"), (168, "7 Days"), (720, "30 Days")] %}
    <button
      hx-get="/activity?period={{ p }}"
      hx-target="#activity-panel"
      hx-swap="innerHTML"
      class="px-4 py-2.5 text-sm font-medium -mb-px transition-colors
             {% if p == period %}border-b-2 border-blue-600 text-blue-600
             {% else %}text-gray-500 hover:text-gray-700 border-b-2 border-transparent hover:border-gray-300{% endif %}">
      {{ label }}
    </button>
    {% endfor %}
  </div>

  {# Data table #}
  <table class="w-full text-sm">
    <tbody class="divide-y divide-gray-100">

      {# Uptime #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Uptime</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium {{ 'text-amber-600' if s.uptime_pct < 90 else 'text-gray-900' }}">
            {{ "%.1f"|format(s.uptime_pct) }}%
          </span>
          {% if a.uptime_pct is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ "%.1f"|format(a.uptime_pct) }}%</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Money In #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Money In</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium text-gray-900">${{ "%.2f"|format(s.money_in) }}</span>
          {% if a.money_in is not none %}
            <span class="ml-2 text-xs text-gray-400">avg ${{ "%.2f"|format(a.money_in) }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Products Out #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Products Out</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium text-gray-900">{{ s.products_out }}</span>
          {% if a.products_out is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ a.products_out }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Ice Cycles #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Ice Cycles</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium text-gray-900">{{ s.ice_cycles }}</span>
          {% if a.ice_cycles is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ a.ice_cycles }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Errors #}
      <tr class="{{ 'bg-red-50' if s.errors > 0 else '' }}">
        <td class="px-5 py-3 text-gray-500">Errors</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium {{ 'text-red-600' if s.errors > 0 else 'text-gray-900' }}">
            {{ s.errors }}
          </span>
          {% if a.errors is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ a.errors }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Service Door #}
      <tr>
        <td class="px-5 py-3 text-gray-500">Service Door Opens</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium text-gray-900">{{ s.service_door_opens }}</span>
          {% if a.service_door_opens is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ a.service_door_opens }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

      {# Temp Issues #}
      <tr class="{{ 'bg-amber-50' if s.temp_exceedances > 0 else '' }}">
        <td class="px-5 py-3 text-gray-500">Temp Issues</td>
        <td class="px-5 py-3 text-right">
          <span class="font-medium {{ 'text-amber-600' if s.temp_exceedances > 0 else 'text-gray-900' }}">
            {{ s.temp_exceedances }}
          </span>
          {% if a.temp_exceedances is not none %}
            <span class="ml-2 text-xs text-gray-400">avg {{ a.temp_exceedances }}</span>
          {% else %}
            <span class="ml-2 text-xs text-gray-300">avg —</span>
          {% endif %}
        </td>
      </tr>

    </tbody>
  </table>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/activity_fragment.html
git commit -m "feat: rewrite activity_fragment as tabbed card with server-side active tab state"
```

---

## Task 6: Restyle logs_fragment.html

**Files:**
- Rewrite: `web_interface/templates/partials/logs_fragment.html`

Uses the self-refreshing pattern: the outer `<div>` has `hx-get="/logs" hx-trigger="every 5s" hx-target="#content-body" hx-swap="innerHTML"`. When in the DOM it polls every 5s; when replaced by another tab's content, the polling div is removed and polling stops automatically. HTMX cleans up interval triggers when elements leave the DOM.

- [ ] **Step 1: Replace the entire file**

```html
{# web_interface/templates/partials/logs_fragment.html #}
{# Self-refreshing: polling lives inside this fragment, stops when tab is switched #}
<div hx-get="/logs"
     hx-trigger="every 5s"
     hx-target="#content-body"
     hx-swap="innerHTML">
  <pre class="bg-gray-950 text-green-400 font-mono text-xs rounded-lg p-4 overflow-y-auto h-64 leading-relaxed">{% for line in logs %}{{ line[:120] }}{% if line|length > 120 %}…{% endif %}
{% endfor %}</pre>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/logs_fragment.html
git commit -m "feat: restyle logs_fragment — terminal block with self-refreshing pattern"
```

---

## Task 7: Restyle inventory_table.html and fix target ID

**Files:**
- Modify: `web_interface/templates/partials/inventory_table.html`

The old template references `#inventory-box`. In the new dashboard, the content area is `#content-body`. Update all `hx-target` values. Also restyle for light theme — the table already uses `bg-white text-black` but needs Tailwind cleanup and card styling to match the new design system.

- [ ] **Step 1: Replace the entire file**

```html
{# web_interface/templates/partials/inventory_table.html #}
<div>
  <div class="flex items-center justify-between mb-4">
    <h2 class="text-base font-semibold text-gray-900">Products &amp; Inventory</h2>
    <button
      hx-get="/inventory/new"
      hx-target="#content-body"
      hx-swap="innerHTML"
      class="bg-green-600 hover:bg-green-700 text-white px-3 py-1.5 rounded-lg text-sm font-medium transition-colors">
      Add Product
    </button>
  </div>

  <table class="w-full text-sm">
    <thead>
      <tr class="border-b border-gray-200 text-xs text-gray-400 uppercase tracking-wide">
        <th class="pb-2 text-left font-medium">SKU</th>
        <th class="pb-2 text-left font-medium">Name</th>
        <th class="pb-2 text-left font-medium">Price</th>
        <th class="pb-2 text-left font-medium">Inventory</th>
        <th class="pb-2 text-left font-medium">Actions</th>
      </tr>
    </thead>
    <tbody class="divide-y divide-gray-100">
      {% for product in products %}
      <tr class="hover:bg-gray-50">
        <td class="py-3 font-mono text-gray-600 text-xs">{{ product.sku }}</td>
        <td class="py-3 text-gray-900">{{ product.name }}</td>
        <td class="py-3 text-gray-900">${{ "%.2f"|format(product.price) }}</td>
        <td class="py-3 text-gray-900">{{ product.inventory_count }}</td>
        <td class="py-3">
          <div class="flex gap-2">
            <button
              hx-get="/inventory/edit/{{ product.sku }}"
              hx-target="#content-body"
              hx-swap="innerHTML"
              class="border border-gray-200 hover:bg-gray-50 text-gray-600 px-2.5 py-1 rounded text-xs font-medium transition-colors">
              Edit
            </button>
            <button
              hx-get="/inventory/copy/{{ product.sku }}"
              hx-target="#content-body"
              hx-swap="innerHTML"
              class="border border-gray-200 hover:bg-gray-50 text-gray-600 px-2.5 py-1 rounded text-xs font-medium transition-colors">
              Copy
            </button>
          </div>
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/inventory_table.html
git commit -m "feat: restyle inventory_table — light theme, fix hx-target to #content-body"
```

---

## Task 8: Fix target ID in form partials

**Files:**
- Modify: `web_interface/templates/partials/inventory_edit_form.html`
- Modify: `web_interface/templates/partials/inventory_add_form.html`

Both forms use `hx-target="#inventory-box"` which no longer exists. Change to `#content-body`. Also restyle for consistency.

- [ ] **Step 1: Replace inventory_edit_form.html**

```html
{# web_interface/templates/partials/inventory_edit_form.html #}
<form
  hx-post="/inventory/update/{{ product.sku }}"
  hx-target="#content-body"
  hx-swap="innerHTML"
  class="space-y-4 max-w-md">

  <h2 class="text-base font-semibold text-gray-900">Edit Product</h2>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">SKU</label>
    <input type="text" value="{{ product.sku }}" disabled
           class="w-full px-3 py-2 bg-gray-100 border border-gray-200 rounded-lg text-sm text-gray-500 font-mono">
  </div>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Name</label>
    <input type="text" name="name" value="{{ product.name }}"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Price (USD)</label>
    <input type="number" name="price" value="{{ product.price }}" step="0.01"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Inventory Count</label>
    <input type="number" name="inventory_count" value="{{ product.inventory_count }}"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div class="flex gap-3 pt-2">
    <button type="submit"
            class="bg-blue-600 hover:bg-blue-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors">
      Save Changes
    </button>
    <button type="button"
            hx-get="/inventory"
            hx-target="#content-body"
            hx-swap="innerHTML"
            class="border border-gray-200 hover:bg-gray-50 text-gray-600 px-4 py-2 rounded-lg text-sm font-medium transition-colors">
      Cancel
    </button>
  </div>
</form>
```

- [ ] **Step 2: Replace inventory_add_form.html**

```html
{# web_interface/templates/partials/inventory_add_form.html #}
<form
  hx-post="/inventory/add"
  hx-target="#content-body"
  hx-swap="innerHTML"
  class="space-y-4 max-w-md">

  <h2 class="text-base font-semibold text-gray-900">{{ "Copy Product" if mode == "copy" else "Add Product" }}</h2>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">SKU</label>
    <input type="text" name="sku" value="{{ product.sku }}"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 font-mono focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Name</label>
    <input type="text" name="name" value="{{ product.name }}"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Price (USD)</label>
    <input type="number" name="price" value="{{ product.price }}" step="0.01"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div>
    <label class="block text-xs font-medium text-gray-500 uppercase tracking-wide mb-1">Inventory Count</label>
    <input type="number" name="inventory_count" value="{{ product.inventory_count }}"
           class="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500">
  </div>

  <div class="flex gap-3 pt-2">
    <button type="submit"
            class="bg-green-600 hover:bg-green-700 text-white px-4 py-2 rounded-lg text-sm font-medium transition-colors">
      {{ "Copy Product" if mode == "copy" else "Add Product" }}
    </button>
    <button type="button"
            hx-get="/inventory"
            hx-target="#content-body"
            hx-swap="innerHTML"
            class="border border-gray-200 hover:bg-gray-50 text-gray-600 px-4 py-2 rounded-lg text-sm font-medium transition-colors">
      Cancel
    </button>
  </div>
</form>
```

- [ ] **Step 3: Commit**

```bash
git add web_interface/templates/partials/inventory_edit_form.html web_interface/templates/partials/inventory_add_form.html
git commit -m "feat: restyle inventory forms — light theme, fix hx-target to #content-body"
```

---

## Task 9: Restyle health_fragment.html

**Files:**
- Modify: `web_interface/templates/partials/health_fragment.html`

The existing template uses `bg-gray-800` dark theme. Update to light theme with clean table styling matching the design system.

- [ ] **Step 1: Replace the entire file**

```html
{# web_interface/templates/partials/health_fragment.html #}
<div class="space-y-5">

  <h2 class="text-base font-semibold text-gray-900">System Health</h2>

  <div class="flex items-center gap-3">
    <span class="w-2.5 h-2.5 rounded-full flex-shrink-0
                 {{ 'bg-green-500' if health.mqtt_connected else 'bg-red-500' }}"></span>
    <span class="text-sm text-gray-700">
      MQTT: {{ 'Connected' if health.mqtt_connected else 'Disconnected' }}
    </span>
  </div>

  <div class="flex items-center gap-3">
    <span class="w-2.5 h-2.5 rounded-full flex-shrink-0
                 {{ 'bg-red-500' if health.vmc_state == 'error' else 'bg-green-500' }}"></span>
    <span class="text-sm text-gray-700">VMC State: {{ health.vmc_state }}</span>
  </div>

  {% if health.subsystems %}
  <div>
    <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Subsystems</h3>
    <table class="w-full text-sm">
      <thead>
        <tr class="border-b border-gray-200 text-xs text-gray-400 uppercase tracking-wide">
          <th class="pb-2 text-left font-medium">Name</th>
          <th class="pb-2 text-left font-medium">Last Seen</th>
          <th class="pb-2 text-left font-medium">Status</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for name, sub in health.subsystems.items() %}
        <tr>
          <td class="py-2.5 text-gray-700 font-mono text-xs">{{ name }}</td>
          <td class="py-2.5 text-gray-500">{{ sub.seconds_since_seen }}s ago</td>
          <td class="py-2.5">
            {% if sub.stale %}
              <span class="text-red-600 font-medium">Stale</span>
            {% elif sub.alive %}
              <span class="text-green-600 font-medium">OK</span>
            {% else %}
              <span class="text-gray-400">Never seen</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {% if health.temperatures %}
  <div>
    <h3 class="text-xs font-medium text-gray-400 uppercase tracking-wide mb-2">Temperatures</h3>
    <table class="w-full text-sm">
      <thead>
        <tr class="border-b border-gray-200 text-xs text-gray-400 uppercase tracking-wide">
          <th class="pb-2 text-left font-medium">Location</th>
          <th class="pb-2 text-left font-medium">Value</th>
          <th class="pb-2 text-left font-medium">Status</th>
        </tr>
      </thead>
      <tbody class="divide-y divide-gray-100">
        {% for loc, temp in health.temperatures.items() %}
        <tr>
          <td class="py-2.5 text-gray-700">{{ loc }}</td>
          <td class="py-2.5 text-gray-900 font-medium">{{ temp.value }}°C</td>
          <td class="py-2.5">
            {% if temp.in_range %}
              <span class="text-green-600 font-medium">OK</span>
            {% else %}
              <span class="text-red-600 font-medium">Out of range</span>
            {% endif %}
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% endif %}

  {% if not health.subsystems and not health.temperatures %}
  <p class="text-sm text-gray-400">No subsystems reporting yet.</p>
  {% endif %}

</div>
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/health_fragment.html
git commit -m "feat: restyle health_fragment — light theme, clean table layout"
```

---

## Task 10: Restyle machine_info.html

**Files:**
- Modify: `web_interface/templates/partials/machine_info.html`

- [ ] **Step 1: Replace the entire file**

```html
{# web_interface/templates/partials/machine_info.html #}
<div class="space-y-4">
  <h2 class="text-base font-semibold text-gray-900">Machine Information</h2>

  <dl class="space-y-3 text-sm">
    <div class="flex justify-between">
      <dt class="text-gray-500">Common Name</dt>
      <dd class="font-mono text-gray-900">{{ details.common_name }}</dd>
    </div>
    <div class="flex justify-between border-t border-gray-100 pt-3">
      <dt class="text-gray-500">Serial Number</dt>
      <dd class="font-mono text-gray-900">{{ details.serial_number }}</dd>
    </div>
    <div class="flex justify-between border-t border-gray-100 pt-3">
      <dt class="text-gray-500">Location Address</dt>
      <dd class="text-gray-900 text-right max-w-xs">{{ details.location.address }}</dd>
    </div>
    <div class="flex justify-between border-t border-gray-100 pt-3">
      <dt class="text-gray-500">Location Notes</dt>
      <dd class="text-gray-900 text-right max-w-xs">{{ details.location.notes or "—" }}</dd>
    </div>
    <div class="flex justify-between border-t border-gray-100 pt-3">
      <dt class="text-gray-500">Products Configured</dt>
      <dd class="text-gray-900">{{ details.product_count }}</dd>
    </div>
  </dl>
</div>
```

- [ ] **Step 2: Commit**

```bash
git add web_interface/templates/partials/machine_info.html
git commit -m "feat: restyle machine_info — light theme, definition list layout"
```

---

## Task 11: Update tests

**Files:**
- Modify: `tests/test_web_routes.py`

Add tests for the new `/kpi` endpoint and the `period` param on `/activity`. Update the existing `TestActivityEndpoint` class.

- [ ] **Step 1: Add KPI and activity period tests to the file**

Add a new `TestKpiEndpoint` class and update `TestActivityEndpoint` — append both after the existing `TestActivityEndpoint` class (around line 114):

```python
class TestKpiEndpoint:
    def test_kpi_returns_200(self, client):
        response = client.get("/kpi")
        assert response.status_code == 200

    def test_kpi_without_recorder_returns_placeholder(self, client):
        # No event_recorder set on the fixture — should return placeholder cards
        response = client.get("/kpi")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_kpi_with_recorder(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r
        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)
        try:
            response = client.get("/kpi")
            assert response.status_code == 200
        finally:
            r.set_event_recorder(None)


class TestActivityPeriodParam:
    def test_activity_default_period(self, client):
        response = client.get("/activity")
        assert response.status_code == 200

    def test_activity_period_168(self, client):
        response = client.get("/activity?period=168")
        assert response.status_code == 200

    def test_activity_period_720(self, client):
        response = client.get("/activity?period=720")
        assert response.status_code == 200

    def test_activity_invalid_period_falls_back_to_24(self, client):
        # Invalid period values should fall back to 24 without error
        response = client.get("/activity?period=99")
        assert response.status_code == 200


class TestStatusHealthSignal:
    def test_status_is_healthy_without_recorder_or_monitor(self, client):
        # When neither event_recorder nor health_monitor is set, no issues → healthy
        response = client.get("/status")
        assert response.status_code == 200
        assert "All Systems OK" in response.text

    def test_status_with_recorder_no_errors(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r
        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        r.set_event_recorder(recorder)
        try:
            response = client.get("/status")
            assert response.status_code == 200
            assert "All Systems OK" in response.text
        finally:
            r.set_event_recorder(None)

    def test_status_with_recorder_has_errors(self, client, tmp_path):
        from services.event_recorder import EventRecorder
        from web_interface import routes as r
        recorder = EventRecorder(db_path=str(tmp_path / "test.db"))
        recorder.record("error")
        r.set_event_recorder(recorder)
        try:
            response = client.get("/status")
            assert response.status_code == 200
            assert "Issues Detected" in response.text
        finally:
            r.set_event_recorder(None)
```

- [ ] **Step 2: Run all tests**

```bash
uv run pytest tests/test_web_routes.py -v
```

Expected: all tests pass. If `TestStatusHealthSignal::test_status_is_healthy_without_recorder_or_monitor` fails with "Issues Detected", the `/status` route is not defaulting `is_healthy=True` when both `event_recorder` and `health_monitor` are None — re-check the route logic in Task 1.

- [ ] **Step 3: Commit**

```bash
git add tests/test_web_routes.py
git commit -m "test: add tests for /kpi, /activity?period=, and /status health signal"
```

---

## Verification

After all tasks are complete:

- [ ] Run the full test suite: `uv run pytest -v`
- [ ] Start the app: `uv run python main.py`
- [ ] Open `http://localhost:8000` and verify:
  - Page background is light (`slate-50`)
  - Hero card shows green bar + "All Systems OK" (or red if issues exist)
  - Four KPI cards render in a row
  - Activity panel shows "24 Hours" tab as active by default; clicking "7 Days" / "30 Days" switches data
  - Logs tab shows terminal block; switching to Inventory tab stops log polling; switching back resumes it
  - Inventory edit/add forms submit correctly and return to inventory list
  - Restart/Reset/Shutdown feedback appears inline next to buttons
