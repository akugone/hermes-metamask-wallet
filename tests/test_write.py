import json
import sys


def _mod(name):
    return sys.modules[f"hermes_plugins.metamask_wallet.{name}"]


def _fake_run(monkeypatch, responses):
    tools = _mod("tools"); tw = _mod("tools_write"); mm = _mod("mm_client")
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        for pred, env in responses:
            if pred(args):
                return env
        return {"ok": True, "data": {}}

    monkeypatch.setattr(mm, "run", fake_run)
    monkeypatch.setattr(mm, "is_installed", lambda: True)
    return calls


ADDR = "0x" + "ab" * 20
HASH = "0x" + "cd" * 32


# --- jobs helpers ----------------------------------------------------------

def test_jobs_polling_and_status(plugin):
    jobs = _mod("jobs")
    env = {"ok": True, "data": {"status": "pending", "pendingJob": {"pollingId": "abc-123", "kind": "TRANSACTION"}}}
    assert jobs.polling_id(env) == "abc-123"
    assert jobs.status(env) == "PENDING"
    assert jobs.is_pending(env)
    done = {"ok": True, "data": {"status": "CONFIRMED", "txHash": HASH}}
    assert not jobs.is_pending(done) and jobs.tx_hash(done) == HASH
    mfa = {"ok": True, "data": {"status": "AWAITING_MFA", "pollingId": "p1"}}
    assert jobs.status(mfa) == "AWAITING_MFA" and jobs.is_pending(mfa)
    assert "user_action" in jobs.summarize(mfa)
    timeout = {"ok": False, "error": {"code": "JOB_TIMEOUT", "message": "x", "pollingId": "p2"}}
    assert jobs.is_pending(timeout)
    assert jobs.chain_label(8453) == "Base (8453)" and jobs.chain_label(999999) == "chain 999999"
    assert jobs.intent_key("mm_transfer", "a") != jobs.intent_key("mm_transfer", "b")


# --- approval prompts --------------------------------------------------------

def test_hook_escalates_writes_with_intent_grain(plugin):
    hook = plugin._pre_tool_call
    a = hook(tool_name="mm_transfer", args={"to": ADDR, "amount": "25", "token": "usdc", "chain_id": 8453})
    assert a["action"] == "approve"
    assert a["message"] == f"Send 25 USDC to {ADDR} on Base (8453)"
    assert a["rule_key"].startswith("mm_transfer:") and a["rule_key"] != "mm_transfer"
    b = hook(tool_name="mm_transfer", args={"to": ADDR, "amount": "26", "token": "USDC", "chain_id": 8453})
    assert b["rule_key"] != a["rule_key"]  # "always" never covers a different amount


def test_hook_blocks_invalid_write(plugin):
    hook = plugin._pre_tool_call
    r = hook(tool_name="mm_transfer", args={"to": "vitalik.eth", "amount": "1", "token": "ETH"})
    assert r["action"] == "block"
    r = hook(tool_name="mm_sign", args={"kind": "typed_data", "payload": "{not json"})
    assert r["action"] == "block"


def test_swap_and_sign_intents(plugin):
    hook = plugin._pre_tool_call
    s = hook(tool_name="mm_swap_execute", args={"quote_id": "q-12345678"})
    assert "quote q-12345678" in s["message"]
    s2 = hook(tool_name="mm_swap_execute", args={"from_token": "eth", "to_token": "usdc", "amount": "0.1",
                                                  "from_chain_id": 1, "to_chain_id": 8453})
    assert s2["message"] == "Swap 0.1 ETH for USDC on Ethereum (1) → Base (8453)"
    m = hook(tool_name="mm_sign", args={"kind": "message", "message": "hello\nworld", "chain_id": 1})
    assert 'Sign the message "hello world"' in m["message"]
    td = {"types": {}, "primaryType": "Permit", "domain": {"name": "USDC", "chainId": 8453}, "message": {}}
    t = hook(tool_name="mm_sign", args={"kind": "typed_data", "payload": json.dumps(td), "chain_id": 8453,
                                        "intent": "Approve 10 USDC"})
    assert t["message"] == "Sign EIP-712 Approve 10 USDC for USDC on Base (8453)"
    bad = hook(tool_name="mm_sign", args={"kind": "typed_data", "payload": json.dumps(td), "chain_id": 1})
    assert bad["action"] == "block"  # domain chainId mismatch


# --- handlers ---------------------------------------------------------------

def test_transfer_confirms_inline(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [
        (lambda a: a[:1] == ["transfer"], {"ok": True, "data": {"status": "pending", "pollingId": "p-1"}}),
        (lambda a: a[:3] == ["wallet", "requests", "watch"], {"ok": True, "data": {"status": "CONFIRMED", "txHash": HASH}}),
    ])
    out = json.loads(tw.mm_transfer({"to": ADDR, "amount": "1", "token": "ETH", "chain_id": 8453}))
    assert out["status"] == "CONFIRMED" and out["tx_hash"] == HASH and not out.get("watching")
    assert calls[0] == ["transfer", "--to", ADDR, "--amount", "1", "--chain-id", "8453", "--token", "ETH"]
    assert "--wait" not in calls[0]


