"""mm_policy — read and change the Guard Mode policy of the active MetaMask server wallet.

Why this exists: a fresh server wallet ships with ``rolling_24h: 0`` and an empty allowlist, so EVERY
outgoing transaction is a policy violation and MetaMask asks for 2FA. Raising the limit and allowlisting the
recipients the user actually pays is what lets in-policy transactions run without a human — the prerequisite
for any cron job. Changing the policy is itself gated twice: the Hermes approval prompt first (``pre_tool_call``
in ``__init__``), then MetaMask's own MFA for any *broadening* change (higher or removed limit, new allowlisted
address, new chain, removed blocklist entry). Narrowing changes apply immediately on MetaMask's side.

The policy travels as YAML. PyYAML is a hard dependency of Hermes itself, so it is always present at runtime;
the plugin still degrades to a clear error instead of crashing if it is missing.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from . import jobs
from . import mm_client as mm
from . import tools

USD_RE = re.compile(r"^\d+(\.\d{1,2})?$")
MAX_ENTRIES = 50           # per list, per call — keeps the approval sentence readable
SERVICE_DEFAULT_CHAINS = [1, 10, 56, 137, 143, 999, 1329, 4326, 4663, 8453, 42161, 43114, 46630, 59144, 84532, 11155111]


def _yaml():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:  # pragma: no cover - Hermes always ships PyYAML
        return None


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _bad(message: str, hint: str = "") -> str:
    return _dump(mm.error("INVALID_INPUT", message, hint))


# ---------------------------------------------------------------------------
# Policy YAML <-> normalised dict
# ---------------------------------------------------------------------------

def _address(value: Any) -> Optional[str]:
    """Normalise an address that may have been YAML-parsed as an int (unquoted 0x… is a YAML 1.1 hex int)."""
    if isinstance(value, int) and not isinstance(value, bool):
        value = f"0x{value:040x}"
    text = str(value or "").strip()
    return text.lower() if tools.ADDRESS_RE.match(text) else None


def _entries(raw: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            addr, cid = _address(item.get("address")), item.get("chain_id", 0)
        else:
            addr, cid = _address(item), 0
        try:
            cid = int(cid if cid not in (None, "") else 0)
        except (TypeError, ValueError):
            continue
        if addr and cid >= 0 and not any(e["address"] == addr and e["chain_id"] == cid for e in out):
            out.append({"address": addr, "chain_id": cid})
    return out


def parse_policy(text: str) -> Optional[Dict[str, Any]]:
    """Policy YAML from ``mm wallet policy get`` -> normalised dict. None when it cannot be read."""
    y = _yaml()
    if y is None or not isinstance(text, str) or not text.strip():
        return None
    try:
        raw = y.safe_load(text)
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    addresses = raw.get("addresses") if isinstance(raw.get("addresses"), dict) else {}
    evm = raw.get("evm") if isinstance(raw.get("evm"), dict) else {}
    limits = evm.get("outflow_limits_usd") if isinstance(evm.get("outflow_limits_usd"), dict) else {}
    chains: List[int] = []
    for c in evm.get("allowed_chains") or []:
        try:
            c = int(c)
        except (TypeError, ValueError):
            continue
        if c > 0 and c not in chains:
            chains.append(c)
    rolling = limits.get("rolling_24h") if "rolling_24h" in limits else 0
    if rolling is not None:
        try:
            rolling = float(rolling)
        except (TypeError, ValueError):
            rolling = 0.0
    wallet = _address(raw.get("wallet_address")) or str(raw.get("wallet_address") or "")
    return {
        "schema_version": int(raw.get("schema_version") or 1),
        "wallet_address": wallet,
        "addresses": {"allowlist": _entries(addresses.get("allowlist")), "blocklist": _entries(addresses.get("blocklist"))},
        "evm": {"allowed_chains": chains, "outflow_limits_usd": {"rolling_24h": rolling}},
    }


def _num(value: Optional[float]) -> Any:
    if value is None:
        return None
    return int(value) if float(value).is_integer() else float(value)


def serialize_policy(policy: Dict[str, Any]) -> str:
    y = _yaml()
    doc = {
        "schema_version": policy.get("schema_version", 1),
        "wallet_address": str(policy.get("wallet_address", "")),
        "addresses": {
            "allowlist": [{"address": e["address"], "chain_id": int(e["chain_id"])} for e in policy["addresses"]["allowlist"]],
            "blocklist": [{"address": e["address"], "chain_id": int(e["chain_id"])} for e in policy["addresses"]["blocklist"]],
        },
        "evm": {
            "allowed_chains": [int(c) for c in policy["evm"]["allowed_chains"]],
            "outflow_limits_usd": {"rolling_24h": _num(policy["evm"]["outflow_limits_usd"].get("rolling_24h"))},
        },
    }
    # safe_dump quotes the 0x… strings itself (they would otherwise re-read as hex ints).
    return y.safe_dump(doc, sort_keys=False, default_flow_style=False, allow_unicode=True)


def summarize_policy(policy: Dict[str, Any]) -> Dict[str, Any]:
    """Model-friendly view, with the one hint that matters for unattended use."""
    limit = policy["evm"]["outflow_limits_usd"].get("rolling_24h")
    out: Dict[str, Any] = {
        "wallet_address": policy.get("wallet_address"),
        "outflow_limit_24h_usd": _num(limit) if limit is not None else None,
        "allowlist": policy["addresses"]["allowlist"],
        "blocklist": policy["addresses"]["blocklist"],
        "allowed_chains": policy["evm"]["allowed_chains"],
    }
    if limit is None:
        out["hint"] = ("No 24h outflow limit: only the allowlist, Blockaid and the trading mode bound this wallet. "
                       "Consider setting a limit with mm_policy action='set'.")
    elif float(limit) <= 0:
        out["hint"] = ("Outflow limit is 0 USD: EVERY outgoing transaction exceeds it and MetaMask will ask for 2FA. "
                       "To let routine transactions run without approval (e.g. from cron), raise the limit and allowlist "
                       "the recipients with mm_policy action='set' — that change itself needs one 2FA.")
    else:
        out["hint"] = (f"Transactions to allowlisted addresses that keep the rolling 24h outflow under {_num(limit)} USD "
                       "run without 2FA; anything else (or a Blockaid flag) asks the user to approve.")
    return out


# ---------------------------------------------------------------------------
# Requested changes: validation, application, approval sentence
# ---------------------------------------------------------------------------

def _entry_args(raw: Any, what: str) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if raw in (None, "", []):
        return [], None
    if isinstance(raw, (str, dict)):
        raw = [raw]
    if not isinstance(raw, list):
        return None, f"{what} must be a list of addresses or {{address, chain_id}} objects."
    if len(raw) > MAX_ENTRIES:
        return None, f"{what}: at most {MAX_ENTRIES} entries per call."
    out: List[Dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            addr_raw, cid_raw = item.get("address"), item.get("chain_id", 0)
        else:
            addr_raw, cid_raw = item, 0
        addr = _address(addr_raw)
        if not addr:
            return None, f"{what}: '{jobs.clean_text(addr_raw, limit=60)}' is not a 0x-prefixed 40-hex address (ENS is not supported)."
        try:
            cid = int(cid_raw if cid_raw not in (None, "") else 0)
        except (TypeError, ValueError):
            return None, f"{what}: chain_id must be an integer (0 = all chains)."
        if cid < 0:
            return None, f"{what}: chain_id must be 0 (all chains) or a positive chain id."
        if not any(e["address"] == addr and e["chain_id"] == cid for e in out):
            out.append({"address": addr, "chain_id": cid})
    return out, None


def validate_changes(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validated, normalised change set for action='set'. (None, error) when anything is off."""
    changes: Dict[str, Any] = {}
    limit_raw = args.get("outflow_limit_usd")
    remove_limit = bool(args.get("remove_outflow_limit"))
    if remove_limit and limit_raw not in (None, ""):
        return None, "Give either outflow_limit_usd or remove_outflow_limit, not both."
    if remove_limit:
        changes["outflow_limit_usd"] = None
    elif limit_raw not in (None, ""):
        text = str(limit_raw).strip()
        if not USD_RE.match(text):
            return None, "outflow_limit_usd must be a non-negative USD amount with at most 2 decimals, e.g. 100 or 25.50."
        try:
            changes["outflow_limit_usd"] = float(Decimal(text))
        except InvalidOperation:
            return None, "outflow_limit_usd is not a valid number."
    for key in ("allowlist_add", "allowlist_remove", "blocklist_add", "blocklist_remove"):
        entries, err = _entry_args(args.get(key), key)
        if err:
            return None, err
        if entries:
            changes[key] = entries
    chains_raw = args.get("allowed_chains_add")
    if chains_raw not in (None, "", []):
        if not isinstance(chains_raw, list):
            chains_raw = [chains_raw]
        chains: List[int] = []
        for c in chains_raw:
            try:
                c = int(c)
            except (TypeError, ValueError):
                return None, "allowed_chains_add must be a list of positive chain ids."
            if c <= 0:
                return None, "allowed_chains_add must be a list of positive chain ids."
            if c not in chains:
                chains.append(c)
        if len(chains) > MAX_ENTRIES:
            return None, f"allowed_chains_add: at most {MAX_ENTRIES} chains per call."
        changes["allowed_chains_add"] = chains
    if not changes:
        return None, ("Nothing to change. Give outflow_limit_usd, remove_outflow_limit, allowlist_add, allowlist_remove, "
                      "blocklist_add, blocklist_remove and/or allowed_chains_add.")
    return changes, None


