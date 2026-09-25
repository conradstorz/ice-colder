# Dashboard v2 — Program Overview and Delivery Plan

**Date:** 2026-09-25
**Status:** Approved for planning; implementation on hold until the owner releases each part
**Covers:** the four design specs dated 2026-09-25

## 1. What this refactor is

The dashboard today is one desktop page behind a single HTTP Basic admin
password: a hero status card, four KPIs, an activity table, a tabbed panel
(logs, inventory, health, machine info) and three control buttons. It works,
and it is the wrong shape for where the machine is going: a 7-inch
touchscreen mounted inside the cabinet, used by whoever is standing there,
plus the owner's phone from anywhere.

v2 turns it into a touch-first shell. Home shows operational status and a
grid of tiles. Each tile opens a sub-menu that owns the whole screen except a
bar with Home, Back, a live health pill, and Lock. Who is standing at the
tablet matters, so the shell is built on named users with roles. Two areas
that do not exist today, sales reports and system tests, get real backing.

The work is split into four specs. Each is independently shippable and each
depends on the ones before it.

| Part | Spec | Delivers | Depends on |
|---|---|---|---|
| 1 | `specs/2026-09-25-roles-and-access-design.md` | Named users with `owner` / `secretary` / `tech` / `loader` roles; PIN login; per-device trust with a one-time emailed OTP or an offline emergency code; setup wizard and ownership transfer; exponential back-off on every secret; a `Permission` table every route declares | nothing |
| 2 | `specs/2026-09-25-dashboard-v2-shell-design.md` | `base.html` shell, level tree with real URLs, eight tiles filtered by permission, vendored Tailwind and HTMX for offline use, landscape / portrait / phone layouts, Settings edit forms, `routes.py` split by area | part 1 |
| 3 | `specs/2026-09-25-sales-reports-design.md` | Per-sale records with FIFO payment-method attribution, never-pruned `sales` table, cash collection log, reports by period / product / method, email with CSV, scheduled daily or weekly summary | parts 1, 2 |
| 4 | `specs/2026-09-25-system-tests-design.md` | Shared MQTT command/ack channel for every subsystem, `CommandDispatcher`, maintenance lease `SVC-102`, Tests level with automatic and operator-verdict tests, end-to-end simulated sale, test log, simulator support | parts 1, 2, 3 |

The order is fixed. Part 2 removes the routes and templates part 1's
login pages first land in, so part 1 ships against the old dashboard and
part 2 moves it. Parts 3 and 4 fill tiles part 2 leaves as placeholders.

## 2. Program goals

These are the goals the four specs exist to meet. Every part is judged
against the ones that apply to it, and the program is done only when all
hold on the simulator stack (`docker compose up` with the three ESP32
simulators and mosquitto).

1. **A stranger cannot use the dashboard.** No default credential exists.
   The wizard claims the machine once and only with a setup code that
   exists solely at the machine; after that every request carries a session
   tied to a trusted device. A lost owner has no software recovery.
2. **The person at the tablet sees only what their role allows**, and the
   server enforces it regardless of what the page shows.
3. **The tablet works with no internet at the machine**: styling, scripts,
   login, and emergency-code enrollment all function offline. Only email
   needs the network.
4. **Status is never hidden.** Home shows health, state, and KPIs; every
   deeper level shows the health pill; a fault is one tap from anywhere.
5. **Every level has a URL, a Back that goes to its parent, and a Home.**
   Reloading lands where you were.
6. **Nothing the machine does today stops working.** Selling, faults,
   refunds, availability gates, the customer `/screen` page, MQTT
   contracts with existing firmware (until part 4's deliberate version
   bump), and all existing tests, retargeted where routes moved.
7. **Sales are recorded per sale, forever**, with SKU, price, and how it was
   paid, and the owner can get them on screen or by email without asking a
   developer.
8. **A tech can prove a subsystem works without making a sale**, and the
   machine cannot sell while they do it.
9. **The dashboard is the last thing to go down.** Auth, reports, and tests
   never block the FSM or the MQTT client. A corrupt access file degrades
   the dashboard only. A corrupt events database stops startup today; part 3
   changes the recorder to set the file aside, start fresh, and raise an
   alert fault, so from part 3 on it degrades history, not the machine.

## 3. Delivery model

The owner has standing instructions: implementation is sub-agent driven.
Model policy for this program: Opus dispatches and executes each part,
Sonnet and Haiku do the hands-on work underneath. Applied at three levels.

### 3.1 Levels of agent

**Program orchestrator** (Opus; this session, or a fresh session given
this document). Owns the order in §1, opens one branch and one pull request per
part, and never edits code itself. For each part it:

