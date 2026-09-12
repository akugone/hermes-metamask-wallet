"""Tool handlers — the code that runs when the LLM calls each mm_* tool.

Contract (Hermes): ``handler(args: dict, **kwargs) -> str`` returning JSON, never raising.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

from . import mm_client as mm

logger = logging.getLogger(__name__)

# Populated by register(ctx) so handlers can read plugin settings.
_ctx: Any = None

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
HEX_RE = re.compile(r"^0x[0-9a-fA-F]*$")
AMOUNT_RE = re.compile(r"^\d+(\.\d+)?$")
SYMBOL_RE = re.compile(r"^[A-Za-z0-9._\-]{1,24}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9._:\-]{20,600}$")  # cliToken:cliRefreshToken, no whitespace

# Native asset per chain, in CAIP-19 form (slip44 coin type).
NATIVE_ASSET = {
    1: ("ETH", "slip44:60"), 8453: ("ETH", "slip44:60"), 42161: ("ETH", "slip44:60"),
    10: ("ETH", "slip44:60"), 59144: ("ETH", "slip44:60"), 324: ("ETH", "slip44:60"),
    137: ("POL", "slip44:966"), 56: ("BNB", "slip44:714"), 43114: ("AVAX", "slip44:9000"),
}
NATIVE_ALIASES = {"MATIC": "POL", "WETH": None}


def set_context(ctx: Any) -> None:
    global _ctx
    _ctx = ctx


def _setting(key: str, default: Any) -> Any:
    try:
        if _ctx is not None:
            value = _ctx.get_config(key, default)
            return default if value in (None, "") else value
    except Exception:
        pass
    return default


def _timeout() -> int:
    try:
        return max(10, int(_setting("command_timeout", mm.DEFAULT_TIMEOUT)))
    except (TypeError, ValueError):
        return mm.DEFAULT_TIMEOUT


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _bad(message: str, hint: str = "") -> str:
    return _dump(mm.error("INVALID_INPUT", message, hint))


def _chain_id(value: Any, default_ok: bool = True) -> Optional[int]:
    if value in (None, ""):
        if not default_ok:
            return None
        value = _setting("default_chain_id", 8453)
    try:
        cid = int(value)
    except (TypeError, ValueError):
        return None
    return cid if cid > 0 else None


def _chain_flag(args: Dict[str, Any], flag: str = "--chain-ids") -> List[str]:
    ids = mm.csv(args.get("chain_ids"))
    return [flag, ids] if ids else []


# ---------------------------------------------------------------------------
# mm_status
# ---------------------------------------------------------------------------

def mm_status(args: Dict[str, Any], **kwargs: Any) -> str:
    out: Dict[str, Any] = {"installed": mm.is_installed()}
    if not out["installed"]:
        out["next_step"] = ("The mm CLI is not installed. Ask the user for permission, then call "
                            "mm_setup action='install_cli'.")
        return _dump({"ok": True, "data": out})

    ver = mm.version()
    out["version"] = ver
    out["version_ok"] = mm.version_ok(ver)
    if not out["version_ok"]:
        out["warning"] = f"mm {ver or '?'} is older than the tested minimum {'.'.join(map(str, mm.MIN_CLI_VERSION))}."

    doctor = mm.data(mm.run(["doctor"], timeout=_timeout()), {}) or {}
    out["authenticated"] = bool(doctor.get("authenticated"))
    out["initialized"] = bool(doctor.get("initialized"))
    hints = [h for h in (doctor.get("hints") or []) if "skills add" not in str(h)]
    if hints:
        out["cli_hints"] = hints

    if not out["authenticated"]:
        out["next_step"] = ("Not signed in. Call mm_setup action='login_start', show the user the URL, "
                            "then mm_setup action='login_complete' with the token they paste back.")
        return _dump({"ok": True, "data": out})

    if out["initialized"]:
        init = mm.data(mm.run(["init", "show"], timeout=_timeout()))
        if isinstance(init, dict):
            out["init"] = init
        addr = mm.data(mm.run(["wallet", "address"], timeout=_timeout()))
        if isinstance(addr, dict):
            out["address"] = addr.get("address") or addr
        elif addr:
            out["address"] = addr
        mode = mm.run(["wallet", "trading-mode", "get"], timeout=_timeout())
        if mode.get("ok"):
            md = mode.get("data")
            out["trading_mode"] = md.get("mode") if isinstance(md, dict) and md.get("mode") else md
        if args.get("include_policy"):
            policy = mm.run(["wallet", "policy", "get"], timeout=_timeout())
            out["policy"] = policy.get("data") if policy.get("ok") else policy.get("error")
        out["next_step"] = ("Ready. Reads: mm_balance, mm_market, mm_history, mm_swap_quote, mm_requests. "
                            "Writes (user approval required): mm_transfer, mm_swap_execute, mm_sign.")
    else:
        out["next_step"] = ("Signed in but no wallet initialised. Ask the user: server-wallet (recommended) "
                            "or byok, and guard (recommended) or beast, then call mm_setup action='init'. "
                            "If init reports no wallet, call mm_setup action='create_wallet'.")
    return _dump({"ok": True, "data": out})


# ---------------------------------------------------------------------------
# mm_setup
# ---------------------------------------------------------------------------

def _login_complete(token: str) -> Dict[str, Any]:
    token = (token or "").strip()
    if not TOKEN_RE.match(token) or ":" not in token:
        return mm.error("INVALID_TOKEN", "That does not look like a MetaMask CLI token.",
                        "The token shown on the sign-in page has the form cliToken:cliRefreshToken. "
                        "Ask the user to copy it again, whole.")
    # The CLI documents MM_CLI_TOKEN; keep the secret out of argv.
    result = mm.run(["login"], timeout=_timeout(), env_extra={"MM_CLI_TOKEN": token})
    if not result.get("ok"):
        result = mm.run(["login", "--token", token], timeout=_timeout())
    if result.get("ok"):
        status = mm.data(mm.run(["auth", "status"], timeout=_timeout()), {}) or {}
        return {"ok": True, "data": {"authenticated": bool(status.get("authenticated")),
                                     "next_step": "Signed in. Now call mm_status, then mm_setup action='init' "
                                                  "after asking the user for wallet_mode and trading_mode."}}
    return result


def mm_setup(args: Dict[str, Any], **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    timeout = _timeout()

    if action == "install_cli":
        if mm.is_installed():
            return _dump({"ok": True, "data": {"installed": True, "version": mm.version(),
                                               "note": "Already installed; nothing to do."}})
        return _dump(mm.install_cli())

    if not mm.is_installed():
        return _dump(mm.error("MM_NOT_INSTALLED", "The mm CLI is not installed yet.",
                              "Call mm_setup action='install_cli' first."))

    if action == "login_start":
        result = mm.run(["login", "browser", "--no-wait"], timeout=timeout)
        payload = mm.data(result, {}) or {}
        url = payload.get("loginUrl") if isinstance(payload, dict) else None
        if not url:
            return _dump(result if not result.get("ok") else
                         mm.error("NO_LOGIN_URL", "MetaMask did not return a sign-in URL.", "Retry login_start."))
        return _dump({"ok": True, "data": {
            "login_url": url,
            "instructions": ("Show this URL to the user. They open it, sign in with Google, email or "
                             "MetaMask, and the page displays a CLI token. Ask them to paste that token "
                             "here, then call mm_setup action='login_complete' with it."),
        }})

    if action == "login_complete":
        return _dump(_login_complete(args.get("token") or ""))

    if action == "init":
        wallet_mode = (args.get("wallet_mode") or "server-wallet").strip()
        trading_mode = (args.get("trading_mode") or "guard").strip()
        if wallet_mode not in ("server-wallet", "byok"):
            return _bad("wallet_mode must be 'server-wallet' or 'byok'.")
        if trading_mode not in ("guard", "beast"):
            return _bad("trading_mode must be 'guard' or 'beast'.")
        if wallet_mode == "byok" and not os.environ.get("MM_MNEMONIC"):
            return _dump(mm.error(
                "BYOK_MNEMONIC_MISSING",
                "BYOK mode needs the seed phrase in the MM_MNEMONIC environment variable, set by the user outside the chat.",
                "Never ask the user to paste a seed phrase in the conversation. Suggest server-wallet instead, "
                "or tell them to export MM_MNEMONIC in Hermes' environment and retry."))
        cmd = ["init", "--wallet", wallet_mode]
        if wallet_mode == "server-wallet":
            cmd += ["--mode", trading_mode]
        result = mm.run(cmd, timeout=max(timeout, 180))
        if result.get("ok"):
            result.setdefault("data", {})
            if isinstance(result["data"], dict):
                result["data"]["next_step"] = "Call mm_status to confirm the address; create a wallet if none exists."
        return _dump(result)

    if action == "create_wallet":
        trading_mode = (args.get("trading_mode") or "guard").strip()
        if trading_mode not in ("guard", "beast"):
            return _bad("trading_mode must be 'guard' or 'beast'.")
        cmd = ["wallet", "create", "--trading-mode", trading_mode]
        name = (args.get("name") or "").strip()
        if name:
            if not re.match(r"^[\w .\-]{1,40}$", name):
                return _bad("name may only contain letters, digits, spaces, dots, dashes (max 40).")
            cmd += ["--name", name]
        return _dump(mm.run(cmd, timeout=max(timeout, 180)))

    if action == "logout":
        return _dump(mm.run(["logout"], timeout=timeout))

    return _bad(f"Unknown action '{action}'.",
                "Use install_cli, login_start, login_complete, init, create_wallet or logout.")


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

# Circle USDC on the testnets mm reads over RPC (--testnet). Used when probing testnets automatically.
TESTNET_USDC = {
    11155111: "0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238",  # Sepolia
    421614: "0x75faf114eafb1BDbe2F0316DF893fd58CE46AA4d",    # Arbitrum Sepolia
    80002: "0x41E94Eb019C0762f9Bfcf9Fb1E58725BfB0e7582",     # Polygon Amoy
}


def _balance_is_empty(data: Any) -> bool:
    if not isinstance(data, dict):
        return True
    chains = data.get("chains") or []
    for chain in chains:
        for tok in (chain.get("tokens") or []) if isinstance(chain, dict) else []:
            try:
                if float(str(tok.get("amount", "0")).replace(",", "")) > 0:
                    return False
            except ValueError:
                return False
    return True


def _balance_cmd(args: Dict[str, Any], testnet: bool) -> Optional[list]:
    cmd = ["wallet", "balance"]
    if not testnet:
        cmd += _chain_flag(args)
    token = (args.get("token") or "").strip()
    if token:
        if not (ADDRESS_RE.match(token) or SYMBOL_RE.match(token) or ":" in token):
            return None
        cmd += ["--token", token]
    currency = (args.get("currency") or _setting("default_currency", "usd") or "usd").strip().lower()
    if not re.match(r"^[a-z]{3,5}$", currency):
        return None
    cmd += ["--currency", currency]
    address = (args.get("address") or "").strip()
    if address:
        cmd += ["--address", address]
    if testnet:
        cmd.append("--testnet")
        contracts = [c.strip() for c in (args.get("token_contracts") or []) if isinstance(c, str)] or list(TESTNET_USDC.values())
        cmd += ["--token-contracts", ",".join(contracts)]
    return cmd


def mm_balance(args: Dict[str, Any], **kwargs: Any) -> str:
    """Balances. Mainnets by default; when they are empty (and the caller did not pick chains), the
    testnets mm can read over RPC are probed too, USDC included, so a test wallet is never reported as
    empty by mistake."""
    token = (args.get("token") or "").strip()
    if token and not (ADDRESS_RE.match(token) or SYMBOL_RE.match(token) or ":" in token):
        return _bad("token must be a symbol, a 0x contract address or a CAIP-19 id.")
    currency = (args.get("currency") or _setting("default_currency", "usd") or "usd").strip().lower()
    if not re.match(r"^[a-z]{3,5}$", currency):
        return _bad("currency must be a fiat code like usd or eur.")
    address = (args.get("address") or "").strip()
    if address and not ADDRESS_RE.match(address):
        return _bad("address must be a 0x-prefixed 40-hex EVM address.")
    contracts = [c.strip() for c in (args.get("token_contracts") or []) if isinstance(c, str)]
    if contracts and not all(ADDRESS_RE.match(c) for c in contracts):
        return _bad("token_contracts must be 0x ERC-20 contract addresses.")

    if args.get("testnet"):
        return _dump(mm.run(_balance_cmd(args, testnet=True), timeout=_timeout()))

    result = mm.run(_balance_cmd(args, testnet=False), timeout=_timeout())
    if not result.get("ok"):
        return _dump(result)
    result["scope"] = "mainnets"
    if _balance_is_empty(result.get("data")) and not args.get("chain_ids"):
        probe = mm.run(_balance_cmd(args, testnet=True), timeout=_timeout())
        if probe.get("ok") and not _balance_is_empty(probe.get("data")):
            result["testnet"] = probe.get("data")
            result["hint"] = ("No mainnet holdings, but this wallet holds TESTNET funds (see `testnet`: Sepolia, "
                              "Arbitrum Sepolia, Polygon Amoy — natives plus Circle USDC). Report them clearly as test "
                              "funds with no fiat value; do not call the wallet empty.")
        else:
            result["hint"] = ("No holdings on the mainnets mm tracks, and none on the testnets it reads over RPC "
                              "(Sepolia, Arbitrum Sepolia, Polygon Amoy). This is authoritative: no need to re-check "
                              "with other tools. Fund the address to get started.")
    return _dump(result)


def _resolve_asset_id(symbol: str, chain_id: int) -> Optional[str]:
    sym = symbol.upper()
    sym = NATIVE_ALIASES.get(sym, sym) or sym
    native = NATIVE_ASSET.get(chain_id)
    if native and sym == native[0]:
        return f"eip155:{chain_id}/{native[1]}"
    found = mm.data(mm.run(["token", "list", "search", symbol, "--chain-ids", str(chain_id), "--limit", "10"],
                           timeout=_timeout()))
    items: List[Dict[str, Any]] = []
    if isinstance(found, list):
        items = [i for i in found if isinstance(i, dict)]
    elif isinstance(found, dict):
        for key in ("tokens", "items", "results", "data"):
            if isinstance(found.get(key), list):
                items = [i for i in found[key] if isinstance(i, dict)]
                break
    for item in items:
        if str(item.get("symbol", "")).upper() != sym:
            continue
        for key in ("assetId", "caipAssetId", "caip19", "assetID", "id"):
            val = item.get(key)
            if isinstance(val, str) and val.startswith("eip155:"):
                return val
        addr = item.get("address") or item.get("contractAddress")
        if isinstance(addr, str) and ADDRESS_RE.match(addr):
            return f"eip155:{chain_id}/erc20:{addr.lower()}"
    return None


def mm_market(args: Dict[str, Any], **kwargs: Any) -> str:
    action = (args.get("action") or "").strip()
    timeout = _timeout()

    if action == "chains":
        return _dump(mm.run(["chains", "list"], timeout=timeout))

    if action in ("trending", "popular"):
        return _dump(mm.run(["token", "list", action], timeout=timeout))

    if action == "token_search":
        query = (args.get("query") or args.get("symbol") or "").strip()
        if not query or len(query) > 64 or not re.match(r"^[\w .\-]+$", query):
            return _bad("query must be a token symbol or name (letters, digits, spaces).")
        cmd = ["token", "list", "search", query] + _chain_flag(args)
        limit = args.get("limit")
        if limit:
            try:
                cmd += ["--limit", str(max(1, min(int(limit), 100)))]
            except (TypeError, ValueError):
                return _bad("limit must be an integer.")
        return _dump(mm.run(cmd, timeout=timeout))

    if action == "price":
        asset_ids = [a for a in (args.get("asset_ids") or []) if isinstance(a, str) and a.startswith("eip155:")]
        if not asset_ids:
            symbol = (args.get("symbol") or "").strip()
            if not SYMBOL_RE.match(symbol):
                return _bad("Give symbol (e.g. ETH) or asset_ids (CAIP-19).")
            chain_id = _chain_id(args.get("chain_id"))
            if chain_id is None:
                return _bad("chain_id must be a positive integer.")
            resolved = _resolve_asset_id(symbol, chain_id)
            if not resolved:
                return _dump(mm.error("ASSET_NOT_FOUND", f"Could not find {symbol} on chain {chain_id}.",
                                      "Try mm_market action='token_search' to find the right symbol/chain."))
            asset_ids = [resolved]
        cmd = ["price", "spot", "--asset-ids", ",".join(asset_ids)]
        vs = (args.get("vs") or _setting("default_currency", "usd") or "usd").strip().lower()
        if re.match(r"^[a-z]{3,5}$", vs):
            cmd += ["--vs", vs]
        if args.get("market_data"):
            cmd.append("--market-data")
        result = mm.run(cmd, timeout=timeout)
        if result.get("ok") and isinstance(result.get("data"), dict):
            result["data"] = {"asset_ids": asset_ids, "prices": result["data"]}
        return _dump(result)

    return _bad(f"Unknown action '{action}'.", "Use price, token_search, chains, trending or popular.")


def mm_history(args: Dict[str, Any], **kwargs: Any) -> str:
    action = (args.get("action") or "list").strip()
    timeout = _timeout()

    if action == "list":
        cmd = ["tx", "history"] + _chain_flag(args)
        limit = args.get("limit") or 20
        try:
            cmd += ["--limit", str(max(1, min(int(limit), 50)))]
        except (TypeError, ValueError):
            return _bad("limit must be an integer between 1 and 50.")
        kind = (args.get("type") or "").strip().lower()
        if kind:
            if kind not in ("in", "out", "self"):
                return _bad("type must be in, out or self.")
            cmd += ["--type", kind]
        return _dump(mm.run(cmd, timeout=timeout))

    if action == "tx":
        tx_hash = (args.get("hash") or "").strip()
        if not TX_HASH_RE.match(tx_hash):
            return _bad("hash must be a 0x-prefixed 64-hex transaction hash.")
        cmd = ["tx", "--hash", tx_hash]
        chain_id = _chain_id(args.get("chain_id"), default_ok=False)
        if chain_id:
            cmd += ["--chain-id", str(chain_id)]
        return _dump(mm.run(cmd, timeout=timeout))

    if action == "decode":
        payload = (args.get("payload") or "").strip()
        if not HEX_RE.match(payload) or len(payload) < 10:
            return _bad("payload must be hex calldata starting with 0x (at least a 4-byte selector).")
        return _dump(mm.run(["decode", "--payload", payload], timeout=timeout))

    return _bad(f"Unknown action '{action}'.", "Use list, tx or decode.")


def mm_swap_quote(args: Dict[str, Any], **kwargs: Any) -> str:
    from_token = (args.get("from_token") or "").strip()
    to_token = (args.get("to_token") or "").strip()
    amount = str(args.get("amount") or "").strip()
    if not SYMBOL_RE.match(from_token) or not SYMBOL_RE.match(to_token):
        return _bad("from_token and to_token must be token symbols (ETH, USDC...).")
    if not AMOUNT_RE.match(amount) or float(amount) <= 0:
        return _bad("amount must be a positive decimal number, e.g. '0.5' or '100'.")
    from_chain = _chain_id(args.get("from_chain_id"))
    if from_chain is None:
        return _bad("from_chain_id must be a positive integer.")
    cmd = ["swap", "quote", "--from", from_token, "--to", to_token, "--amount", amount,
           "--from-chain-id", str(from_chain), "--all-quotes"]  # --all-quotes = compare-only, never executes
    to_chain = _chain_id(args.get("to_chain_id"), default_ok=False)
    if to_chain and to_chain != from_chain:
        cmd += ["--to-chain-id", str(to_chain)]
    slippage = args.get("slippage")
    if slippage not in (None, ""):
        try:
            s = float(slippage)
        except (TypeError, ValueError):
            return _bad("slippage must be a number (percent).")
        if not 0 <= s <= 100:
            return _bad("slippage must be between 0 and 100.")
        cmd += ["--slippage", str(s)]
    result = mm.run(cmd, timeout=max(_timeout(), 120))
    if result.get("ok"):
        result["note"] = ("Quote only — nothing was executed. Executing requires the approval-gated "
                          "mm_swap_execute tool, not available in this release.")
    return _dump(result)


HANDLERS = {
    "mm_status": mm_status,
    "mm_setup": mm_setup,
    "mm_balance": mm_balance,
    "mm_market": mm_market,
    "mm_history": mm_history,
    "mm_swap_quote": mm_swap_quote,
}
