"""One-time desktop handoff code store (Ludus D55).

These four properties are the entire reason the deep link is safe to hand a
code instead of the tokens themselves. If any of them stops holding, the
handoff quietly degrades back to "a credential sitting in a URL that lands on
the Windows command line" — which is the bug this replaced — and nothing else
in the system would notice.

Tests the store directly rather than through the HTTP layer: the endpoints are
thin wrappers over these functions. routes.py imports supertokens_python at
module scope, so importing IT here would need the whole auth stack installed --
the exact 'cannot import, so it went untested' trap that left two Ludus tests
asserting against hand-copied duplicates. The store lives in its own pure
module for that reason.
"""
import time

from auth.desktop_handoff import (
    DESKTOP_CODE_TTL_SECONDS as _DESKTOP_CODE_TTL_SECONDS,
    _clear_for_tests,
    _desktop_codes,
    consume_code as _consume_desktop_code,
    purge_expired as _purge_expired_desktop_codes,
    store_code as _store_desktop_code,
)


def setup_function():
    _clear_for_tests()


def test_code_roundtrips_to_the_pair_it_stands_for():
    code, expires_in = _store_desktop_code("access-tok", "refresh-tok")
    assert expires_in == _DESKTOP_CODE_TTL_SECONDS
    assert _consume_desktop_code(code) == ("access-tok", "refresh-tok")


def test_code_is_single_use():
    """A replayed deep link must not produce a second live session.

    The deep link is the thing an attacker can read off the command line, so
    'someone re-fires the same URL' is the concrete attack, not a hypothetical.
    """
    code, _ = _store_desktop_code("access-tok", "refresh-tok")
    assert _consume_desktop_code(code) is not None
    assert _consume_desktop_code(code) is None


def test_unknown_code_is_rejected():
    assert _consume_desktop_code("never-issued") is None


def test_expired_code_is_rejected_and_not_returned():
    """Past the TTL the code must be worthless even though it was real.

    Rewinds the stored expiry rather than sleeping 120s — the value under test
    is the comparison, not the clock.
    """
    code, _ = _store_desktop_code("access-tok", "refresh-tok")
    expires_at, tok, ref = _desktop_codes[code]
    _desktop_codes[code] = (time.monotonic() - 1, tok, ref)
    assert _consume_desktop_code(code) is None


def test_codes_do_not_accumulate_forever():
    """The store is an in-memory dict; without purging it is a slow leak.

    Backdating happens AFTER all five are stored, not inside the loop:
    store_code() purges on every write, so an interleaved version deletes the
    earlier entries as it goes and the assertion never sees five. (It caught me
    writing exactly that.)
    """
    codes = [_store_desktop_code(f"tok-{i}", f"ref-{i}")[0] for i in range(5)]
    assert len(_desktop_codes) == 5
    for code in codes:
        _, tok, ref = _desktop_codes[code]
        _desktop_codes[code] = (time.monotonic() - 1, tok, ref)
    _purge_expired_desktop_codes(time.monotonic())
    assert len(_desktop_codes) == 0


def test_storing_a_new_code_purges_expired_ones():
    """Purge-on-write is what keeps the dict bounded without a background task.

    Pinned deliberately: it is easy to 'tidy' the purge call out of store_code()
    and see every test still pass, because nothing else depends on it.
    """
    stale, _ = _store_desktop_code("old-tok", "old-ref")
    _, tok, ref = _desktop_codes[stale]
    _desktop_codes[stale] = (time.monotonic() - 1, tok, ref)

    _store_desktop_code("new-tok", "new-ref")   # this write does the purging

    assert stale not in _desktop_codes
    assert len(_desktop_codes) == 1


def test_codes_are_unique_and_high_entropy():
    """Two mints must never collide, and a code must not be guessable.

    token_urlsafe(32) is 256 bits; the length check is a canary for someone
    'simplifying' it to something short later.
    """
    codes = {_store_desktop_code("t", "r")[0] for _ in range(50)}
    assert len(codes) == 50
    assert all(len(c) >= 40 for c in codes)


def test_consuming_one_code_does_not_disturb_another():
    a, _ = _store_desktop_code("a-tok", "a-ref")
    b, _ = _store_desktop_code("b-tok", "b-ref")
    assert _consume_desktop_code(a) == ("a-tok", "a-ref")
    assert _consume_desktop_code(b) == ("b-tok", "b-ref")
