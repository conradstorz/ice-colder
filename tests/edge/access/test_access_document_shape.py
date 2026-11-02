"""The on-disk access.json key set and order are a stable format."""

import json

from ice_edge.access.access import AccessStore


def test_saved_record_key_order_and_old_file_defaults(tmp_path):
    path = tmp_path / "access.json"
    path.write_text(
        json.dumps(
            {
                "users": {
                    "u1": {
                        "name": "A",
                        "role": "owner",
                        "pin_hash": "h",
                        "pin_salt": "s",
                    }
                },
                "devices": {"d1": {"token_hash": "t", "trusted_user_ids": ["u1", "x"]}},
            }
        )
    )
    store = AccessStore(path)
    store.save()
    doc = json.loads(path.read_text())
    assert list(doc) == [
        "users",
        "devices",
        "emergency_codes",
        "setup",
        "pending_transfer",
        "pending_link",
    ]
    assert doc["users"]["u1"] == {
        "name": "A",
        "email": None,
        "role": "owner",
        "pin_hash": "h",
        "pin_salt": "s",
        "disabled": False,
        "created_at": "",
        "last_login_at": None,
    }
    assert list(doc["users"]["u1"]) == [
        "name",
        "email",
        "role",
        "pin_hash",
        "pin_salt",
        "disabled",
        "created_at",
        "last_login_at",
    ]
    assert list(doc["devices"]["d1"]) == [
        "token_hash",
        "label",
        "shared",
        "trusted_user_ids",
        "created_at",
        "last_seen_at",
    ]
    assert doc["devices"]["d1"]["trusted_user_ids"] == ["u1"]
