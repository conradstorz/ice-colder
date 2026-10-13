# Test suite review — TODO

Source: a review of `tests/` on 2026-10-13 by six review agents, with some items checked by hand.
Shared doc copy: https://claude.ai/code/artifact/af77f4a8-e9c8-471d-b989-10273df0f516

## How to use this file (for the implementing agent)

- Items are ranked by what regression slips through. Work section 1 first.
- **(verified)** means the item was checked by hand against the code. Every other item came from a review agent. Read the cited code and confirm the defect before fixing it. If the defect isn't real, tick the box and append `— rejected: <reason>`.
- When an item is done, change `- [ ]` to `- [x]` and append `— <commit sha>` or a one-line note. Commit this file alongside the fix.
- A fixed test must fail against the bug it guards. Break the production code temporarily, watch the test go red, then restore it.
- Lowering a number in `tests/private_access_budget.py` is fine. Raising one is not.
- Open decision: item 2.1 (real broker in CI, or keep the skip). Ask the user before doing it.

## 1. Tests that can't fail on the bug they guard

- [ ] 1.1 **(verified)** `tests/test_static_css.py:84`: `class_is_covered` is a bare substring match with no leading `.`. So `flex`, `hidden`, `border` and `p-2` count as present because of `display:flex` or `.p-20`. Fix: match a real selector boundary, e.g. the regex `\.<escaped>(?=[\s,:{.\[>])`. Add a test where a short real class is absent.
- [ ] 1.2 **(verified)** `tests/test_routes_health_pay104.py:464-551`: the concurrency test only asserts one row. `record_sale(idempotent=True)` (`services/event_recorder.py:489`) already guarantees that, so removing `_pay104_lock` stays green. Fix: assert `arrivals["n"] == 1`.
- [ ] 1.3 `tests/test_routes_health_pay104.py:416`: the discard 403 tests never check that the evidence snapshot file still exists. Fix: assert `session_path.exists()` and that PAY-104 is still active.
- [ ] 1.4 `tests/test_vmc_flows.py:2852` (and the `ValueError` at 2785): `pytest.raises(RuntimeError)` has no `match`, and any of four different RuntimeErrors satisfies it. Fix: add `match=`.
- [ ] 1.5 `tests/test_startup_policy.py:~270`: the recorder-failure test asserts only `state == "idle"`. Fix: assert on `vmc.active_faults()` (DATA-101 expected).
- [ ] 1.6 `tests/test_reports.py:881`: the empty-DB test loops over `period_rows` without asserting the list is non-empty. Fix: `assert len(period_rows) >= 30`.
- [ ] 1.7 `tests/test_display_controller.py:49`: `test_no_change_skips_publish` has no assertion and no MQTT client. Fix: delete it; the next test covers the behaviour.
- [ ] 1.8 `tests/test_routes_tests.py:798-818`: the allowlist 403 tests assert only the status code, not that the lease and dispatcher were left alone. Fix: assert `machine.lease.hold is None` and that the dispatcher saw no calls.
- [ ] 1.9 The 403 tests in the users, products, health and inventory route test files never check that state is unchanged. Examples: the user still exists, the product still exists, the fault is still active, the count is unchanged. Fix: add a post-condition to each test.
- [ ] 1.10 `tests/test_routes_settings.py:215`: the `form_field` parameter is unused, so the 4 cases are identical. Fix: post a masked placeholder for each field.
- [ ] 1.11 `tests/test_routes_settings.py:29-35`: the SMTP password is never checked for leaking into the page. POST responses are never checked for the newly submitted plaintext secret. Fix: use a distinctive SMTP default, and assert the secret is absent after each POST.
- [ ] 1.12 `tests/test_startup_dispensers.py:~89`: only proves the helper calls two mocks, not that `main()` calls `wire_dispenser_profiles`. Fix: assert on the real `machine.gate.profiles` after startup wiring.
- [ ] 1.13 `tests/test_routes_controls.py:140-215`: reset is never exercised from the `error` state, and `test_post_reset_with_no_vmc_...` never sets the VMC to None. Fix: drive the VMC into `error` and assert it is `idle` after the reset.
- [ ] 1.14 `tests/test_routes_products.py`, `test_route_tolerates_no_vmc_instance`: it sets `vmc_instance` to None, but the routes check `machine_instance`. Fix: set the machine to None instead.
- [ ] 1.15 `tests/test_routes_inventory_collect.py:114`: the permission test loops over allowed roles only, and `":" in resp.text` is always true. Fix: add a denied role, or drop the claim.
- [ ] 1.16 `tests/test_mailer.py:~58`: the "never blocks the loop" test doesn't check that the send ran on another thread. Fix: compare `threading.get_ident()`.
- [ ] 1.17 `tests/test_config_store.py:~44`: the rolling backup test checks only that `.bak` exists. Fix: assert `.bak` holds the previous content.
- [ ] 1.18 `tests/test_session_recovery.py:75-87`: the PAY-104 uncertain case asserts the evidence is not None, but not that it is unchanged. Fix: `store.load() == snap`.
- [ ] 1.19 `tests/test_machine.py:77`: the wiring test checks only `is not None`. Fix: use identity checks such as `machine.vmc.escrow is machine.escrow`.

