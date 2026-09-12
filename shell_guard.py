"""Detect direct invocations of the MetaMask CLI (``mm``) in shell commands, scripts and written files.

The ``mm_*`` tools carry Hermes' human-approval gate. A model can still reach the wallet by typing
``mm transfer …`` into the terminal tool, or by writing a script that spawns ``mm``. This module lets the
``pre_tool_call`` hook escalate those paths to the same gate (MetaMask's own policy and 2FA still apply
either way). Best effort by design: it catches the obvious spellings, not every possible obfuscation, and
it prefers a false positive (one extra approval prompt, e.g. on `echo mm transfer`) to a false negative.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional

# Tools whose arguments can carry a shell command or code that spawns `mm`.
GUARDED_TOOLS = {"terminal", "execute_code", "write_file", "patch"}
TEXT_KEYS = ("command", "code", "content", "patch", "new_content", "text")

# `mm` as a command: bare, or any path ending in /mm, followed by whitespace. Also inside common
# spawn wrappers ("mm", ["mm", ...], `mm ...`).
_MM_BIN = r"(?:^|[\s;&|(`'\"=,\[])(?:[\w./~-]*/)?mm(?:\.cmd)?(?=[\s'\",\]])"

# Write / state-changing sub-commands. Order matters only for the label.
WRITE_SUBCOMMANDS = (
    "transfer",
    "wallet send-transaction", "wallet sign-message", "wallet sign-typed-data",
    "wallet create", "wallet select", "wallet password", "wallet policy set", "wallet trading-mode set",
    "swap execute", "swap quote --yes",
    "perps open", "perps close", "perps modify", "perps cancel", "perps deposit", "perps withdraw", "perps transfer",
    "predict place", "predict deposit", "predict withdraw", "predict redeem", "predict cancel", "predict setup",
    "predict approve", "predict auth",
    "earn supply", "earn withdraw",
    "allowances revoke",
    "init", "login", "logout", "reset",
    "plugins install", "plugins add", "plugins link", "plugins remove", "plugins uninstall", "plugins update", "plugins reset",
    "config set",
)

_SUB_RE = "|".join(re.escape(s).replace(r"\ ", r"[\s'\",]+") for s in sorted(WRITE_SUBCOMMANDS, key=len, reverse=True))
# Global flags may sit before the sub-command, with or without a value (`--json`, `--format json`, `-f json`).
_PATTERN = re.compile(_MM_BIN + r"[\s'\",\]]+(?:--?[\w-]+(?:[\s=]+[^\s'\"-][^\s'\",]*)?[\s'\",]+)*(?:'|\")?(" + _SUB_RE + r")\b", re.IGNORECASE | re.MULTILINE)
# Scripts often build argv lists: ["mm", "wallet", "send-transaction", ...] or subprocess.run([MM, "transfer", ...]).
_ARGV_RE = re.compile(r"\[[^\]]{0,200}?(?:mm|MM|mm_bin|MM_BIN|\"mm\"|'mm')[^\]]{0,300}?(" + _SUB_RE + r")", re.IGNORECASE | re.DOTALL)


def texts_from_args(args: dict) -> Iterable[str]:
    for key in TEXT_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val:
            yield val
        elif isinstance(val, list):
            joined = " ".join(str(v) for v in val if isinstance(v, (str, int, float)))
            if joined:
                yield joined


def detect_mm_write(text: str) -> Optional[str]:
    """Return the matched write sub-command (normalised) when *text* invokes ``mm`` for a write, else None."""
    if not text or "mm" not in text.lower():
        return None
    m = _PATTERN.search(text) or _ARGV_RE.search(text)
    if not m:
        return None
    return re.sub(r"[\s'\",]+", " ", m.group(1)).strip().lower()


def detect_in_args(tool_name: str, args: dict) -> Optional[str]:
    if tool_name not in GUARDED_TOOLS or not isinstance(args, dict):
        return None
    for text in texts_from_args(args):
        hit = detect_mm_write(text)
        if hit:
            return hit
    return None


def approval_directive(tool_name: str, sub: str, args: dict) -> dict:
    snippet = next(iter(texts_from_args(args)), "")
    digest = hashlib.sha256(snippet.encode("utf-8", "replace")).hexdigest()[:12]
    return {
        "action": "approve",
        "message": (f"Direct MetaMask CLI call from `{tool_name}` (mm {sub}). This bypasses the wallet tools' checks; "
                    f"the mm_* tools exist for this and carry the approval gate. Allow this direct call?"),
        "rule_key": f"mm_shell:{digest}",
    }
