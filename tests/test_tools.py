import json


def _install_fake_run(tools, monkeypatch, responses):
    """responses: list of (predicate(args)->bool, envelope) or a single envelope for everything."""
    calls = []

    def fake_run(args, **kw):
        calls.append((list(args), kw))
        if isinstance(responses, dict):
            return responses
        for pred, env in responses:
            if pred(args):
                return env
        return {"ok": True, "data": {}}

    monkeypatch.setattr(tools.mm, "run", fake_run)
    monkeypatch.setattr(tools.mm, "is_installed", lambda: True)
    monkeypatch.setattr(tools.mm, "version", lambda: "6.2.0")
    return calls


def test_register_wires_everything(plugin, fake_ctx):
    plugin.register(fake_ctx)
    assert set(fake_ctx.tools) == {"mm_status", "mm_setup", "mm_balance", "mm_market", "mm_history", "mm_swap_quote",
                                   "mm_transfer", "mm_swap_execute", "mm_sign", "mm_requests"}
    assert "post_tool_call" in fake_ctx.hooks
    assert fake_ctx.tools["mm_status"]["check_fn"] is None
    assert fake_ctx.tools["mm_balance"]["check_fn"] is not None
    assert "pre_tool_call" in fake_ctx.hooks
    assert "wallet" in fake_ctx.commands
    assert "safe-flows" in fake_ctx.skills


def test_hook_gates_install_and_beast_only(plugin):
    hook = plugin._pre_tool_call
    assert hook(tool_name="mm_setup", args={"action": "install_cli"})["action"] == "approve"
    assert hook(tool_name="mm_setup", args={"action": "init", "trading_mode": "beast"})["action"] == "approve"
    assert hook(tool_name="mm_setup", args={"action": "init", "wallet_mode": "byok"})["action"] == "approve"
    assert hook(tool_name="mm_setup", args={"action": "logout"})["action"] == "approve"
    assert hook(tool_name="mm_setup", args={"action": "login_start"}) is None
    assert hook(tool_name="mm_setup", args={"action": "init", "trading_mode": "guard"}) is None
    assert hook(tool_name="mm_balance", args={}) is None
    assert hook(tool_name="terminal", args={"command": "rm -rf /"}) is None


def test_status_not_installed(tools, monkeypatch):
    monkeypatch.setattr(tools.mm, "is_installed", lambda: False)
    out = json.loads(tools.mm_status({}))
    assert out["data"]["installed"] is False and "install_cli" in out["data"]["next_step"]


def test_status_not_authenticated(tools, monkeypatch):
    _install_fake_run(tools, monkeypatch, [
        (lambda a: a[:1] == ["doctor"], {"ok": True, "data": {"authenticated": False, "initialized": False,
                                                              "hints": ["Install with `npx skills add x`", "Run mm login"]}}),
    ])
    out = json.loads(tools.mm_status({}))["data"]
    assert out["authenticated"] is False and "login_start" in out["next_step"]
    assert out["cli_hints"] == ["Run mm login"]


def test_status_ready(tools, monkeypatch):
    _install_fake_run(tools, monkeypatch, [
        (lambda a: a[:1] == ["doctor"], {"ok": True, "data": {"authenticated": True, "initialized": True}}),
        (lambda a: a[:2] == ["wallet", "address"], {"ok": True, "data": {"address": "0x" + "1" * 40}}),
        (lambda a: a[:2] == ["wallet", "trading-mode"], {"ok": True, "data": {"mode": "guard"}}),
        (lambda a: a[:2] == ["init", "show"], {"ok": True, "data": {"walletMode": "server-wallet"}}),
    ])
    out = json.loads(tools.mm_status({}))["data"]
    assert out["address"] == "0x" + "1" * 40 and out["trading_mode"] == {"mode": "guard"}
    assert out["next_step"].startswith("Ready")


def test_login_complete_rejects_garbage(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {}})
    out = json.loads(tools.mm_setup({"action": "login_complete", "token": "hello world"}))
    assert out["error"]["code"] == "INVALID_TOKEN" and calls == []


