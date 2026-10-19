"""Ed25519 request signing helpers for the edge-to-cloud channel."""

from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256

from ice_contracts.cloud import signing

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
BODY = b'{"a": 1}'


def _keys() -> tuple[bytes, bytes]:
    private = signing.generate_private_key()
    return private, signing.public_key_from_private(private)


def _message() -> bytes:
    return signing.canonical_message("post", "/api/v1/x", "2026-10-19T12:00:00Z", BODY)


def test_sign_verify_round_trip() -> None:
    private, public = _keys()
    assert len(private) == 32
    assert len(public) == 32
    sig = signing.sign(private, _message())
    assert signing.verify(public, _message(), sig) is True


def test_canonical_message_shape() -> None:
    expected = (
        f"POST\n/api/v1/x\n2026-10-19T12:00:00Z\n{sha256(BODY).hexdigest()}".encode()
    )
    assert _message() == expected


def test_tampered_body_fails() -> None:
    private, public = _keys()
    sig = signing.sign(private, _message())
    other = signing.canonical_message("post", "/api/v1/x", "2026-10-19T12:00:00Z", b"x")
    assert signing.verify(public, other, sig) is False


def test_tampered_path_fails() -> None:
    private, public = _keys()
    sig = signing.sign(private, _message())
    other = signing.canonical_message("post", "/api/v1/y", "2026-10-19T12:00:00Z", BODY)
    assert signing.verify(public, other, sig) is False


def test_tampered_method_fails() -> None:
    private, public = _keys()
    sig = signing.sign(private, _message())
    other = signing.canonical_message("put", "/api/v1/x", "2026-10-19T12:00:00Z", BODY)
    assert signing.verify(public, other, sig) is False


def test_tampered_timestamp_fails() -> None:
    private, public = _keys()
    sig = signing.sign(private, _message())
    other = signing.canonical_message("post", "/api/v1/x", "2026-10-19T12:00:01Z", BODY)
    assert signing.verify(public, other, sig) is False


def test_verify_with_wrong_key_is_false() -> None:
    private, _ = _keys()
    _, other_public = _keys()
    sig = signing.sign(private, _message())
    assert signing.verify(other_public, _message(), sig) is False


def test_verify_garbage_signature_is_false_not_raise() -> None:
    _, public = _keys()
    assert signing.verify(public, _message(), "not base64!!") is False
    assert signing.verify(public, _message(), "AAAA") is False
    assert signing.verify(public, _message(), "") is False
    assert signing.verify(b"short", _message(), "AAAA") is False


def test_fingerprint_is_sha256_of_public_key() -> None:
    _, public = _keys()
    fp = signing.fingerprint(public)
    assert fp == sha256(public).hexdigest()
    assert len(fp) == 64


def test_short_fingerprint_format_and_parse() -> None:
    _, public = _keys()
    fp = signing.fingerprint(public)
    short = signing.short_fingerprint(fp)
    assert short == f"{fp[0:4]}-{fp[4:8]}-{fp[8:12]}-{fp[12:16]}"
    assert signing.parse_short_fingerprint(short) == fp[:16]
    assert signing.parse_short_fingerprint(fp[:16]) == fp[:16]
    assert signing.parse_short_fingerprint(short.upper()) == fp[:16]
    assert signing.parse_short_fingerprint(short.replace("-", " ")) == fp[:16]
    assert signing.parse_short_fingerprint(fp[:15]) is None
    assert signing.parse_short_fingerprint("zzzz-zzzz-zzzz-zzzz") is None


def test_timestamp_fresh_window_edges() -> None:
    def ts(delta: int) -> str:
        return signing.format_timestamp(NOW + timedelta(seconds=delta))

    assert signing.is_timestamp_fresh(ts(0), NOW)
    assert signing.is_timestamp_fresh(ts(300), NOW)
    assert signing.is_timestamp_fresh(ts(-300), NOW)
    assert not signing.is_timestamp_fresh(ts(301), NOW)
    assert not signing.is_timestamp_fresh(ts(-301), NOW)
    assert not signing.is_timestamp_fresh("garbage", NOW)


def test_format_timestamp_is_utc_seconds() -> None:
    assert signing.format_timestamp(NOW) == "2026-10-19T12:00:00Z"
    local = datetime(2026, 10, 19, 8, 0, 0, 999, tzinfo=timezone(timedelta(hours=-4)))
    assert signing.format_timestamp(local) == "2026-10-19T12:00:00Z"