def test_transfer_awaiting_mfa_hands_off_to_watcher(plugin, monkeypatch):
    tw = _mod("tools_write"); watcher = _mod("watcher")
    _fake_run(monkeypatch, [
        (lambda a: a[:1] == ["transfer"], {"ok": True, "data": {"status": "AWAITING_MFA", "pendingJob": {"pollingId": "p-mfa"}}}),
        (lambda a: a[:3] == ["wallet", "requests", "watch"], {"ok": False, "error": {"code": "JOB_TIMEOUT", "message": "t", "pollingId": "p-mfa"}}),
    ])
    out = json.loads(tw.mm_transfer({"to": ADDR, "amount": "1", "token": "ETH", "chain_id": 8453}))
    assert out["status"] == "AWAITING_MFA" and out["watching"] and out["polling_id"] == "p-mfa"
    assert "approve" in out["user_action"].lower()
    started = {}
    monkeypatch.setattr(watcher, "start", lambda pid, **kw: started.update({pid: kw}) or True)
    plugin._post_tool_call(tool_name="mm_transfer", args={}, result=json.dumps(out), task_id="t1", session_id="s1")
    assert "p-mfa" in started and started["p-mfa"]["session_key"] == "s1"
    plugin._post_tool_call(tool_name="mm_balance", args={}, result=json.dumps(out), task_id="t1")
    assert len(started) == 1


def test_transfer_rejects_bad_args_without_running(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [])
    assert json.loads(tw.mm_transfer({"to": "0x12", "amount": "1", "token": "ETH"}))["error"]["code"] == "INVALID_INPUT"
    assert json.loads(tw.mm_transfer({"to": ADDR, "amount": "0", "token": "ETH"}))["error"]["code"] == "INVALID_INPUT"
    assert calls == []


def test_swap_execute_by_quote(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [(lambda a: True, {"ok": True, "data": {"status": "CONFIRMED", "txHash": HASH}})])
    out = json.loads(tw.mm_swap_execute({"quote_id": "quote-abcdef"}))
    assert out["tx_hash"] == HASH and calls[0] == ["swap", "execute", "--quote-id", "quote-abcdef"]


def test_sign_message_and_typed_data(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [(lambda a: True, {"ok": True, "data": {"status": "CONFIRMED", "signature": "0xsig"}})])
    out = json.loads(tw.mm_sign({"kind": "message", "message": "hi", "chain_id": 1}))
    assert out["signature"] == "0xsig" and calls[0] == ["wallet", "sign-message", "--message", "hi", "--chain-id", "1"]
    td = {"types": {}, "primaryType": "Permit", "domain": {"chainId": 1}, "message": {}}
    tw.mm_sign({"kind": "typed_data", "payload": json.dumps(td), "chain_id": 1, "intent": "Approve"})
    assert calls[-1][:3] == ["wallet", "sign-typed-data", "--chain-id"] and "--intent" in calls[-1]


def test_requests_list_and_watch(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [(lambda a: True, {"ok": True, "data": {"requests": []}})])
    out = json.loads(tw.mm_requests({"action": "list"}))
    assert out["ok"] and "watching_in_background" in out
    tw.mm_requests({"action": "watch", "polling_id": "p-1234", "timeout": 5000})
    assert calls[-1] == ["wallet", "requests", "watch", "p-1234", "--wallet-timeout", "600"]
    assert json.loads(tw.mm_requests({"action": "watch", "polling_id": "!!"}))["error"]["code"] == "INVALID_INPUT"


# --- watcher ------------------------------------------------------------------

def test_watcher_notifies_on_completion(plugin, monkeypatch):
    watcher = _mod("watcher"); mm = _mod("mm_client")
    seq = iter([
        {"ok": False, "error": {"code": "JOB_TIMEOUT", "message": "t"}},
        {"ok": True, "data": {"status": "CONFIRMED", "txHash": HASH}},
    ])
    monkeypatch.setattr(mm, "run", lambda args, **kw: next(seq))
    sent = []

    class Ctx:
        def inject_message(self, text, role="user", session_key=None):
            sent.append((text, role, session_key)); return True
        def on_unload(self, cb): pass

    watcher.configure(Ctx())
    watcher._run("p-9", "Send 1 ETH", "sess")
    assert len(sent) == 1
    text, role, key = sent[0]
    assert role == "system" and key == "sess" and "Send 1 ETH" in text and HASH in text and "CONFIRMED" in text
    assert watcher.active_ids() == []