def test_login_complete_uses_env_not_argv(tools, monkeypatch):
    token = "cli_" + "a" * 40 + ":" + "b" * 40
    calls = _install_fake_run(tools, monkeypatch, [
        (lambda a: a == ["login"], {"ok": True, "data": {}}),
        (lambda a: a == ["auth", "status"], {"ok": True, "data": {"authenticated": True}}),
    ])
    out = json.loads(tools.mm_setup({"action": "login_complete", "token": token}))
    assert out["ok"] and out["data"]["authenticated"] is True
    login_call = next(c for c in calls if c[0] == ["login"])
    assert login_call[1]["env_extra"] == {"MM_CLI_TOKEN": token}
    assert token not in json.dumps(out)


def test_login_start_returns_url(tools, monkeypatch):
    _install_fake_run(tools, monkeypatch, {"ok": True, "data": {"mode": "no-wait", "loginUrl": "https://x/login"}})
    out = json.loads(tools.mm_setup({"action": "login_start"}))
    assert out["data"]["login_url"] == "https://x/login"


def test_init_byok_refuses_without_env(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {}})
    monkeypatch.delenv("MM_MNEMONIC", raising=False)
    out = json.loads(tools.mm_setup({"action": "init", "wallet_mode": "byok"}))
    assert out["error"]["code"] == "BYOK_MNEMONIC_MISSING" and calls == []


def test_init_server_wallet_guard(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {"walletMode": "server-wallet"}})
    out = json.loads(tools.mm_setup({"action": "init"}))
    assert out["ok"] and calls[0][0] == ["init", "--wallet", "server-wallet", "--mode", "guard"]


def test_balance_validation_and_flags(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {}})
    assert json.loads(tools.mm_balance({"address": "0x123"}))["error"]["code"] == "INVALID_INPUT"
    tools.mm_balance({"chain_ids": [8453, 1], "token": "USDC", "currency": "EUR"})
    assert calls[-1][0] == ["wallet", "balance", "--chain-ids", "8453,1", "--token", "USDC", "--currency", "eur"]


def test_market_price_native_symbol(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {"eip155:8453/slip44:60": {"usd": 3000}}})
    out = json.loads(tools.mm_market({"action": "price", "symbol": "ETH", "chain_id": 8453}))
    assert out["ok"] and out["data"]["asset_ids"] == ["eip155:8453/slip44:60"]
    assert calls[0][0][:4] == ["price", "spot", "--asset-ids", "eip155:8453/slip44:60"]


def test_market_price_erc20_via_search(tools, monkeypatch):
    usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    calls = _install_fake_run(tools, monkeypatch, [
        (lambda a: a[:3] == ["token", "list", "search"], {"ok": True, "data": [{"symbol": "USDC", "address": usdc}]}),
        (lambda a: a[:2] == ["price", "spot"], {"ok": True, "data": {}}),
    ])
    out = json.loads(tools.mm_market({"action": "price", "symbol": "USDC", "chain_id": 8453}))
    assert out["data"]["asset_ids"] == [f"eip155:8453/erc20:{usdc.lower()}"]


def test_history_validation(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {}})
    assert json.loads(tools.mm_history({"action": "tx", "hash": "0xzz"}))["error"]["code"] == "INVALID_INPUT"
    assert json.loads(tools.mm_history({"action": "decode", "payload": "nope"}))["error"]["code"] == "INVALID_INPUT"
    tools.mm_history({"action": "list", "limit": 500, "type": "out"})
    assert calls[-1][0] == ["tx", "history", "--limit", "50", "--type", "out"]


def test_swap_quote_is_compare_only(tools, monkeypatch):
    calls = _install_fake_run(tools, monkeypatch, {"ok": True, "data": {"quotes": []}})
    out = json.loads(tools.mm_swap_quote({"from_token": "ETH", "to_token": "USDC", "amount": "0.1", "from_chain_id": 8453}))
    cmd = calls[0][0]
    assert "--all-quotes" in cmd and "--yes" not in cmd and "execute" not in cmd
    assert "nothing was executed" in out["note"]
    assert json.loads(tools.mm_swap_quote({"from_token": "ETH", "to_token": "USDC", "amount": "-1"}))["error"]["code"] == "INVALID_INPUT"
