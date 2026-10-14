"""Guard tests for `tests/route_context.py` (Task 4, VMC-reduction tests
cleanup): the snapshot/restore module the autouse fixture in
`tests/conftest.py` uses to isolate every `ice_edge.web.context` global
between tests.
"""

from ice_common.config_model import ConfigModel
from ice_edge.app.machine import Machine
from tests import route_context
from ice_edge.web import context


def test_globals_covers_every_module_global():
    """The guard: adding a 14th global to context.py fails this test until
    it is added to route_context.GLOBALS too."""
    assert set(route_context.GLOBALS) == route_context.discoverable_globals()


def test_restore_puts_every_global_back():
    original_pending = getattr(context, "_pending_codes")
    snap = route_context.snapshot()

    sentinel = object()
    for name in route_context.GLOBALS:
        setattr(context, name, sentinel)

    route_context.restore(snap)

    for name in route_context.GLOBALS:
        assert getattr(context, name) == snap[name]
    assert getattr(context, "_pending_codes") is not original_pending


def test_set_machine_instance_pair_restored_together():
    """set_machine_instance couples vmc_instance and machine_instance;
    restore must not leave one stale relative to the other."""
    snap = route_context.snapshot()

    machine = Machine(config=ConfigModel())
    try:
        context.set_machine_instance(machine)
        assert context.machine_instance is machine
        assert context.vmc_instance is machine.vmc

        route_context.restore(snap)

        assert context.machine_instance == snap["machine_instance"]
        assert context.vmc_instance == snap["vmc_instance"]
    finally:
        machine.cancel_pending_tasks()
