"""Write tools — every handler here moves funds or signs, and is escalated to the Hermes approval gate
by the ``pre_tool_call`` hook in ``__init__`` BEFORE it runs. Validation lives here so the approval
prompt is built from the same sanitised values the command will use."""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Optional, Tuple

from . import jobs
from . import mm_client as mm
from . import tools

QUICK_WAIT = 20   # seconds to wait inline before handing the job to the background watcher
QUOTE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{6,128}$")
GWEI_RE = re.compile(r"^\d+(\.\d{1,9})?$")
GAS_SPEEDS = ("low", "medium", "high")
ERC20_TRANSFER_SELECTOR = "a9059cbb"


def _parse_gwei(value: Any, what: str) -> Tuple[Optional[int], Optional[str]]:
    """Gwei (str/number) -> wei int. (None, None) when absent; (None, error) when invalid."""
    if value in (None, ""):
        return None, None
    text = str(value).strip()
    if not GWEI_RE.match(text) or Decimal(text) <= 0:
        return None, f"{what} must be a positive number of gwei, e.g. 5 or 1.5."
    return int(Decimal(text) * Decimal(10 ** 9)), None


def _to_units(amount: str, decimals: int) -> int:
    scaled = Decimal(amount) * (Decimal(10) ** decimals)
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount has more than {decimals} decimal places")
    return int(scaled)


def _pad32(hex_no_prefix: str) -> str:
    return hex_no_prefix.lower().rjust(64, "0")


def erc20_transfer_calldata(to: str, units: int) -> str:
    return "0x" + ERC20_TRANSFER_SELECTOR + _pad32(to[2:]) + _pad32(format(units, "x"))