## 2. CI and isolation holes

- [ ] 2.1 **(verified)** `tests/test_integration_e2e.py:72` and `tests/skip_policy.py:22`: the only full-stack money-path tests (sale, refund, jam lockout, overpay) skip in CI. There is no broker service, and the skip is allowlisted. Fix: add a mosquitto service to `.github/workflows/ci.yml` and remove the allowlist entry. **Ask the user first.**
- [ ] 2.2 `tests/test_integration_e2e.py:300, 378, 464, 538, 586, 629`: the teardowns catch `(CancelledError, Exception)` around `mqtt_task`, which hides a crashed client. Fix: catch only `CancelledError`.
- [ ] 2.3 **(verified)** `tests/conftest.py:34`: the autouse `isolated_config_path` fixture patches `CONFIG_PATH`, but `_config_path()` reads `ICE_COLDER_CONFIG` first (`services/config_store.py:43`). With that env var exported, tests write the real config. Fix: add `monkeypatch.delenv("ICE_COLDER_CONFIG", raising=False)` to the same fixture, and the same for `ICE_COLDER_DISPENSERS`.
- [ ] 2.4 `tests/browser/*.mjs` (23 fixed sleeps): the checks wait 600–1200 ms after a click without confirming the htmx swap happened. The Python wrappers don't check `returncode`. Fix: poll for a marker that only appears after the swap, and assert the return code.
- [ ] 2.5 `sleep(0)` is used as synchronisation at about 95 sites in `tests/test_vmc_flows.py` and `tests/test_vmc_dispense_profiles.py`, even though `settle()` exists. Fix: replace them with `settle()`.
- [ ] 2.6 `tests/test_vmc_flows.py`, `tests/test_vmc_dispense_profiles.py`, `tests/test_vmc_observers.py`: the bare `machine.cancel_pending_tasks()` at the end of a test is skipped when the test fails. Fix: use the `machine` fixture or try/finally.
- [ ] 2.7 `tests/test_no_private_access.py:92-94, 164-168`: the ratchet misses `getattr(obj, "_x")`, `f()._x` and `xs[0]._x`. A `# private:` marker exempts up to 3 lines, and its reason is never checked. Fix: scan with the AST, and require a non-empty reason.
- [ ] 2.8 `tests/test_vmc_complexity.py:~58`: only the class body and the file length are measured, so module-level helpers escape the method budgets. Fix: budget module-level functions too.
- [ ] 2.9 `tests/test_dispensers_example.py:32`, `tests/test_startup_dispensers.py:20`: the paths are relative to the current directory. Fix: anchor them on `Path(__file__).parent.parent`.
- [ ] 2.10 `tests/test_startup_dispensers.py:~118`: the compose check cuts the text at the next service name and ignores `docker/docker-compose.prod.yml`. Fix: parse the YAML and check both files.
- [ ] 2.11 `tests/test_event_recorder.py:1795-1850`, `tests/test_task_runner.py:105-170`: these assert on elapsed wall-clock time with margins of about 30 ms. Fix: use event gates or `FakeTaskRunner`.
- [ ] 2.12 `tests/test_simulator_vending.py:368+`, `tests/test_simulator_mdb.py:197`: they patch the global `asyncio.sleep`, and the MDB recover test never asserts the delays. Fix: patch the module's own reference and assert the delay arguments.
- [ ] 2.13 `tests/test_simulator_base.py:~975, ~1006`: `except (CancelledError, Exception): pass` around `sim.run()`. Fix: catch only the expected exceptions.
- [ ] 2.14 `tests/route_context.py:~88`: `discoverable_globals` skips callables and ALL_CAPS names, so a mutable ALL_CAPS dict escapes snapshot and restore. Fix: include mutable containers whatever their case.

## 3. Mirrored expectations and duplication

