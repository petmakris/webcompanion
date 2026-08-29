from __future__ import annotations

import inspect

import pytest

from webcompanion import CONTRACT, gate


class FakeHandler:
    def __init__(self, addr="127.0.0.1", **headers):
        self.client_address = (addr, 5000)
        self.headers = {k.replace("_", "-"): v for k, v in headers.items()}


def test_loopback_is_the_owner_with_no_token():
    assert gate.is_owner(FakeHandler("127.0.0.1"), token="secret") is True
    assert gate.is_owner(FakeHandler("::1"), token="secret") is True


def test_a_remote_client_needs_the_token():
    assert gate.is_owner(FakeHandler("10.0.0.5"), token="secret") is False
    h = FakeHandler("10.0.0.5")
    h.headers[gate.WRITE_TOKEN_HEADER] = "secret"
    assert gate.is_owner(h, token="secret") is True


def test_a_wrong_token_is_refused():
    h = FakeHandler("10.0.0.5")
    h.headers[gate.WRITE_TOKEN_HEADER] = "wrong"
    assert gate.is_owner(h, token="secret") is False


def test_a_cross_site_browser_request_is_refused_even_from_loopback():
    # Script on any website runs from loopback. Without this, visiting a page
    # would let it drive the local daemon as the owner.
    h = FakeHandler("127.0.0.1")
    h.headers["Sec-Fetch-Site"] = "cross-site"
    assert gate.is_owner(h, token="secret") is False


def test_a_same_origin_browser_request_is_allowed():
    h = FakeHandler("127.0.0.1")
    h.headers["Sec-Fetch-Site"] = "same-origin"
    assert gate.is_owner(h, token="secret") is True


def test_a_foreign_origin_header_is_refused():
    h = FakeHandler("127.0.0.1")
    h.headers["Origin"] = "https://evil.example"
    h.headers["Host"] = "127.0.0.1:3080"
    assert gate.is_owner(h, token="secret") is False


def test_a_matching_origin_host_is_allowed_ignoring_scheme_and_port():
    h = FakeHandler("127.0.0.1")
    h.headers["Origin"] = "http://127.0.0.1:3080"
    h.headers["Host"] = "127.0.0.1:3080"
    assert gate.is_owner(h, token="secret") is True


def test_a_non_browser_caller_sending_neither_header_is_allowed():
    # The CLI and the IDE plugin send neither; requiring one would break them.
    assert gate.is_owner(FakeHandler("127.0.0.1"), token="secret") is True


def test_an_empty_configured_token_never_authorises_a_remote_client():
    h = FakeHandler("10.0.0.5")
    h.headers[gate.WRITE_TOKEN_HEADER] = ""
    assert gate.is_owner(h, token="") is False


def test_an_ipv6_loopback_origin_matching_bracketed_host_is_allowed():
    # Host headers bracket IPv6 literals ([::1]:3080); Origin's hostname does
    # not (::1). A naive rsplit(":", 1) on Host would compare "[::1]" against
    # "::1" and wrongly refuse the owner on IPv6 loopback.
    h = FakeHandler("::1")
    h.headers["Origin"] = "http://[::1]:3080"
    h.headers["Host"] = "[::1]:3080"
    assert gate.is_owner(h, token="secret") is True


def test_a_foreign_origin_against_an_ipv6_host_is_still_refused():
    h = FakeHandler("::1")
    h.headers["Origin"] = "http://evil.example"
    h.headers["Host"] = "[::1]:3080"
    assert gate.is_owner(h, token="secret") is False


def test_a_missing_contract_header_is_tolerated():
    ok, _ = gate.check_contract(FakeHandler())
    assert ok is True


def test_a_matching_contract_header_is_accepted():
    h = FakeHandler()
    h.headers[gate.CONTRACT_HEADER] = str(CONTRACT)
    assert gate.check_contract(h)[0] is True


@pytest.mark.parametrize("sent,who", [("0", "client"), ("99", "daemon")])
def test_a_mismatched_contract_names_which_side_is_old(sent, who):
    h = FakeHandler()
    h.headers[gate.CONTRACT_HEADER] = sent
    ok, message = gate.check_contract(h)
    assert ok is False
    assert who in message


def test_a_junk_contract_header_is_a_mismatch_not_a_crash():
    h = FakeHandler()
    h.headers[gate.CONTRACT_HEADER] = "banana"
    assert gate.check_contract(h)[0] is False


def test_token_comparison_uses_compare_digest_not_equality():
    # A timing attack isn't practical to demonstrate in a unit test, so this
    # is a structural check: the token check must go through
    # secrets.compare_digest, not `==`, or a wrong guess doesn't take
    # constant time relative to a right one.
    source = inspect.getsource(gate.is_owner)
    assert "secrets.compare_digest(" in source
    assert "presented == token" not in source

