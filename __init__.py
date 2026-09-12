"""metamask-wallet — MetaMask Agent Wallet for Hermes Agent.

Registers ten ``mm_*`` tools over the MetaMask ``mm`` CLI, a ``pre_tool_call`` hook that escalates every
fund-moving / signing call (and sensitive setup steps) to Hermes' human-approval gate, a ``post_tool_call``
hook that hands requests awaiting MetaMask 2FA to a background watcher, a ``/wallet`` slash command and a
bundled skill. Keys never enter Hermes: MetaMask holds them (server wallet in a TEE, or the user's BYOK).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from . import jobs
from . import mm_client as mm
from . import schemas, shell_guard, tools, tools_write, watcher

logger = logging.getLogger(__name__)

TOOLSET = "metamask"
_EMOJI = {"mm_status": "🦊", "mm_setup": "🔑", "mm_balance": "💰", "mm_market": "📈", "mm_history": "🧾",
          "mm_swap_quote": "🔁", "mm_transfer": "💸", "mm_swap_execute": "🔁", "mm_sign": "✍️", "mm_requests": "⏳"}

# Every tool here moves funds or signs: always escalated, allow-list grain = one exact intent.
WRITE_TOOLS = frozenset({"mm_transfer", "mm_swap_execute", "mm_sign"})
HANDLERS: Dict[str, Any] = {**tools.HANDLERS, **tools_write.HANDLERS}


def describe_intent(tool_name: str, args: Dict[str, Any]) -> Optional[str]:
    """One human-readable sentence for the approval prompt, built from validated args only."""
    if tool_name in WRITE_TOOLS:
        return tools_write.describe(tool_name, args)
    if tool_name == "mm_setup":
        action = args.get("action")
        if action == "install_cli":
            return "Install the MetaMask Agent Wallet CLI (npm install -g @metamask/agent-wallet)"
        if action in ("init", "create_wallet") and args.get("trading_mode") == "beast":
            return "Set up the MetaMask wallet in BEAST mode (no allowlists, no outflow limit — threat scan only)"
        if action == "init" and args.get("wallet_mode") == "byok":
            return "Initialise MetaMask Agent Wallet with your OWN seed phrase (BYOK) from MM_MNEMONIC"
        if action == "logout":
            return "Sign out of MetaMask Agent Wallet and clear its local credentials"
    return None


def _shell_guard_enabled() -> bool:
    try:
        return bool(_ctx_get("guard_shell_mm", True)) if _ctx_get else True
    except Exception:
        return True


_ctx_get = None  # set in register(): ctx.get_config


def _pre_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, **kwargs: Any) -> Optional[dict]:
    """Escalate sensitive mm_* calls — and direct `mm` invocations from the shell / code / file tools — to the
    Hermes approval gate (fails closed without a human)."""
    args = args or {}
    if not tool_name.startswith("mm_"):
        if _shell_guard_enabled():
            try:
                sub = shell_guard.detect_in_args(tool_name, args)
            except Exception:  # never let the detector break other tools
                sub = None
            if sub:
                return shell_guard.approval_directive(tool_name, sub, args)
        return None
    try:
        message = describe_intent(tool_name, args)
    except Exception:  # never let a formatting bug skip the gate
        message = None
    if tool_name in WRITE_TOOLS:
        if not message:
            # Invalid arguments: block outright rather than run an un-describable write.
            return {"action": "block", "message": f"BLOCKED: {tool_name} called with invalid arguments; "
                                                   "fix them (see the tool description) and try again."}
        return {"action": "approve", "message": message, "rule_key": jobs.intent_key(tool_name, message)}
    if not message:
        return None
    action = args.get("action") or ""
    return {"action": "approve", "message": message, "rule_key": f"{tool_name}:{action}" if action else tool_name}


def _post_tool_call(tool_name: str = "", args: Optional[Dict[str, Any]] = None, result: Any = None,
                    task_id: str = "", session_id: str = "", **kwargs: Any) -> None:
    """Hand a request that is still pending (typically AWAITING_MFA) to the background watcher."""
    if tool_name not in WRITE_TOOLS or not isinstance(result, str):
        return
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return
    if not isinstance(payload, dict) or not payload.get("watching"):
        return
    pid = payload.get("polling_id")
    if pid:
        started = watcher.start(str(pid), intent=str(payload.get("intent") or ""),
                                session_key=session_id or task_id or None)
        logger.debug("metamask-wallet: watcher for %s started=%s", pid, started)


def _slash_wallet(raw_args: str = "") -> str:
    """``/wallet`` — quick status card without going through the model. ``/wallet requests`` lists pending.

    Plain text on purpose: slash-command output is shown verbatim by the Desktop app and the TUI, and
    Markdown markers would appear literally there."""
    sub = (raw_args or "").strip().lower()
    if sub.startswith("req"):
        payload = json.loads(tools_write.mm_requests({"action": "list"}))
        if not payload.get("ok"):
            return f"⏳ Pending MetaMask requests unavailable — {payload.get('error', {}).get('message', '?')}"
        reqs = (payload.get("data") or {}).get("requests") or []
        if not reqs:
            return "⏳ No pending MetaMask requests."
        lines = [f"⏳ MetaMask requests ({len(reqs)})", ""]
        for r in reqs[:10]:
            intent = r.get("intent") or r.get("kind") or "request"
            st = str(r.get("status") or "?")
            marker = "🟢" if st in jobs.TERMINAL_STATUSES else "🟠"
            tx = f"   tx {jobs.short_address(r['txHash'])}" if r.get("txHash") else ""
            lines.append(f"{marker} {intent}")
            lines.append(f"     {st}{tx}   id {str(r.get('pollingId', ''))[:8]}")
        return "\n".join(lines)
    try:
        payload = json.loads(tools.mm_status({}))
    except Exception as exc:  # pragma: no cover
        return f"🦊 MetaMask Agent Wallet — status unavailable: {exc}"
    d = payload.get("data", {})
    if not d.get("installed"):
        return "🦊 MetaMask Agent Wallet — 🔴 CLI not installed. Ask me to set it up."
    ready = bool(d.get("authenticated") and d.get("initialized"))
    light, word = ("🟢", "ready") if ready else (("🟠", "signed in, no wallet yet") if d.get("authenticated") else ("🔴", "not signed in"))
    init = d.get("init") if isinstance(d.get("init"), dict) else {}
    lines = [f"🦊 MetaMask Agent Wallet   {light} {word}", ""]
    if d.get("address"):
        lines.append(f"Address:   {d['address']}")
    mode_bits = [b for b in (init.get("walletMode"), str(d["trading_mode"]).upper() if d.get("trading_mode") else None) if b]
    if mode_bits:
        lines.append(f"Mode:      {' · '.join(mode_bits)}")
    lines.append(f"CLI:       mm {d.get('version') or '?'}")
    if watcher.active_ids():
        lines.append(f"Watching:  {len(watcher.active_ids())} pending request(s) — /wallet requests")
    if d.get("next_step") and not ready:
        lines += ["", d["next_step"]]
    return "\n".join(lines)


def register(ctx) -> None:
    """Called once by the Hermes plugin loader."""
    global _ctx_get
    _ctx_get = ctx.get_config
    tools.set_context(ctx)
    watcher.configure(ctx)
    for schema in schemas.ALL:
        name = schema["name"]
        # mm_status / mm_setup must stay callable when mm is missing (they report / install it).
        check_fn = None if name in ("mm_status", "mm_setup") else mm.is_installed
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=HANDLERS[name],
                          check_fn=check_fn, emoji=_EMOJI.get(name, ""))
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_command("wallet", _slash_wallet, description="MetaMask Agent Wallet status", args_hint="[requests]")
    skill = Path(__file__).parent / "skills" / "metamask-wallet" / "SKILL.md"
    if skill.exists():
        try:
            ctx.register_skill("safe-flows", skill, description="Safe conversational flows for MetaMask Agent Wallet")
        except Exception as exc:  # pragma: no cover
            logger.debug("metamask-wallet: skill registration skipped: %s", exc)
