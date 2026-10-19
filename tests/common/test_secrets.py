from ice_common.secrets import hash_secret, verify_secret
from ice_edge.access import access


def test_round_trip_and_wrong_value():
    stored = hash_secret("12345678")
    assert verify_secret("12345678", stored)
    assert not verify_secret("12345679", stored)


def test_garbage_is_false():
    for bad in ("", "nonsense", "scrypt$zz$zz", "md5$00$00"):
        assert verify_secret("12345678", bad) is False


def test_edge_reexports_the_same_functions():
    assert access.hash_secret is hash_secret
    assert access.verify_secret is verify_secret
