import json
import sys

import pytest

yaml = pytest.importorskip("yaml")


def _mod(name):
    return sys.modules[f"hermes_plugins.metamask_wallet.{name}"]


ADDR = "0x" + "ab" * 20
ADDR2 = "0x" + "cd" * 20
WALLET = "0x62fe7760f9462d766af38eccfa4b5889d9fa32ab"

FRESH_POLICY = f"""# Mimir Wallet Policy
schema_version: 1
wallet_address: "{WALLET}"

addresses:
  allowlist: []
  blocklist: []

evm:
  allowed_chains:
    - 1
    - 8453
    - 11155111
  outflow_limits_usd:
    rolling_24h: 0
"""


def _fake_run(monkeypatch, set_env=None):
    tp = _mod("tools_policy"); mm = _mod("mm_client")
    calls = []

    def fake_run(args, **kw):
        calls.append(list(args))
        if args[:3] == ["wallet", "policy", "get"]:
            return {"ok": True, "data": {"policy": FRESH_POLICY, "address": WALLET}}
        if args[:3] == ["wallet", "policy", "set"]:
            return set_env or {"ok": True, "data": {"status": "AWAITING_MFA", "pollingId": "pol-1"}}
        if args[:3] == ["wallet", "requests", "watch"]:
            return {"ok": True, "data": {"status": "AWAITING_MFA", "pollingId": "pol-1"}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(mm, "run", fake_run)
    monkeypatch.setattr(mm, "is_installed", lambda: True)
    return calls


# --- parsing / serialising ---------------------------------------------------

def test_parse_and_roundtrip(plugin):
    tp = _mod("tools_policy")
    pol = tp.parse_policy(FRESH_POLICY)
    assert pol["wallet_address"] == WALLET
    assert pol["evm"]["outflow_limits_usd"]["rolling_24h"] == 0
    assert pol["evm"]["allowed_chains"] == [1, 8453, 11155111]
    text = tp.serialize_policy(pol)
    again = tp.parse_policy(text)
    assert again == pol
    # the 0x address must survive as a string, never as a YAML hex int
    assert yaml.safe_load(text)["wallet_address"] == WALLET


def test_parse_recovers_unquoted_hex_address(plugin):
    tp = _mod("tools_policy")
    pol = tp.parse_policy(FRESH_POLICY.replace(f'"{WALLET}"', WALLET))
    assert pol["wallet_address"] == WALLET


def test_summary_hint_for_zero_limit(plugin):
    tp = _mod("tools_policy")
    s = tp.summarize_policy(tp.parse_policy(FRESH_POLICY))
    assert s["outflow_limit_24h_usd"] == 0 and "2FA" in s["hint"]


# --- validation / apply -------------------------------------------------------

def test_validate_rejects_bad_inputs(plugin):
    tp = _mod("tools_policy")
    assert tp.validate_changes({})[1]
    assert tp.validate_changes({"outflow_limit_usd": -1})[1]
    assert tp.validate_changes({"outflow_limit_usd": "12.345"})[1]
    assert tp.validate_changes({"outflow_limit_usd": 10, "remove_outflow_limit": True})[1]
    assert tp.validate_changes({"allowlist_add": ["vitalik.eth"]})[1]
    assert tp.validate_changes({"allowlist_add": [{"address": ADDR, "chain_id": -5}]})[1]
    assert tp.validate_changes({"allowed_chains_add": [0]})[1]
    ok, err = tp.validate_changes({"outflow_limit_usd": "100", "allowlist_add": ["0x" + ADDR[2:].upper(), {"address": ADDR, "chain_id": 8453}]})
    assert err is None
    assert ok["outflow_limit_usd"] == 100.0
    assert ok["allowlist_add"] == [{"address": ADDR, "chain_id": 0}, {"address": ADDR, "chain_id": 8453}]


def test_apply_changes_and_broadening(plugin):
    tp = _mod("tools_policy")
    pol = tp.parse_policy(FRESH_POLICY)
    new, applied, broad = tp.apply_changes(pol, {"outflow_limit_usd": 100.0, "allowlist_add": [{"address": ADDR, "chain_id": 0}],
                                                 "allowed_chains_add": [5000]})
    assert broad
    assert new["evm"]["outflow_limits_usd"]["rolling_24h"] == 100.0
    assert new["addresses"]["allowlist"] == [{"address": ADDR, "chain_id": 0}]
    assert 5000 in new["evm"]["allowed_chains"]
    assert len(applied) == 3
    # idempotent: same request again changes nothing
    again, applied2, _ = tp.apply_changes(new, {"outflow_limit_usd": 100.0, "allowlist_add": [{"address": ADDR, "chain_id": 0}]})
    assert applied2 == [] and again == new
    # narrowing: lower limit + remove entry → no MFA expected
    narrowed, applied3, broad3 = tp.apply_changes(new, {"outflow_limit_usd": 50, "allowlist_remove": [{"address": ADDR, "chain_id": 0}]})
    assert not broad3 and narrowed["addresses"]["allowlist"] == [] and len(applied3) == 2
    # removing the limit is broadening and says so
    unlimited, applied4, broad4 = tp.apply_changes(new, {"outflow_limit_usd": None})
    assert broad4 and unlimited["evm"]["outflow_limits_usd"]["rolling_24h"] is None and "unlimited" in applied4[0]


# --- approval gate -------------------------------------------------------------

def test_hook_gates_policy_set_only(plugin):
    hook = plugin._pre_tool_call
    assert hook(tool_name="mm_policy", args={"action": "get"}) is None
    assert hook(tool_name="mm_policy", args={}) is None
    assert hook(tool_name="mm_policy", args={"action": "template"}) is None
    a = hook(tool_name="mm_policy", args={"action": "set", "outflow_limit_usd": 100, "allowlist_add": [{"address": ADDR}]})
    assert a["action"] == "approve"
    assert a["message"] == f"Change MetaMask wallet policy: set the 24h outflow limit to 100 USD; allowlist {ADDR} on all chains"
    assert a["rule_key"].startswith("mm_policy:") and a["rule_key"] != "mm_policy:set"
    b = hook(tool_name="mm_policy", args={"action": "set", "outflow_limit_usd": 200})
    assert b["rule_key"] != a["rule_key"]
    r = hook(tool_name="mm_policy", args={"action": "set", "remove_outflow_limit": True})
    assert "REMOVE the 24h outflow limit" in r["message"]
    bad = hook(tool_name="mm_policy", args={"action": "set"})
    assert bad["action"] == "block"
    bad2 = hook(tool_name="mm_policy", args={"action": "set", "allowlist_add": ["not-an-address"]})
    assert bad2["action"] == "block"


def test_shell_guard_catches_direct_policy_set(plugin):
    sg = _mod("shell_guard")
    assert sg.detect_mm_write("mm wallet policy set --policy 'x' --json")
    assert sg.detect_mm_write("mm wallet policy get --json") is None


# --- handler -----------------------------------------------------------------

def test_policy_get(plugin, monkeypatch):
    tp = _mod("tools_policy")
    calls = _fake_run(monkeypatch)
    out = json.loads(tp.mm_policy({"action": "get"}))
    assert out["ok"] and out["data"]["outflow_limit_24h_usd"] == 0
    assert "2FA" in out["data"]["hint"]
    assert calls == [["wallet", "policy", "get"]]


def test_policy_set_sends_merged_yaml_and_watches_mfa(plugin, monkeypatch):
    tp = _mod("tools_policy")
    calls = _fake_run(monkeypatch)
    out = json.loads(tp.mm_policy({"action": "set", "outflow_limit_usd": 100, "allowlist_add": [{"address": ADDR, "chain_id": 8453}]}))
    assert out["ok"] and out["changed"] and out["mfa_expected"]
    assert out["status"] == "AWAITING_MFA" and out["polling_id"] == "pol-1" and out["watching"]
    assert "user_action" in out
    set_call = next(c for c in calls if c[:3] == ["wallet", "policy", "set"])
    assert "--no-wait" in set_call
    sent = yaml.safe_load(next(a for a in set_call if a.startswith("--policy="))[len("--policy="):])
    assert sent["wallet_address"] == WALLET
    assert sent["evm"]["outflow_limits_usd"]["rolling_24h"] == 100
    assert sent["addresses"]["allowlist"] == [{"address": ADDR, "chain_id": 8453}]
    assert sent["evm"]["allowed_chains"] == [1, 8453, 11155111]  # existing chains kept
    # post hook hands the pending request to the watcher
    started = []
    monkeypatch.setattr(_mod("watcher"), "start", lambda pid, **kw: started.append(pid) or True)
    plugin._post_tool_call(tool_name="mm_policy", args={}, result=json.dumps(out), session_id="s")
    assert started == ["pol-1"]


def test_policy_set_noop_does_not_call_set(plugin, monkeypatch):
    tp = _mod("tools_policy")
    calls = _fake_run(monkeypatch)
    out = json.loads(tp.mm_policy({"action": "set", "outflow_limit_usd": 0}))
    assert out["ok"] and out["changed"] is False
    assert all(c[:3] != ["wallet", "policy", "set"] for c in calls)


def test_policy_set_narrowing_reports_no_mfa(plugin, monkeypatch):
    tp = _mod("tools_policy")
    _fake_run(monkeypatch, set_env={"ok": True, "data": {"status": "COMPLETED"}})
    out = json.loads(tp.mm_policy({"action": "set", "blocklist_add": [{"address": ADDR2}]}))
    assert out["ok"] and out["changed"] and out["mfa_expected"] is False
    assert "no 2FA" in out["note"]


def test_policy_invalid_set_returns_error(plugin, monkeypatch):
    tp = _mod("tools_policy")
    _fake_run(monkeypatch)
    out = json.loads(tp.mm_policy({"action": "set", "outflow_limit_usd": "abc"}))
    assert not out["ok"] and out["error"]["code"] == "INVALID_INPUT"
    out = json.loads(tp.mm_policy({"action": "bogus"}))
    assert not out["ok"]