def test_watcher_dedupes_and_caps(plugin, monkeypatch):
    watcher = _mod("watcher")
    import threading
    monkeypatch.setattr(watcher.threading, "Thread", lambda **kw: type("T", (), {"start": lambda s: None, "is_alive": lambda s: True})())
    watcher._active.clear()
    assert watcher.start("x1") is True
    assert watcher.start("x1") is False
    for i in range(2, 6):
        watcher.start(f"x{i}")
    assert watcher.start("x99") is False  # capacity 5
    watcher._active.clear()


def test_jobs_prefer_envelope_status_over_mfa_notice(plugin):
    jobs = _mod("jobs")
    env = {"ok": True, "data": {"status": "BROADCASTED", "hash": HASH, "pollingId": "p-1"},
           "notices": [{"kind": "AWAITING_MFA", "pollingId": "p-1", "message": "Approve by email"}]}
    assert jobs.status(env) == "BROADCASTED"
    assert not jobs.is_pending(env)
    s = jobs.summarize(env, "Send 1 ETH")
    assert s["tx_hash"] == HASH and s["mfa_message"] == "Approve by email" and "user_action" not in s
    waiting = {"ok": True, "data": {"pollingId": "p-2"}, "notices": [{"kind": "AWAITING_MFA", "pollingId": "p-2", "message": "Approve"}]}
    assert jobs.status(waiting) == "AWAITING_MFA" and jobs.is_pending(waiting)
    assert jobs.summarize(waiting)["user_action"]


def test_transfer_fee_too_low_is_reported_not_sent(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [(lambda a: a[:1] == ["transfer"], {"ok": False, "error": {"code": "TX_FAILED", "message": "rpc_fee_too_low"}})])
    out = json.loads(tw.mm_transfer({"to": ADDR, "amount": "0.001", "token": "ETH", "chain_id": 11155111}))
    assert out["ok"] is False and out["sent"] is False and "NOT SENT" in out["hint"] and "max_fee_gwei" in out["hint"]
    assert len(calls) == 1  # no silent retry


def test_transfer_with_fees_routes_native_through_send_transaction(plugin, monkeypatch):
    tw = _mod("tools_write")
    calls = _fake_run(monkeypatch, [(lambda a: a[:2] == ["wallet", "send-transaction"], {"ok": True, "data": {"status": "BROADCASTED", "hash": HASH}})])
    out = json.loads(tw.mm_transfer({"to": ADDR, "amount": "0.001", "token": "ETH", "chain_id": 11155111, "max_fee_gwei": 5, "priority_fee_gwei": 1.5}))
    assert out["tx_hash"] == HASH and out["route"] == "send-transaction"
    cmd = calls[0]
    payload = json.loads(cmd[cmd.index("--payload") + 1])
    assert payload["to"] == ADDR and int(payload["value"], 16) == 10**15
    assert payload["maxFeePerGas"] == hex(5 * 10**9) and payload["maxPriorityFeePerGas"] == hex(15 * 10**8)
    assert cmd[cmd.index("--intent") + 1].startswith("Send 0.001 ETH to")


def test_transfer_with_fees_builds_erc20_calldata(plugin, monkeypatch):
    tw = _mod("tools_write")
    usdc = "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238"
    calls = _fake_run(monkeypatch, [
        (lambda a: a[:3] == ["token", "list", "search"], {"ok": True, "data": [{"symbol": "USDC", "address": usdc, "decimals": 6}]}),
        (lambda a: a[:2] == ["wallet", "send-transaction"], {"ok": True, "data": {"status": "BROADCASTED", "hash": HASH}}),
    ])
    out = json.loads(tw.mm_transfer({"to": ADDR, "amount": "2.5", "token": "usdc", "chain_id": 11155111, "gas_speed": "high"}))
    assert out["tx_hash"] == HASH and out["fees"] == {"options": {"speed": "high"}}
    cmd = next(c for c in calls if c[:2] == ["wallet", "send-transaction"])
    payload = json.loads(cmd[cmd.index("--payload") + 1])
    assert payload["to"] == usdc and payload["value"] == "0x0"
    assert payload["data"] == "0xa9059cbb" + ADDR[2:].lower().rjust(64, "0") + format(2_500_000, "x").rjust(64, "0")


def test_transfer_gas_validation(plugin):
    hook = plugin._pre_tool_call
    assert hook(tool_name="mm_transfer", args={"to": ADDR, "amount": "1", "token": "ETH", "gas_speed": "turbo"})["action"] == "block"
    assert hook(tool_name="mm_transfer", args={"to": ADDR, "amount": "1", "token": "ETH", "max_fee_gwei": 1, "priority_fee_gwei": 2})["action"] == "block"
    ok = hook(tool_name="mm_transfer", args={"to": ADDR, "amount": "1", "token": "ETH", "chain_id": 1, "max_fee_gwei": 5})
    assert ok["action"] == "approve" and "max fee 5 gwei" in ok["message"]
