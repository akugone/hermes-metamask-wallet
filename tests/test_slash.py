"""``/wallet setup`` — state-aware onboarding checklist, read-only."""
import sys

import pytest

pytest.importorskip("yaml")

WALLET = "0x62fe7760f9462d766af38eccfa4b5889d9fa32ab"
ADDR = "0x" + "ab" * 20


def _policy_yaml(limit, allow):
    entries = "".join(f"\n    - address: \"{a}\"\n      chain_id: 0" for a in allow) or " []"
    return (f"schema_version: 1\nwallet_address: \"{WALLET}\"\naddresses:\n  allowlist:{entries}\n  blocklist: []\n"
            f"evm:\n  allowed_chains:\n    - 1\n  outflow_limits_usd:\n    rolling_24h: {limit}\n")


def _fake(plugin, monkeypatch, *, installed=True, authenticated=True, initialized=True, limit=0, allow=()):
    mm = sys.modules["hermes_plugins.metamask_wallet.mm_client"]
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        if args[:1] == ["doctor"]:
            return {"ok": True, "data": {"authenticated": authenticated, "initialized": initialized}}
        if args[:2] == ["init", "show"]:
            return {"ok": True, "data": {"walletMode": "server-wallet", "tradingMode": "guard"}}
        if args[:2] == ["wallet", "address"]:
            return {"ok": True, "data": {"address": WALLET}}
        if args[:3] == ["wallet", "trading-mode", "get"]:
            return {"ok": True, "data": {"mode": "guard"}}
        if args[:3] == ["wallet", "policy", "get"]:
            return {"ok": True, "data": {"policy": _policy_yaml(limit, allow), "address": WALLET}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(mm, "run", fake_run)
    monkeypatch.setattr(mm, "is_installed", lambda: installed)
    monkeypatch.setattr(mm, "version", lambda: "6.2.0")
    return calls


def test_setup_not_installed(plugin, monkeypatch):
    _fake(plugin, monkeypatch, installed=False)
    out = plugin._slash_wallet("setup")
    assert "⬜ 1  mm CLI installed" in out
    assert 'Say: "Set up my MetaMask wallet"' in out
    assert "**" not in out  # plain text: shown verbatim by the TUI / Desktop


def test_setup_not_signed_in(plugin, monkeypatch):
    _fake(plugin, monkeypatch, authenticated=False, initialized=False)
    out = plugin._slash_wallet("setup")
    assert "✅ 1  mm CLI installed (6.2.0)" in out and "⬜ 2  Signed in" in out
    assert 'Say: "Sign me in to MetaMask"' in out and "paste" in out


def test_setup_no_wallet(plugin, monkeypatch):
    _fake(plugin, monkeypatch, initialized=False)
    out = plugin._slash_wallet("setup")
    assert "⬜ 3  Wallet created" in out
    assert 'Say: "Create my MetaMask wallet"' in out and "server-wallet" in out


def test_setup_fresh_policy_points_to_mm_policy(plugin, monkeypatch):
    _fake(plugin, monkeypatch, limit=0, allow=())
    out = plugin._slash_wallet("setup")
    assert "✅ 3  Wallet created — 0x62fe…32ab · server-wallet · GUARD" in out
    assert "⬜ 4  Policy — 0 USD / 24h, 0 allowlisted addresses → every transfer asks for 2FA" in out
    assert 'Say: "Raise my 24h outflow limit' in out and "ONE 2FA" in out


def test_setup_policy_ok_is_all_green(plugin, monkeypatch):
    calls = _fake(plugin, monkeypatch, limit=10, allow=(ADDR,))
    out = plugin._slash_wallet("setup")
    assert "✅ 4  Policy — 10 USD / 24h, 1 allowlisted address" in out
    assert "Policy in place" in out and "cron_mode = deny" in out
    # read-only: no write sub-command was ever issued
    assert all(c[:3] != ["wallet", "policy", "set"] and c[:1] != ["transfer"] for c in calls)


def test_setup_aliases_and_default_card_unchanged(plugin, monkeypatch):
    _fake(plugin, monkeypatch, limit=10, allow=(ADDR,))
    assert plugin._slash_wallet("onboarding") == plugin._slash_wallet("setup")
    assert plugin._slash_wallet("start") == plugin._slash_wallet("setup")
    card = plugin._slash_wallet("")
    assert card.startswith("🦊 MetaMask Agent Wallet   🟢 ready") and "Next step" not in card