def _fee_fields(v: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if v.get("max_fee_wei") is not None:
        out["maxFeePerGas"] = hex(v["max_fee_wei"])
    if v.get("priority_fee_wei") is not None:
        out["maxPriorityFeePerGas"] = hex(v["priority_fee_wei"])
    if v.get("gas_speed"):
        out["options"] = {"speed": v["gas_speed"]}
    return out


def _resolve_erc20(token: str, chain_id: int) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """(address, decimals, error) for a token symbol or address on *chain_id*, via mm's token APIs."""
    if tools.ADDRESS_RE.match(token):
        asset = mm.data(mm.run(["token", "assets", "--asset-ids", f"eip155:{chain_id}/erc20:{token.lower()}"], timeout=tools._timeout()))
        items = asset if isinstance(asset, list) else (asset or {}).get("assets") or (asset or {}).get("items") or ([asset] if isinstance(asset, dict) else [])
        for item in items:
            if isinstance(item, dict) and item.get("decimals") is not None:
                return token, int(item["decimals"]), None
        return None, None, f"Could not read decimals for token {token} on chain {chain_id}."
    found = mm.data(mm.run(["token", "list", "search", token, "--chain-ids", str(chain_id), "--limit", "10"], timeout=tools._timeout()))
    items = found if isinstance(found, list) else next((found[k] for k in ("tokens", "items", "results", "data") if isinstance(found, dict) and isinstance(found.get(k), list)), [])
    for item in items:
        if isinstance(item, dict) and str(item.get("symbol", "")).upper() == token.upper():
            addr = item.get("address") or item.get("contractAddress")
            dec = item.get("decimals")
            if isinstance(addr, str) and tools.ADDRESS_RE.match(addr) and dec is not None:
                return addr, int(dec), None
    return None, None, f"Could not resolve token {token} on chain {chain_id}; pass its 0x contract address."


_dump = tools._dump
_bad = tools._bad


# ---------------------------------------------------------------------------
# Validation shared with the approval prompt
# ---------------------------------------------------------------------------

def validate_transfer(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    to = str(args.get("to") or "").strip()
    amount = str(args.get("amount") or "").strip()
    token = str(args.get("token") or "").strip()
    chain_id = tools._chain_id(args.get("chain_id"))
    if not tools.ADDRESS_RE.match(to):
        return None, "to must be a 0x-prefixed 40-hex EVM address (ENS names are not supported)."
    if not tools.AMOUNT_RE.match(amount) or float(amount) <= 0:
        return None, "amount must be a positive decimal number, e.g. '0.5' or '100'."
    if not (tools.SYMBOL_RE.match(token) or tools.ADDRESS_RE.match(token)):
        return None, "token must be a symbol (ETH, USDC) or a 0x contract address."
    if chain_id is None:
        return None, "chain_id must be a positive integer."
    out: Dict[str, Any] = {"to": to, "amount": amount, "token": token.upper() if tools.SYMBOL_RE.match(token) else token,
                           "chain_id": chain_id}
    speed = str(args.get("gas_speed") or "").strip().lower()
    if speed:
        if speed not in GAS_SPEEDS:
            return None, "gas_speed must be low, medium or high."
        out["gas_speed"] = speed
    max_fee, err = _parse_gwei(args.get("max_fee_gwei"), "max_fee_gwei")
    if err:
        return None, err
    prio, err = _parse_gwei(args.get("priority_fee_gwei"), "priority_fee_gwei")
    if err:
        return None, err
    if max_fee is not None and prio is not None and prio > max_fee:
        return None, "priority_fee_gwei cannot exceed max_fee_gwei."
    if max_fee is not None:
        out["max_fee_wei"] = max_fee
    if prio is not None:
        out["priority_fee_wei"] = prio
    out["explicit_fees"] = bool(speed or max_fee is not None or prio is not None)
    return out, None


def validate_swap(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    quote_id = str(args.get("quote_id") or "").strip()
    if quote_id:
        if not QUOTE_ID_RE.match(quote_id):
            return None, "quote_id has an unexpected format."
        return {"quote_id": quote_id}, None
    from_token = str(args.get("from_token") or "").strip()
    to_token = str(args.get("to_token") or "").strip()
    amount = str(args.get("amount") or "").strip()
    if not tools.SYMBOL_RE.match(from_token) or not tools.SYMBOL_RE.match(to_token):
        return None, "Give quote_id, or from_token + to_token + amount (+ from_chain_id)."
    if not tools.AMOUNT_RE.match(amount) or float(amount) <= 0:
        return None, "amount must be a positive decimal number."
    from_chain = tools._chain_id(args.get("from_chain_id"))
    if from_chain is None:
        return None, "from_chain_id must be a positive integer."
    to_chain = tools._chain_id(args.get("to_chain_id"), default_ok=False) or from_chain
    out = {"from_token": from_token.upper(), "to_token": to_token.upper(), "amount": amount,
           "from_chain_id": from_chain, "to_chain_id": to_chain}
    slippage = args.get("slippage")
    if slippage not in (None, ""):
        try:
            s = float(slippage)
        except (TypeError, ValueError):
            return None, "slippage must be a number (percent)."
        if not 0 <= s <= 100:
            return None, "slippage must be between 0 and 100."
        out["slippage"] = s
    return out, None


def validate_sign(args: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    kind = str(args.get("kind") or "message").strip()
    chain_id = tools._chain_id(args.get("chain_id"))
    if chain_id is None:
        return None, "chain_id must be a positive integer."
    if kind == "message":
        message = args.get("message")
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            return None, "message must be a non-empty string (max 4000 chars)."
        return {"kind": "message", "chain_id": chain_id, "message": message}, None
    if kind == "typed_data":
        payload = args.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return None, "payload must be valid EIP-712 JSON."
        if not isinstance(payload, dict) or not all(k in payload for k in ("types", "primaryType", "domain", "message")):
            return None, "payload needs types, primaryType, domain and message."
        domain_chain = payload.get("domain", {}).get("chainId") if isinstance(payload.get("domain"), dict) else None
        if domain_chain not in (None, "") and str(domain_chain) != str(chain_id):
            return None, f"payload.domain.chainId ({domain_chain}) differs from chain_id ({chain_id})."
        intent = str(args.get("intent") or "").strip()[:200]
        return {"kind": "typed_data", "chain_id": chain_id, "payload": payload, "intent": intent}, None
    return None, "kind must be 'message' or 'typed_data'."


def describe(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """Human sentence for the approval prompt. None when validation fails (the handler will reject)."""
    if tool_name == "mm_transfer":
        v, err = validate_transfer(args)
        if err:
            return None
        text = f"Send {v['amount']} {v['token']} to {v['to']} on {jobs.chain_label(v['chain_id'])}"
        if v.get("explicit_fees"):
            bits = []
            if v.get("gas_speed"):
                bits.append(f"gas {v['gas_speed']}")
            if v.get("max_fee_wei") is not None:
                bits.append(f"max fee {Decimal(v['max_fee_wei']) / Decimal(10 ** 9):g} gwei")
            text += f" ({', '.join(bits)})" if bits else ""
        return text
    if tool_name == "mm_swap_execute":
        v, err = validate_swap(args)
        if err:
            return None
        if "quote_id" in v:
            return f"Execute MetaMask swap quote {v['quote_id']} (see the quote shown before)"
        route = jobs.chain_label(v["from_chain_id"])
        if v["to_chain_id"] != v["from_chain_id"]:
            route += f" → {jobs.chain_label(v['to_chain_id'])}"
        return f"Swap {v['amount']} {v['from_token']} for {v['to_token']} on {route}"
    if tool_name == "mm_sign":
        v, err = validate_sign(args)
        if err:
            return None
        if v["kind"] == "message":
            preview = jobs.clean_text(v["message"], limit=80)
            return f"Sign the message \"{preview}\" with your MetaMask wallet on {jobs.chain_label(v['chain_id'])}"
        domain = v["payload"].get("domain", {})
        who = jobs.clean_text(domain.get("name"), limit=60) if isinstance(domain, dict) and domain.get("name") else None
        what = jobs.clean_text(v["intent"], limit=120) if v["intent"] else f"{jobs.clean_text(v['payload'].get('primaryType'), limit=40)} typed data"
        return f"Sign EIP-712 {what}" + (f" for {who}" if who else "") + f" on {jobs.chain_label(v['chain_id'])}"
    return None


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------

def _run_job(cmd: list, intent: str, timeout: int) -> Dict[str, Any]:
    result = mm.run(cmd, timeout=timeout)
    summary = jobs.summarize(result, intent)
    if result.get("ok") and jobs.is_pending(result) and summary.get("polling_id"):
        # Give a no-MFA job a moment to confirm inline so the model can report a hash right away.
        quick = mm.run(["wallet", "requests", "watch", summary["polling_id"], "--wallet-timeout", str(QUICK_WAIT)],
                       timeout=QUICK_WAIT + 30)
        if quick.get("ok") and not jobs.is_pending(quick):
            summary = jobs.summarize(quick, intent)
        elif jobs.status(quick) == "AWAITING_MFA" or "AWAITING_MFA" in str(result):
            summary["status"] = "AWAITING_MFA"
            summary["user_action"] = jobs.summarize({"ok": True, "status": "AWAITING_MFA"}).get("user_action")
    if summary.get("polling_id") and summary.get("status") not in jobs.TERMINAL_STATUSES:
        summary["watching"] = True
        summary["note"] = ("Request still pending. The plugin watches it in the background and will post the "
                           "outcome in this conversation; mm_requests action='watch' checks it on demand.")
    summary["raw"] = result.get("data") if result.get("ok") else None
    return summary


def mm_transfer(args: Dict[str, Any], **kwargs: Any) -> str:
    v, err = validate_transfer(args)
    if err:
        return _bad(err)
    intent = describe("mm_transfer", args) or ""
    timeout = tools._timeout()

    if not v.get("explicit_fees"):
        cmd = ["transfer", "--to", v["to"], "--amount", v["amount"], "--chain-id", str(v["chain_id"]), "--token", v["token"]]
        summary = _run_job(cmd, intent, timeout)
        err_msg = json.dumps(summary.get("error") or {}).lower()
        if not summary.get("ok") and "fee_too_low" in err_msg and not summary.get("tx_hash") and not summary.get("polling_id"):
            summary["sent"] = False
            summary["hint"] = ("NOT SENT: MetaMask's fee estimate was below what this chain's RPC accepts (common on "
                               "Sepolia). Tell the user, then, with their go, call mm_transfer ONCE more with explicit "
                               "fees, e.g. max_fee_gwei=5 and priority_fee_gwei=1.5 (or gas_speed='high'); the plugin "
                               "then routes through send-transaction with EIP-1559 fees.")
        return _dump(summary)

    # Explicit fees: build the transaction ourselves and use send-transaction, which honours fee fields.
    native = v["token"] in {"ETH", "POL", "MATIC", "BNB", "AVAX"}
    payload: Dict[str, Any]
    try:
        if native:
            payload = {"to": v["to"], "value": hex(_to_units(v["amount"], 18))}
        else:
            addr, decimals, rerr = _resolve_erc20(v["token"], v["chain_id"])
            if rerr:
                return _dump(mm.error("TOKEN_NOT_RESOLVED", rerr, "Give the token's 0x contract address, or send without explicit fees."))
            units = _to_units(v["amount"], 18 if decimals is None else decimals)
            payload = {"to": addr, "value": "0x0", "data": erc20_transfer_calldata(v["to"], units)}
    except (InvalidOperation, ValueError) as exc:
        return _bad(f"amount is not valid for this token: {exc}")
    payload.update(_fee_fields(v))
    cmd = ["wallet", "send-transaction", "--chain-id", str(v["chain_id"]), f"--payload={json.dumps(payload)}", f"--intent={intent}"]
    summary = _run_job(cmd, intent, timeout)
    summary["route"] = "send-transaction"
    summary["fees"] = {k: payload[k] for k in ("maxFeePerGas", "maxPriorityFeePerGas", "options") if k in payload}
    return _dump(summary)


def mm_swap_execute(args: Dict[str, Any], **kwargs: Any) -> str:
    v, err = validate_swap(args)
    if err:
        return _bad(err)
    intent = describe("mm_swap_execute", args) or ""
    if "quote_id" in v:
        cmd = ["swap", "execute", "--quote-id", v["quote_id"]]
    else:
        cmd = ["swap", "execute", "--from", v["from_token"], "--to", v["to_token"], "--amount", v["amount"],
               "--from-chain-id", str(v["from_chain_id"])]
        if v["to_chain_id"] != v["from_chain_id"]:
            cmd += ["--to-chain-id", str(v["to_chain_id"])]
        if "slippage" in v:
            cmd += ["--slippage", str(v["slippage"])]
    return _dump(_run_job(cmd, intent, max(tools._timeout(), 180)))


def mm_sign(args: Dict[str, Any], **kwargs: Any) -> str:
    v, err = validate_sign(args)
    if err:
        return _bad(err)
    intent = describe("mm_sign", args) or ""
    if v["kind"] == "message":
        cmd = ["wallet", "sign-message", f"--message={v['message']}", "--chain-id", str(v["chain_id"])]
    else:
        cmd = ["wallet", "sign-typed-data", "--chain-id", str(v["chain_id"]), f"--payload={json.dumps(v['payload'])}"]
        if v["intent"]:
            cmd += [f"--intent={jobs.clean_text(v['intent'], limit=200)}"]
    summary = _run_job(cmd, intent, tools._timeout())
    if summary.get("signature"):
        summary["note"] = ("Signature produced by MetaMask for the active wallet; it is authoritative, no need to verify "
                           "it locally. Treat it as a credential: whoever holds it can present it wherever this exact "
                           "message is accepted as a login.")
    return _dump(summary)


def mm_requests(args: Dict[str, Any], **kwargs: Any) -> str:
    action = str(args.get("action") or "list").strip()
    if action == "list":
        result = mm.run(["wallet", "requests", "list"], timeout=tools._timeout())
        if result.get("ok"):
            from . import watcher
            result["watching_in_background"] = watcher.active_ids()
        return _dump(result)
    if action == "watch":
        pid = str(args.get("polling_id") or "").strip()
        if not re.match(r"^[A-Za-z0-9._:\-]{4,128}$", pid):
            return _bad("polling_id must be the id returned by a previous request.")
        try:
            wait = max(5, min(int(args.get("timeout") or 60), 600))
        except (TypeError, ValueError):
            return _bad("timeout must be an integer number of seconds (5-600).")
        result = mm.run(["wallet", "requests", "watch", pid, "--wallet-timeout", str(wait)], timeout=wait + 30)
        summary = jobs.summarize(result)
        summary["raw"] = result.get("data") if result.get("ok") else None
        return _dump(summary)
    return _bad(f"Unknown action '{action}'.", "Use list or watch.")


HANDLERS = {
    "mm_transfer": mm_transfer,
    "mm_swap_execute": mm_swap_execute,
    "mm_sign": mm_sign,
    "mm_requests": mm_requests,
}