1. Invokes `superpowers:writing-plans` against that part's spec to produce
   `docs/superpowers/plans/2026-09-25-<part>-plan.md`: tasks of one to three
   files each, every task with its tests named first, in dependency order,
   with the spec section it implements cited.
2. Invokes `superpowers:subagent-driven-development` to execute that plan.
3. When the plan is complete, invokes `superpowers:finishing-a-development-branch`,
   pushes, opens the pull request, and hands the Copilot review to a review
   agent (§3.4).
4. Stops and reports to the owner before starting the next part.

**Part executor** (Opus; the agent `subagent-driven-development` runs as).
Reads the plan and the spec. For each task it dispatches one implementer
and one reviewer, in order, and does not move to the next task until the
reviewer passes it. It keeps a running list of deviations from the spec and
puts them in the pull-request description. It splits a task further when
an implementer reports it is larger than one sitting; a task that touches
more than three files or needs more than one new test file is split before
dispatch, not after.

**Implementer** (Sonnet for anything touching the FSM, auth, contracts, or
the recorder; Haiku for templates, restyling, moving partials, docs, and
test retargeting; never Opus). Receives exactly: the task text, the spec section, the
files it may touch, and the test it must make pass. Works test-first:
writes the failing test, runs it, implements, runs `uv run pytest` for the
affected test file, then `ruff check --fix .` and `ruff format .`. Reports
what changed, test counts, and anything the spec did not cover. Never
widens scope; an adjacent bug is reported, not fixed.

**Reviewer** (Sonnet). Receives the task, the diff, and the spec section.
Checks the behavior against the spec, not the style, runs the full
`uv run pytest`, and returns pass or a list of concrete defects. A task
with defects goes back to a fresh implementer with the defect list.

**Waves (parts 2–4).** From part 2 on, the plan groups tasks into numbered
waves and the executor dispatches a whole wave at once: every implementer in
the wave in parallel (one agent call per task, sent together), then every
reviewer in parallel, then one full `uv run pytest` and `ruff check .` before
the next wave. A reviewer failure sends only its own task back to a fresh
implementer; the rest of the wave proceeds. A wave holds only tasks whose
"files it may touch" sets are pairwise disjoint and whose tests do not share
a fixture another task in the same wave is editing. Anything touching a
shared file — route registration, `conftest.py`, `main.py`, `CLAUDE.md`,
`base.html`, `contracts/common.py` — is a serial step of its own between
waves, dispatched one implementer and one reviewer at a time exactly as part
1 was. Part 1 ran entirely serially and is not retrofitted. The model policy
above is unchanged by this.

### 3.2 Increment size

A task is the unit of hand-off. The rule of thumb: one service or one
route module or one template group, its tests, and nothing else. Examples
from part 1: "`Backoff` class with clock injection and its tests" is one
task; "login page, keypad partial, and `POST /login`" is one task;
"`AccessStore` persistence" and "`AccessStore` sessions and OTPs" are two.
Part 2's route split is one task per area module. Part 3's FIFO escrow
change is its own task before anything touches the recorder. Part 4's
contract models land before the dispatcher, the dispatcher before the VMC
hold, the hold before any route.

**A task states interfaces and behavior, not implementations.** Part 1's plan
embedded full function bodies, and implementers transcribed them: an unguarded
`save()` was copied into eleven `AccessStore` mutators, an enrollment token was
bound to a client key the response had not yet set, and a test was written that
never reached the branch its name promised. Every plan from part 2 on gives, per
task: the spec section, the files it may touch, the interface (exact names,
signatures, routes, template variables — these are contracts other tasks
depend on and must be spelled out), the behavior in prose, and the tests to
write first. A code block appears only for a wire format, a shell command, or a
genuinely non-obvious algorithm. The implementer writes the code; its reviewer
checks the result against the spec and the named interfaces, never against a
snippet in the plan. Part 1's tasks 11–21 were rewritten this way mid-flight
and produced measurably cleaner work: no task needed a fix round afterwards.

### 3.3 Branches and pull requests

One branch per part from `main`: `feat/roles-and-access`,
`feat/dashboard-v2-shell`, `feat/sales-reports`, `feat/system-tests`. Each
opens a pull request whose description links the spec and the plan, lists
deviations, and states which program goals (§2) it claims. CI (ruff,
pytest, compose config) must pass. A part is merged before the next branch
is cut, so every part builds on reviewed code.

### 3.4 Review loop

GitHub Copilot reviews every pull request. A review agent (Sonnet,
dispatched by the part executor) reads
each comment under `superpowers:receiving-code-review`: it verifies the
claim against the code and the spec before acting, fixes what is real in a
commit that names the comment, and replies with a reason when a comment is
wrong or out of scope. Prior parts of this project resolved Copilot rounds
this way (`fix: Copilot review — …` commits); the same convention holds.
The owner merges.

### 3.5 When an agent stops and asks

