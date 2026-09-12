"""Helpers for MetaMask server-wallet jobs (transfer / swap / sign requests tracked by pollingId)."""

from __future__ import annotations

import hashlib
from typing import Any, Dict, Iterable, Optional

CHAIN_NAMES = {
    1: "Ethereum", 8453: "Base", 42161: "Arbitrum", 10: "Optimism", 137: "Polygon", 56: "BNB Chain",
    59144: "Linea", 324: "zkSync Era", 43114: "Avalanche", 11155111: "Sepolia", 84532: "Base Sepolia",
    421614: "Arbitrum Sepolia", 80002: "Polygon Amoy",
}

TERMINAL_STATUSES = {"CONFIRMED", "COMPLETED", "SUCCESS", "SUCCEEDED", "BROADCASTED", "FAILED", "REJECTED", "DENIED",
                     "EXPIRED", "REVERTED", "CANCELLED", "CANCELED"}
PENDING_STATUSES = {"AWAITING_MFA", "PENDING", "SUBMITTED", "PROCESSING", "QUEUED", "SIGNING"}


_CONTROL_RE = None


def clean_text(value: Any, limit: int = 300) -> str:
    """Text destined for an approval prompt or a chat notice: control characters and terminal escape
    sequences removed, whitespace collapsed, length capped. Strings from MetaMask, token contracts or the
    model never reach the human unfiltered."""
    global _CONTROL_RE
    if _CONTROL_RE is None:
        import re
        _CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069]")
    text = _CONTROL_RE.sub(" ", str(value if value is not None else ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def chain_label(chain_id: Any) -> str:
    try:
        cid = int(chain_id)
    except (TypeError, ValueError):
        return str(chain_id)
    name = CHAIN_NAMES.get(cid)
    return f"{name} ({cid})" if name else f"chain {cid}"


def short_address(addr: str) -> str:
    addr = str(addr or "")
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def _walk(obj: Any, depth: int = 0) -> Iterable[tuple]:
    if depth > 6:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _walk(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:50]:
            yield from _walk(v, depth + 1)


def find_first(obj: Any, keys: Iterable[str]) -> Optional[Any]:
    wanted = {k.lower() for k in keys}
    for k, v in _walk(obj):
        if isinstance(k, str) and k.lower() in wanted and v not in (None, ""):
            return v
    return None


def notices(envelope: Dict[str, Any]) -> list:
    items = envelope.get("notices")
    return [n for n in items if isinstance(n, dict)] if isinstance(items, list) else []


def mfa_notice(envelope: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    for n in notices(envelope):
        if str(n.get("kind", "")).upper() == "AWAITING_MFA":
            return n
    return None


def polling_id(envelope: Dict[str, Any]) -> Optional[str]:
    val = find_first(envelope.get("data", envelope), ("pollingId", "polling_id", "requestId"))
    if val in (None, ""):
        n = mfa_notice(envelope)
        val = (n or {}).get("pollingId") or find_first(envelope, ("pollingId",))
    return str(val) if isinstance(val, (str, int)) and str(val).strip() else None


def status(envelope: Dict[str, Any]) -> Optional[str]:
    """The job's own status when the envelope carries one (data.status wins over notices), else the
    MFA marker when a notice or the raw text shows one."""
    data = envelope.get("data")
    if isinstance(data, dict):
        own = data.get("status")
        if isinstance(own, str) and own.strip():
            return own.upper()
        val = find_first(data, ("status",))
        if isinstance(val, str) and val.strip():
            return val.upper()
    if mfa_notice(envelope) is not None or "AWAITING_MFA" in str(envelope):
        return "AWAITING_MFA"
    val = find_first(envelope, ("status",))
    return str(val).upper() if isinstance(val, str) else None


def tx_hash(envelope: Dict[str, Any]) -> Optional[str]:
    val = find_first(envelope, ("txHash", "hash", "transactionHash"))
    return val if isinstance(val, str) and val.startswith("0x") and len(val) == 66 else None


def signature(envelope: Dict[str, Any]) -> Optional[str]:
    val = find_first(envelope, ("signature",))
    return val if isinstance(val, str) and val.startswith("0x") else None


def is_terminal(st: Optional[str]) -> bool:
    return bool(st) and st.upper() in TERMINAL_STATUSES


def is_pending(envelope: Dict[str, Any]) -> bool:
    """True when the job needs more time or a human (MFA) — i.e. worth watching in the background.
    A BROADCASTED transaction with a hash is treated as done for the user's purposes."""
    if not envelope.get("ok"):
        code = str((envelope.get("error") or {}).get("code", "")).upper()
        return code in {"JOB_TIMEOUT", "RELAY_TIMEOUT"} and polling_id(envelope) is not None
    st = status(envelope)
    if st and is_terminal(st):
        return False
    if tx_hash(envelope) or signature(envelope):
        return st in PENDING_STATUSES  # a hash may exist while still awaiting confirmation
    return polling_id(envelope) is not None


def intent_key(tool_name: str, intent: str) -> str:
    """Approval allow-list grain: one exact intent (same amount, recipient, chain) — never the whole tool."""
    return f"{tool_name}:{hashlib.sha256(intent.encode('utf-8')).hexdigest()[:16]}"


def summarize(envelope: Dict[str, Any], intent: str = "") -> Dict[str, Any]:
    """Compact, model-friendly view of a job result."""
    out: Dict[str, Any] = {"ok": bool(envelope.get("ok"))}
    if intent:
        out["intent"] = intent
    st = status(envelope)
    if st:
        out["status"] = st
    pid = polling_id(envelope)
    if pid:
        out["polling_id"] = pid
    h = tx_hash(envelope)
    if h:
        out["tx_hash"] = h
    sig = signature(envelope)
    if sig:
        out["signature"] = sig
    url = find_first(envelope, ("explorerUrl", "explorer_url"))
    if isinstance(url, str):
        out["explorer_url"] = url
    reason = find_first(envelope, ("failureReason", "failure_reason", "failureDescription"))
    if reason:
        out["failure_reason"] = clean_text(reason)
    if not envelope.get("ok"):
        out["error"] = envelope.get("error")
    n = mfa_notice(envelope)
    if n and n.get("message"):
        out["mfa_message"] = clean_text(n["message"])
        if n.get("expiresAt"):
            out["mfa_expires_at"] = n["expiresAt"]
    if st == "AWAITING_MFA" or (n is not None and not is_terminal(st) and not tx_hash(envelope) and not signature(envelope)):
        out["status"] = "AWAITING_MFA"
        out["user_action"] = ("MetaMask is asking the user to approve this request on MetaMask Mobile (push) "
                              "or via the email link. Tell them; do not retry. The plugin keeps watching and "
                              "will report the outcome.")
    return out
