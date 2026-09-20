from __future__ import annotations

import inspect
import json
import subprocess

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


def test_a_malformed_origin_with_a_valid_host_is_refused_not_crashed():
    # urlsplit raises ValueError: Invalid IPv6 URL on unbalanced brackets.
    # Both headers are attacker-controlled on a raw request; a raise here
    # would surface as a 500 instead of a 403.
    h = FakeHandler("127.0.0.1")
    h.headers["Origin"] = "http://["
    h.headers["Host"] = "127.0.0.1:3080"
    assert gate.is_owner(h, token="secret") is False


def test_a_valid_origin_with_a_malformed_host_is_refused_not_crashed():
    h = FakeHandler("127.0.0.1")
    h.headers["Origin"] = "http://127.0.0.1:3080"
    h.headers["Host"] = "["
    assert gate.is_owner(h, token="secret") is False


def test_a_malformed_origin_and_a_malformed_host_are_refused_not_crashed():
    h = FakeHandler("127.0.0.1")
    h.headers["Origin"] = "http://["
    h.headers["Host"] = "["
    assert gate.is_owner(h, token="secret") is False


def test_an_origin_of_a_bare_bracket_alone_is_refused_not_crashed():
    h = FakeHandler("127.0.0.1")
    h.headers["Origin"] = "["
    h.headers["Host"] = "127.0.0.1:3080"
    assert gate.is_owner(h, token="secret") is False


def test_sec_fetch_site_none_is_allowed():
    # "none" means the request has no initiator at all (typed URL, bookmark,
    # curl with the header set) — not attacker-controlled navigation.
    h = FakeHandler("127.0.0.1")
    h.headers["Sec-Fetch-Site"] = "none"
    assert gate.is_owner(h, token="secret") is True


def test_sec_fetch_site_same_site_is_allowed():
    h = FakeHandler("127.0.0.1")
    h.headers["Sec-Fetch-Site"] = "same-site"
    assert gate.is_owner(h, token="secret") is True


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


def test_tailscale_login_is_off_by_default_even_from_a_tailscale_address():
    h = FakeHandler("100.64.1.2")
    assert gate.is_owner(h, token="secret") is False


def test_a_matching_tailscale_identity_is_allowed(monkeypatch):
    monkeypatch.setattr(gate, "_tailscale_login", lambda addr: "me@example.com")
    h = FakeHandler("100.64.1.2")
    assert gate.is_owner(h, token="secret", owner_login="me@example.com") is True


def test_a_different_tailscale_identity_is_refused(monkeypatch):
    monkeypatch.setattr(gate, "_tailscale_login", lambda addr: "someone-else@example.com")
    h = FakeHandler("100.64.1.2")
    assert gate.is_owner(h, token="secret", owner_login="me@example.com") is False


def test_an_unresolvable_tailscale_address_is_refused(monkeypatch):
    # e.g. `tailscale` binary missing, tailscaled down, or the address turned
    # out not to be a peer -- _tailscale_login fails closed with None.
    monkeypatch.setattr(gate, "_tailscale_login", lambda addr: None)
    h = FakeHandler("100.64.1.2")
    assert gate.is_owner(h, token="secret", owner_login="me@example.com") is False


def test_owner_login_configured_but_address_is_not_tailscale_never_shells_out(monkeypatch):
    def _boom(addr):
        raise AssertionError("must not be called for a non-tailscale address")
    monkeypatch.setattr(gate, "_tailscale_login", _boom)
    h = FakeHandler("10.0.0.5")
    assert gate.is_owner(h, token="secret", owner_login="me@example.com") is False


def test_the_token_path_still_wins_when_both_are_configured(monkeypatch):
    def _boom(addr):
        raise AssertionError("token already matched; must not shell out")
    monkeypatch.setattr(gate, "_tailscale_login", _boom)
    h = FakeHandler("100.64.1.2")
    h.headers[gate.WRITE_TOKEN_HEADER] = "secret"
    assert gate.is_owner(h, token="secret", owner_login="me@example.com") is True


def test_is_tailscale_addr_recognises_the_cgnat_range_and_the_ula_prefix():
    assert gate._is_tailscale_addr("100.64.0.1") is True
    assert gate._is_tailscale_addr("100.127.255.254") is True
    assert gate._is_tailscale_addr("fd7a:115c:a1e0::1") is True
    assert gate._is_tailscale_addr("10.0.0.5") is False
    assert gate._is_tailscale_addr("8.8.8.8") is False
    assert gate._is_tailscale_addr("not-an-ip") is False