- The spec is silent on something that changes behavior (not naming or
  layout). The executor records an assumption in the deviations list and
  continues if the assumption is reversible; otherwise it stops.
- A test that passed before the part fails and the fix would change
  behavior outside the part's spec.
- Any change to `config.json` schema beyond what the specs list, any new
  runtime dependency, or any change to an MQTT contract not in part 4.
- The owner asked to be told: before each part starts, and before merge.

## 4. Per-part acceptance

The executor runs these before declaring a part done, on the compose
simulator stack, and records the result in the pull request.

**Part 1.** Fresh `data/` boots into setup mode and the setup code appears
in the log and on the display; the wizard refuses a wrong code and creates
an owner with the right one, then shows 20 codes; a second browser enrolls
a tech by emergency code with no SMTP configured; a wrong PIN five times in
a row waits 16 s before the sixth attempt, and twenty wrong PINs from
untrusted addresses slow every untrusted address for that user while the
trusted tablet is unaffected; a loader gets 403 on price and 200 on slot;
transfer leaves the old owner in control until the new owner completes the
wizard with the transfer code, then walks the retained users;
`data/access.json` is mode `0600`; every existing route test passes with
the new fixtures. Goals 1, 2, 3 (offline enrollment), 9.

**Part 2.** The tablet layout at 1024×600 and 600×1024 and a 400 px phone
show Home with the right tiles per role; every level in the spec's URL
table renders with bar, crumbs, and working Back, and the bar updates on
boosted navigation without a reload; the machine tablet with
the network cable unplugged is fully styled; `/screen` is unchanged; the
CSS class test passes; no route from the old dashboard answers. Goals 3,
4, 5, 6.

**Part 3.** A simulated sale paid with `cash_bill` then `card` appears in
by-method with the FIFO split and the sale row is on disk before the FSM
returns to idle; a failed vend restores the same shares; a loader's cash
collection shows the expected cash counting the simulator's cash methods;
a deliberately corrupted `events.db` is set aside at startup and the
machine still boots;
reports render for owner and secretary and 403 elsewhere; the emailed
report arrives with a CSV attachment through a local SMTP stub; the
scheduler sends one catch-up on restart and no backlog; `prune` leaves
`sales` alone. Goals 7, 9.

**Part 4.** Run-all on three alive simulators returns ping and self-test
results; injecting a simulator fault makes that subsystem's self-test fail
the matching check; the Tests UI never offers `refund` or `payment/enable`;
a dispense test on slot 3 raises `SVC-102`, the MDB simulator receives
payment disable, and a credit injected during the lease is refunded rather
than escrowed; a duplicated request id does not fire the motor twice; a
second tech cannot release the first tech's lease mid-run; the simulated
sale exercises the FSM and records no sale; production `cmd/dispense`
still vends with the unmodified simulator build; the log shows the
verdicts. Goals 6 (production topics unchanged; the contract bump is
additive and documented), 8, 9.

**Program.** All four merged; a fresh clone with `.env` from
`.env.example` and `docker compose up` reaches the wizard; every goal in §2
holds; `CLAUDE.md` describes the shell, the access store, the sales tables,
and the command channel accurately.

## 5. Risks and how the plan handles them

- **Part 2 deletes what part 1 built on.** Part 1 lands login and Users
  pages inside the old dashboard on purpose; part 2's plan includes a task
  that moves them, so nothing is rebuilt twice.
- **Access file is a single point of lockout.** `data/access.json` is
  bind-mounted with the rest of `data/`; the Users level shows the unused
  emergency-code count so the owner is warned before the pool runs dry.
  Backup guidance goes in `README.md` in part 1.
- **Tailwind build drift.** The CSS class test in part 2 fails CI when a
  template uses a class the committed `app.css` lacks.
- **Contract bump strands real firmware.** Part 4 leaves every production
  topic (`cmd/dispense`, `cmd/payment/*`) untouched and adds the command
  channel beside them; the ack shape is a superset of today's. The health
  tab already flags version mismatches, and a subsystem on the old contract
  simply advertises no tests.
- **Setup and transfer windows.** The wizard is gated by a code that lives
  only at the machine, and a transfer keeps the old owner in control until
  the new one is fully established, so there is no moment when a remote
  client can claim the machine.
- **Refactor fatigue across four pull requests.** Each part has its own
  acceptance list and merges on its own; nothing waits on the whole
  program to be useful.

## 6. Where things live

- Specs: `docs/superpowers/specs/2026-09-25-*-design.md`
- Plans, once written: `docs/superpowers/plans/2026-09-25-<part>-plan.md`
- This document: `docs/superpowers/plans/2026-09-25-dashboard-v2-program.md`
- Brainstorm mockups from the design session: `.superpowers/brainstorm/`
  (ignored by git)
