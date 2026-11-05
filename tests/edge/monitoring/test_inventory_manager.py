# tests/test_inventory_manager.py
"""Tests for ice_edge/monitoring/inventory_manager.py — persistent inventory tracking."""

import json
import threading

import pytest
from ice_common.config_model import Product
from ice_edge.monitoring.inventory_manager import InventoryManager


@pytest.fixture
def tmp_inventory(tmp_path):
    """Return the path to a temporary inventory.json."""
    return tmp_path / "inventory.json"


def _products():
    return [
        Product(
            sku="ICE-SM",
            name="Small Ice",
            price_cents=200,
            track_inventory=True,
            inventory_count=10,
        ),
        Product(
            sku="ICE-LG",
            name="Large Ice",
            price_cents=350,
            track_inventory=True,
            inventory_count=5,
        ),
        Product(
            sku="WATER",
            name="Water",
            price_cents=150,
            track_inventory=False,
            inventory_count=0,
        ),
    ]


class TestInitialization:
    def test_seeds_from_config(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.get_count("ICE-SM") == 10
        assert inv.get_count("ICE-LG") == 5
        assert inv.get_count("WATER") == 0

    def test_creates_file(self, tmp_inventory):
        InventoryManager(_products(), path=tmp_inventory)
        assert tmp_inventory.exists()
        data = json.loads(tmp_inventory.read_text())
        assert data["ICE-SM"] == 10

    def test_loads_from_existing_file(self, tmp_inventory):
        # Pre-populate with different counts
        tmp_inventory.write_text(json.dumps({"ICE-SM": 3, "ICE-LG": 1}))
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.get_count("ICE-SM") == 3
        assert inv.get_count("ICE-LG") == 1
        # New product not in file gets seeded from config
        assert inv.get_count("WATER") == 0

    def test_survives_corrupt_file(self, tmp_inventory):
        tmp_inventory.write_text("not json")
        inv = InventoryManager(_products(), path=tmp_inventory)
        # Falls back to config defaults
        assert inv.get_count("ICE-SM") == 10


class TestTracking:
    def test_is_tracked(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_tracked("ICE-SM") is True
        assert inv.is_tracked("WATER") is False

    def test_is_available_tracked_with_stock(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_available("ICE-SM") is True

    def test_is_available_tracked_no_stock(self, tmp_inventory):
        tmp_inventory.write_text(json.dumps({"ICE-SM": 0, "ICE-LG": 5, "WATER": 0}))
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_available("ICE-SM") is False

    def test_is_available_untracked_always_true(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        assert inv.is_available("WATER") is True

    def test_unknown_sku_not_available(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        # Unknown SKU defaults to not tracked → available
        assert inv.is_available("UNKNOWN") is True


class TestDecrement:
    def test_decrement_reduces_count(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.decrement("ICE-SM")
        assert inv.get_count("ICE-SM") == 9

    def test_decrement_persists_to_file(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.decrement("ICE-SM")
        # Reload from file
        data = json.loads(tmp_inventory.read_text())
        assert data["ICE-SM"] == 9

    def test_decrement_does_not_go_negative(self, tmp_inventory):
        tmp_inventory.write_text(json.dumps({"ICE-SM": 0, "ICE-LG": 5, "WATER": 0}))
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.decrement("ICE-SM")
        assert inv.get_count("ICE-SM") == 0


class TestSetAndAdd:
    def test_set_count(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.set_count("ICE-SM", 50)
        assert inv.get_count("ICE-SM") == 50

    def test_add_sku(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        inv.add_sku("NEW-SKU", 20, tracked=True)
        assert inv.get_count("NEW-SKU") == 20
        assert inv.is_tracked("NEW-SKU") is True

    def test_get_all(self, tmp_inventory):
        inv = InventoryManager(_products(), path=tmp_inventory)
        counts = inv.get_all()
        assert counts == {"ICE-SM": 10, "ICE-LG": 5, "WATER": 0}


class TestRemoveSku:
    def test_remove_sku_deletes_and_persists(self, tmp_path):
        path = tmp_path / "inv.json"
        inv = InventoryManager([], path=path)
        inv.add_sku("X-1", 5, tracked=True)
        inv.remove_sku("X-1")
        assert inv.get_count("X-1") == 0
        assert inv.is_tracked("X-1") is False
        reloaded = InventoryManager([], path=path)
        assert "X-1" not in reloaded.get_all()

    def test_remove_sku_unknown_is_harmless(self, tmp_path):
        inv = InventoryManager([], path=tmp_path / "inv.json")
        inv.remove_sku("NOPE")  # must not raise


def test_save_is_thread_safe_under_concurrent_callers(tmp_inventory):
    """_save() writes a fixed .tmp path via tmp-write-then-os.replace, and is
    now reachable both from the event loop (admin routes calling
    set_count/add_sku/remove_sku) and from a worker thread (save_async()).
    Two threads hammering _save() concurrently must never corrupt the file
    or race the tmp file out from under each other."""
    inv = InventoryManager(_products(), path=tmp_inventory)

    def hammer():
        for _ in range(50):
            inv._save()

    threads = [threading.Thread(target=hammer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads(tmp_inventory.read_text())
    assert data == inv.get_all()


async def test_decrement_without_persist_then_save_async(tmp_path):
    from ice_common.config_model import Product
    from ice_edge.monitoring.inventory_manager import InventoryManager

    path = tmp_path / "inventory.json"
    inv = InventoryManager(
        [Product(sku="A", track_inventory=True, inventory_count=3)], path=path
    )
    inv.decrement("A", persist=False)
    assert json.loads(path.read_text())["A"] == 3  # not yet written
    await inv.save_async()
    assert json.loads(path.read_text())["A"] == 2


class TestSaveObservers:
    def test_failed_save_calls_on_failed_then_good_save_calls_on_ok(self, tmp_path):
        good = tmp_path / "inventory.json"
        inv = InventoryManager(_products(), path=good)
        failed: list[int] = []
        ok: list[int] = []
        inv.set_save_observers(lambda: failed.append(1), lambda: ok.append(1))

        # A directory occupying the temp-file path makes open(tmp, "w") fail
        # on every platform.
        (tmp_path / "inventory.json.tmp").mkdir()
        inv.set_count("ICE-SM", 4)
        assert failed == [1]
        assert ok == []

        (tmp_path / "inventory.json.tmp").rmdir()
        inv.set_count("ICE-SM", 5)
        inv.set_count("ICE-SM", 6)
        assert failed == [1]
        assert ok == [1]  # only the first success after a failure

    def test_raising_observers_do_not_propagate(self, tmp_path):
        inv = InventoryManager(_products(), path=tmp_path / "inventory.json")

        def boom():
            raise RuntimeError("bug")

        inv.set_save_observers(boom, boom)
        (tmp_path / "inventory.json.tmp").mkdir()
        inv.set_count("ICE-SM", 4)


class TestSaveHealthFollowsWriteOrder:
    """Health follows the order the writes landed, even when the earlier
    writer is stalled after its write and before its health report."""

    @staticmethod
    def _run(tmp_path, monkeypatch, *, start_failing, first_fails):
        from ice_common.observers import HealthEdge
        from ice_edge.monitoring import inventory_manager as im

        stalled = {"thread": None}
        settled = threading.Event()  # first writer is stalled or finished
        release = threading.Event()

        class StallingHealthEdge(HealthEdge):
            """Stalls the chosen thread when it reports an outcome the old
            way (after leaving the write lock)."""

            def _stall(self):
                if threading.current_thread() is stalled["thread"]:
                    settled.set()
                    assert release.wait(5)

            def failed(self):
                self._stall()
                super().failed()

            def ok(self):
                self._stall()
                super().ok()

        monkeypatch.setattr(im, "HealthEdge", StallingHealthEdge)

        outcome = {"fail": start_failing}
        first_written = threading.Event()
        real_write = im.atomic_write

        def fake_write(path, text):
            fail = outcome["fail"]
            if threading.current_thread() is stalled["thread"]:
                first_written.set()
            if fail:
                raise OSError("disk full")
            real_write(path, text)

        monkeypatch.setattr(im, "atomic_write", fake_write)
        inv = InventoryManager(_products(), path=tmp_path / "inventory.json")
        calls: list[str] = []
        inv.set_save_observers(
            lambda: calls.append("failed"), lambda: calls.append("ok")
        )
        if start_failing:
            inv.set_count("ICE-SM", 1)  # health is now failing
        calls.clear()

        def first():
            outcome["fail"] = first_fails
            inv.set_count("ICE-SM", 2)
            settled.set()

        a = threading.Thread(target=first)
        stalled["thread"] = a
        a.start()
        assert first_written.wait(5)
        # Old code: A parks in its health report. Fixed code: A just finishes.
        assert settled.wait(5)

        def second():
            outcome["fail"] = not first_fails
            inv.set_count("ICE-SM", 3)

        b = threading.Thread(target=second)
        b.start()
        b.join(5)
        assert not b.is_alive()
        release.set()
        a.join(5)
        assert not a.is_alive()
        return calls

    def test_older_success_cannot_clear_a_newer_failure(self, tmp_path, monkeypatch):
        calls = self._run(tmp_path, monkeypatch, start_failing=True, first_fails=False)
        assert calls == ["ok", "failed"]

    def test_older_failure_cannot_outlive_a_newer_recovery(self, tmp_path, monkeypatch):
        calls = self._run(tmp_path, monkeypatch, start_failing=False, first_fails=True)
        assert calls == ["failed", "ok"]


class TestSaveDelivery:
    def test_delivery_is_outside_the_write_lock_and_in_write_order(
        self, tmp_path, monkeypatch
    ):
        from ice_edge.monitoring import inventory_manager as im

        real_write = im.atomic_write
        fail_next = {"v": False}
        second_written = threading.Event()
        parked = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def fake_write(path, text):
            if fail_next["v"]:
                raise OSError("disk full")
            real_write(path, text)
            if threading.current_thread().name == "second":
                second_written.set()

        monkeypatch.setattr(im, "atomic_write", fake_write)
        inv = InventoryManager(_products(), path=tmp_path / "inventory.json")

        def on_failed():
            parked.set()
            assert release.wait(5)
            calls.append("failed")

        inv.set_save_observers(on_failed, lambda: calls.append("ok"))

        fail_next["v"] = True
        a = threading.Thread(target=lambda: inv.set_count("ICE-SM", 2), name="first")
        a.start()
        assert parked.wait(5)  # A's failed-save notification is in flight

        fail_next["v"] = False
        b = threading.Thread(target=lambda: inv.set_count("ICE-SM", 3), name="second")
        b.start()
        # B's write is not blocked by A's parked delivery ...
        assert second_written.wait(5)
        assert json.loads((tmp_path / "inventory.json").read_text())["ICE-SM"] == 3
        assert calls == []  # ... but its notification waits its turn
        release.set()
        a.join(5)
        b.join(5)
        assert not a.is_alive() and not b.is_alive()
        assert calls == ["failed", "ok"]

    def test_observer_may_save_again_without_deadlock(self, tmp_path):
        inv = InventoryManager(_products(), path=tmp_path / "inventory.json")
        calls: list[str] = []

        def on_failed():
            calls.append("failed:start")
            (tmp_path / "inventory.json.tmp").rmdir()
            inv.set_count("ICE-SM", 9)  # nested save from inside delivery
            calls.append("failed:end")

        inv.set_save_observers(on_failed, lambda: calls.append("ok"))
        (tmp_path / "inventory.json.tmp").mkdir()
        done = threading.Event()

        def run():
            inv.set_count("ICE-SM", 4)
            done.set()

        t = threading.Thread(target=run)
        t.start()
        assert done.wait(5)
        t.join(5)
        assert calls == ["failed:start", "ok", "failed:end"]
        assert json.loads((tmp_path / "inventory.json").read_text())["ICE-SM"] == 9


def test_save_fsyncs_the_file_before_replacing_it(tmp_path, monkeypatch):
    import os

    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(
        os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1]
    )
    monkeypatch.setattr(
        os, "replace", lambda s, d: (calls.append("replace"), real_replace(s, d))[1]
    )
    path = tmp_path / "inventory.json"
    inv = InventoryManager(_products(), path=path)
    calls.clear()

    inv.set_count("ICE-SM", 7)

    assert "fsync" in calls
    assert calls.index("fsync") < calls.index("replace")
    assert json.loads(path.read_text())["ICE-SM"] == 7
