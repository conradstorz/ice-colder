from ice_edge.uplink.identity import (
    ClaimNotice,
    MachineIdentity,
    init_cloud_identity,
    sync_notice_from,
)


class FakeStore:
    def __init__(self, code=None):
        self.code = code
        self.callbacks = []

    def cloud_claim_code(self):
        return self.code

    def subscribe_claim_code_change(self, cb):
        self.callbacks.append(cb)

    def publish(self):
        for cb in self.callbacks:
            cb()


def test_same_hash_with_new_expiry_is_not_resent(tmp_path):
    identity = MachineIdentity.load_or_create(tmp_path / "id.json")
    sync_notice_from(identity, lambda: ("h", "2026-10-20T00:00:00Z"))
    held = identity.notice
    identity.mark_notice_acked(held)
    sync_notice_from(identity, lambda: ("h", None))  # resale: expiry changed only
    assert identity.notice == ClaimNotice("h", "2026-10-20T00:00:00Z", True)


def test_new_hash_and_removal_are_sent(tmp_path):
    identity = MachineIdentity.load_or_create(tmp_path / "id.json")
    sync_notice_from(identity, lambda: ("h", None))
    sync_notice_from(identity, lambda: ("g", None))
    assert identity.notice == ClaimNotice("g", None, False)
    sync_notice_from(identity, lambda: None)
    assert identity.notice == ClaimNotice(None, None, False)


def test_init_sends_unsent_code_but_not_an_acked_unchanged_one(tmp_path):
    path = tmp_path / "id.json"
    store = FakeStore(("h", None))
    identity = init_cloud_identity(store, path)
    assert identity.notice == ClaimNotice("h", None, False)
    identity.mark_notice_acked(identity.notice)
    again = init_cloud_identity(FakeStore(("h", None)), path)
    assert again.notice.acked is True
    store.code = ("k", None)
    store.publish()
    assert identity.notice == ClaimNotice("k", None, False)


def test_init_without_code_or_notice_sends_nothing(tmp_path):
    identity = init_cloud_identity(FakeStore(None), tmp_path / "id.json")
    assert identity.notice is None


def test_corrupt_identity_disables_uplink_instead_of_raising(tmp_path):
    path = tmp_path / "id.json"
    path.write_text("not json", encoding="utf-8")
    assert init_cloud_identity(FakeStore(None), path) is None


def test_unwritable_data_dir_disables_uplink(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert init_cloud_identity(FakeStore(None), blocker / "id.json") is None


def test_reset_ack_makes_an_acked_notice_deliverable_again(tmp_path):
    path = tmp_path / "id.json"
    identity = MachineIdentity.load_or_create(path)
    identity.reset_ack()  # no notice: nothing happens
    assert identity.notice is None
    identity.set_notice("h", None)
    identity.mark_notice_acked(identity.notice)
    identity.reset_ack()
    assert identity.notice == ClaimNotice("h", None, False)
    assert MachineIdentity.load_or_create(path).notice.acked is False