def test_tailscale_login_parses_an_authorized_node_and_caches_the_result():
    calls = []

    class _FakeCompleted:
        stdout = json.dumps({
            "Node": {"MachineAuthorized": True},
            "UserProfile": {"LoginName": "me@example.com"},
        })

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    gate._whois_cache.clear()
    assert gate._tailscale_login("100.99.1.1", run=_fake_run, tailscale_bin="/x/tailscale") == "me@example.com"
    # Second call within the cache window must not shell out again.
    assert gate._tailscale_login("100.99.1.1", run=_fake_run, tailscale_bin="/x/tailscale") == "me@example.com"
    assert len(calls) == 1
    assert calls[0][0] == "/x/tailscale"


def test_tailscale_login_trusts_a_peer_whose_whois_omits_machine_authorized():
    # The real shape for a peer that isn't the daemon's own node -- verified
    # against a live device on the tailnet, which carries no such key at all.
    class _FakeCompleted:
        stdout = json.dumps({
            "Node": {"HostName": "phone"},
            "UserProfile": {"LoginName": "me@example.com"},
        })

    gate._whois_cache.clear()
    run = lambda cmd, **kw: _FakeCompleted()
    assert gate._tailscale_login("100.99.1.4", run=run, tailscale_bin="/x/tailscale") == "me@example.com"


def test_tailscale_login_refuses_an_unauthorized_node():
    class _FakeCompleted:
        stdout = json.dumps({
            "Node": {"MachineAuthorized": False},
            "UserProfile": {"LoginName": "me@example.com"},
        })

    gate._whois_cache.clear()
    run = lambda cmd, **kw: _FakeCompleted()
    assert gate._tailscale_login("100.99.1.2", run=run, tailscale_bin="/x/tailscale") is None


def test_tailscale_login_fails_closed_on_any_error():
    def _raise(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1.5)

    gate._whois_cache.clear()
    assert gate._tailscale_login("100.99.1.3", run=_raise, tailscale_bin="/x/tailscale") is None


def test_tailscale_login_fails_closed_when_the_binary_cannot_be_found_anywhere():
    # The regression this whole module needs to never repeat: a launchd
    # LaunchAgent's PATH is `/usr/bin:/bin:/usr/sbin:/sbin` (verified against
    # this daemon's own service), which does not contain `tailscale` on this
    # machine. Passing tailscale_bin=None simulates _tailscale_bin() coming
    # up empty; this must refuse cleanly, not raise.
    def _boom(cmd, **kwargs):
        raise AssertionError("must not attempt to run a binary that was never found")

    gate._whois_cache.clear()
    assert gate._tailscale_login("100.99.1.5", run=_boom, tailscale_bin=None) is None


def test_tailscale_bin_prefers_path_when_it_resolves(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda name: "/from/path/tailscale")
    gate._tailscale_bin_resolved = False
    try:
        assert gate._tailscale_bin() == "/from/path/tailscale"
    finally:
        gate._tailscale_bin_resolved = False


def test_tailscale_bin_falls_back_to_a_known_install_location(monkeypatch):
    # This is exactly the launchd case: PATH lookup fails, but the binary is
    # sitting in one of the well-known places Tailscale actually installs to.
    monkeypatch.setattr(gate.shutil, "which", lambda name: None)
    monkeypatch.setattr(gate.os, "access",
                         lambda p, mode: p == "/opt/homebrew/bin/tailscale")
    gate._tailscale_bin_resolved = False
    try:
        assert gate._tailscale_bin() == "/opt/homebrew/bin/tailscale"
    finally:
        gate._tailscale_bin_resolved = False


def test_tailscale_bin_is_none_when_nothing_resolves(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", lambda name: None)
    monkeypatch.setattr(gate.os, "access", lambda p, mode: False)
    gate._tailscale_bin_resolved = False
    try:
        assert gate._tailscale_bin() is None
    finally:
        gate._tailscale_bin_resolved = False


def test_token_comparison_uses_compare_digest_not_equality():
    # A timing attack isn't practical to demonstrate in a unit test, so this
    # is a structural check: the token check must go through
    # secrets.compare_digest, not `==`, or a wrong guess doesn't take
    # constant time relative to a right one.
    source = inspect.getsource(gate.is_owner)
    assert "secrets.compare_digest(" in source
    assert "presented == token" not in source

