"""Write tools — every handler here moves funds or signs, and is escalated to the Hermes approval gate
by the ``pre_tool_call`` hook in ``__init__`` BEFORE it runs. Validation lives here so the approval
prompt is built from the same sanitised values the command will use."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple

from . import jobs
from . import mm_client as mm
from . import tools

QUICK_WAIT = 20   # seconds to wait inline before handing the job to the background watcher
QUOTE_ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{6,128}$")


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _bad(message: str, hint: str = "") -> str:
    return _dump(mm.error("INVALID_INPUT", message, hint))


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
    return {"to": to, "amount": amount, "token": token.upper() if tools.SYMBOL_RE.match(token) else token,
            "chain_id": chain_id}, None


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
        return f"Send {v['amount']} {v['token']} to {v['to']} on {jobs.chain_label(v['chain_id'])}"
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
            preview = v["message"].replace("\n", " ")
            preview = preview[:80] + ("…" if len(preview) > 80 else "")
            return f"Sign the message \"{preview}\" with your MetaMask wallet on {jobs.chain_label(v['chain_id'])}"
        domain = v["payload"].get("domain", {})
        who = domain.get("name") if isinstance(domain, dict) else None
        what = v["intent"] or f"{v['payload'].get('primaryType')} typed data"
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
    cmd = ["transfer", "--to", v["to"], "--amount", v["amount"], "--chain-id", str(v["chain_id"]), "--token", v["token"]]
    return _dump(_run_job(cmd, intent, tools._timeout()))


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
        cmd = ["wallet", "sign-message", "--message", v["message"], "--chain-id", str(v["chain_id"])]
    else:
        cmd = ["wallet", "sign-typed-data", "--chain-id", str(v["chain_id"]), "--payload", json.dumps(v["payload"])]
        if v["intent"]:
            cmd += ["--intent", v["intent"]]
    return _dump(_run_job(cmd, intent, tools._timeout()))


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