- [ ] 3.1 `tests/test_web_routes.py:437-505`, `tests/test_routes_tests.py:190, 858-895`: the expected 200 or 403 is derived from `ROLE_PERMISSIONS`, so a wrong grant in that table is mirrored. Fix: hard-code the expected roles per route, from the CLAUDE.md gate table.
- [ ] 3.2 `tests/test_web_routes.py:420-431`: `_MATRIX_ROUTES` covers 10 of the gate table's routes. There is no POST denial test for `/settings/*`, `/reports/email` or the owner-only `/users/*` routes. Fix: extend the matrix to every gated route, GET and POST.
- [ ] 3.3 `tests/test_routes_tests.py:909`: CSRF is tested on `/tests/end` only. Fix: parametrize over every `/tests` POST route.
- [ ] 3.4 `tests/test_reports.py:755, 825, 438-470`: the expected bucket keys are computed with `_floor_to_bucket` and `_next_bucket`, the same helpers under test. Fix: hard-code the ISO strings.
- [ ] 3.5 `tests/test_reports.py:360-390`: the production `tz=None` path is checked only for invariants, in the host's own timezone. CI runs in UTC, so it never sees DST. Fix: inject a synthetic zone for that path too.
- [ ] 3.6 `tests/test_contracts_vending.py:~40`: `fault_for_outcome` is checked against the same `OUTCOME_FAULTS` table it reads. Fix: delete it and keep the test that checks against the spec's literal values.
- [ ] 3.7 `tests/test_dispenser_gate.py:50-68`: `FakeFaults.clear_fault` reimplements the fault semantics, and no gate test uses the real `FaultService`. Fix: add one integration test with the real service.
- [ ] 3.8 `tests/test_integration_e2e.py`: about 40 lines of setup are repeated in 5+ tests. The dispense command is read with a single `__anext__()`, so nothing checks that it isn't published a second time. Fix: one async fixture, and assert exactly one dispense command.
- [ ] 3.9 The four `tests/test_*_browser.py` files repeat `_find_chrome`, `_free_port`, `live_server` and the back-off reset. Fix: move them into one shared module.
- [ ] 3.10 `tests/test_vmc_flows.py:70, 907`: `make_machine` and `make_machine2` overlap. `_vmc_with_profiles` and `FakeMQTT` are copied between the observers and dispense-profiles test files. Fix: consolidate them into `tests/fakes.py`.
- [ ] 3.11 `tests/test_simulator_ice_maker.py:~97`: 61 ticks are hard-coded to cross a 300 s boundary. Fix: derive the count from the simulator's constant.

## 4. Low-value cleanup

- [ ] 4.1 Assertions on log text: `tests/test_vmc_dispense_profiles.py:497, 724` (`"request_id" in r.message` is too generic), `tests/test_task_runner.py`, `tests/test_dispensers_cli.py:~66`. Assert on state or return values instead.
- [ ] 4.2 Generic substring asserts: `tests/test_web_routes.py:38, 73, 92, 735, 1016, 1304`; `tests/test_routes_tests_level.py:70-100` (`"Tests" in text`, and `find("<main")` with no check that it was found); `tests/test_routes_reports.py` (`"Reports" in text`). Target an element with `parse_elements` instead.
- [ ] 4.3 `tests/test_web_routes.py:1553`: accepts either 401 or 403. Pin one.
- [ ] 4.4 `tests/test_web_routes.py:131-164`: the idle-clock tests assert `!= before`. Use `> before`.
- [ ] 4.5 `tests/test_web_routes.py:733, 832`: dead `auth=client.auth` arguments left over from HTTP Basic auth.
- [ ] 4.6 `tests/test_web_routes.py:405-417, 915-921`: comment blocks describing deleted tests. Remove them.
- [ ] 4.7 `tests/test_vmc_fsm.py:112-122, 163-168`: `assert_called()` without checking the arguments or the call count.
- [ ] 4.8 `tests/test_notifier.py:10-14`: a dead `_send_email` stub with a comment that isn't true. No test checks that an alert is sent again after the cooldown expires.
- [ ] 4.9 `tests/test_dispensers_example.py:~72-200`: regex checks over the generated TOML, mostly redundant with the byte-identical test.
- [ ] 4.10 `tests/test_feedback_assets.py:~58-85`, `tests/test_templating.py`, `tests/test_fsm_control.py`: checks on source text. Restart and shutdown are untested.
- [ ] 4.11 `tests/test_levels.py:27`: the hard-coded number "31 levels".

## 5. Claims checked and rejected (do not re-file)

- The `HX-Request: ""` CSRF tests are valid: `require_htmx` rejects anything but `"true"` (`web_interface/context.py:189`).
- `TestScreenTemplatesUnchanged` does run in CI: the checkout uses `fetch-depth: 0`, and an unexpected skip would fail the skip guard.
- `test_get_routes_do_not_need_header` is weak, but it does test something: a GET succeeding without the header is the property it claims.
- Already documented in CLAUDE.md, so not re-filed: the private-access regex can't tell comments from code, the 0.3 s e2e sleep is deliberate, and the CSS test doesn't check for unused classes.