def _same(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    return a["address"] == b["address"] and a["chain_id"] == b["chain_id"]


def _remove(entries: List[Dict[str, Any]], targets: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Remove *targets* from *entries*. A target with chain_id 0 removes that address on every chain."""
    kept: List[Dict[str, Any]] = []
    removed = 0
    for e in entries:
        hit = any(t["address"] == e["address"] and (t["chain_id"] == 0 or t["chain_id"] == e["chain_id"]) for t in targets)
        if hit:
            removed += 1
        else:
            kept.append(e)
    return kept, removed


def apply_changes(policy: Dict[str, Any], changes: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], bool]:
    """(new_policy, human list of what actually changed, broadening?) — broadening = MetaMask will ask for MFA."""
    new = json.loads(json.dumps(policy))  # deep copy of plain data
    applied: List[str] = []
    broadening = False
    limits = new["evm"]["outflow_limits_usd"]

    if "outflow_limit_usd" in changes:
        before, after = limits.get("rolling_24h"), changes["outflow_limit_usd"]
        if before != after:
            limits["rolling_24h"] = after
            if after is None:
                applied.append("24h outflow limit removed (unlimited)")
                broadening = True
            else:
                applied.append(f"24h outflow limit {_num(before) if before is not None else 'none'} → {_num(after)} USD")
                broadening = broadening or before is None or float(after) > float(before)

    for key, list_name, verb in (("allowlist_add", "allowlist", "allowlisted"), ("blocklist_add", "blocklist", "blocklisted")):
        for entry in changes.get(key, []):
            current = new["addresses"][list_name]
            if not any(_same(entry, e) for e in current):
                current.append(entry)
                applied.append(f"{verb} {entry['address']} on {_scope(entry['chain_id'])}")
                broadening = broadening or key == "allowlist_add"

    for key, list_name in (("allowlist_remove", "allowlist"), ("blocklist_remove", "blocklist")):
        if changes.get(key):
            kept, removed = _remove(new["addresses"][list_name], changes[key])
            if removed:
                new["addresses"][list_name] = kept
                applied.append(f"{removed} {list_name} entr{'y' if removed == 1 else 'ies'} removed")
                broadening = broadening or key == "blocklist_remove"

    for cid in changes.get("allowed_chains_add", []):
        if cid not in new["evm"]["allowed_chains"]:
            new["evm"]["allowed_chains"].append(cid)
            applied.append(f"chain {jobs.chain_label(cid)} allowed")
            broadening = True

    return new, applied, broadening


def _scope(chain_id: int) -> str:
    return "all chains" if int(chain_id) == 0 else jobs.chain_label(chain_id)


def describe(args: Dict[str, Any]) -> Optional[str]:
    """Approval sentence for action='set', from validated values only. None when validation fails."""
    changes, err = validate_changes(args)
    if err:
        return None
    bits: List[str] = []
    if "outflow_limit_usd" in changes:
        if changes["outflow_limit_usd"] is None:
            bits.append("REMOVE the 24h outflow limit (unlimited outflow)")
        else:
            bits.append(f"set the 24h outflow limit to {_num(changes['outflow_limit_usd'])} USD")
    for e in changes.get("allowlist_add", []):
        bits.append(f"allowlist {e['address']} on {_scope(e['chain_id'])}")
    for e in changes.get("allowlist_remove", []):
        bits.append(f"remove {e['address']} from the allowlist" + ("" if e["chain_id"] == 0 else f" on {_scope(e['chain_id'])}"))
    for e in changes.get("blocklist_add", []):
        bits.append(f"blocklist {e['address']} on {_scope(e['chain_id'])}")
    for e in changes.get("blocklist_remove", []):
        bits.append(f"remove {e['address']} from the blocklist" + ("" if e["chain_id"] == 0 else f" on {_scope(e['chain_id'])}"))
    for cid in changes.get("allowed_chains_add", []):
        bits.append(f"allow {jobs.chain_label(cid)}")
    return "Change MetaMask wallet policy: " + "; ".join(bits)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def _current_policy(timeout: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """(policy, error_envelope)."""
    if _yaml() is None:
        return None, mm.error("PYYAML_MISSING", "PyYAML is not importable in this Python environment.",
                              "Hermes ships PyYAML; run the plugin inside Hermes' own environment.")
    result = mm.run(["wallet", "policy", "get"], timeout=timeout)
    if not result.get("ok"):
        return None, result
    data = mm.data(result, {}) or {}
    text = data.get("policy") if isinstance(data, dict) else data
    policy = parse_policy(text if isinstance(text, str) else "")
    if policy is None:
        return None, mm.error("POLICY_UNREADABLE", "MetaMask returned a policy the plugin could not parse.",
                              "Run mm_policy action='get' again; if it persists the CLI output format changed.")
    if isinstance(data, dict) and data.get("address") and not policy.get("wallet_address"):
        policy["wallet_address"] = str(data["address"])
    return policy, None


def mm_policy(args: Dict[str, Any], **kwargs: Any) -> str:
    action = str(args.get("action") or "get").strip()
    timeout = tools._timeout()

    if action == "template":
        return _dump(mm.run(["wallet", "policy", "template"], timeout=timeout))

    if action == "get":
        policy, err = _current_policy(timeout)
        if err:
            return _dump(err)
        return _dump({"ok": True, "data": summarize_policy(policy), "yaml": serialize_policy(policy)})

    if action != "set":
        return _bad(f"Unknown action '{action}'.", "Use get, template or set.")

    changes, verr = validate_changes(args)
    if verr:
        return _bad(verr)
    intent = describe(args) or ""
    policy, err = _current_policy(timeout)
    if err:
        return _dump(err)
    new_policy, applied, broadening = apply_changes(policy, changes)
    if not applied:
        return _dump({"ok": True, "data": summarize_policy(policy), "changed": False,
                      "note": "The policy already matches the request; nothing was sent to MetaMask."})

    yaml_text = serialize_policy(new_policy)
    cmd = ["wallet", "policy", "set", f"--policy={yaml_text}", "--no-wait"]
    from . import tools_write  # local import: tools_write imports tools; keep module init order simple
    summary = tools_write._run_job(cmd, intent, timeout)
    summary["changed"] = bool(summary.get("ok"))
    summary["applied"] = applied
    summary["mfa_expected"] = broadening
    summary["policy_requested"] = summarize_policy(new_policy)
    if summary.get("ok"):
        if broadening and summary.get("status") not in jobs.TERMINAL_STATUSES:
            summary.setdefault("status", "AWAITING_MFA")
            summary["user_action"] = ("This broadens the policy, so MetaMask asks the user to approve it once on MetaMask "
                                      "Mobile or via the email link. Once approved, transactions that fit the new policy "
                                      "run without further 2FA.")
        elif not broadening:
            summary["note"] = "Narrowing change: MetaMask applies it immediately, no 2FA."
    return _dump(summary)


HANDLERS = {"mm_policy": mm_policy}
