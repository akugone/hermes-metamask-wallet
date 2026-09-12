"""Thin, dependency-free subprocess client around the MetaMask Agent Wallet CLI (``mm``).

Design rules
------------
* Hermes never holds keys. Every wallet operation is delegated to ``mm``; MetaMask keeps the
  keys (server wallet in a TEE, or the user's own BYOK mnemonic in ``~/.metamask``).
* Secrets never travel through argv. Tokens/passwords go through environment variables the
  CLI documents (``MM_CLI_TOKEN``, ``MM_PASSWORD``, ``MM_MNEMONIC``).
* Every call returns the CLI's own JSON envelope ``{"ok": bool, "data": ...}`` or
  ``{"ok": false, "error": {"code", "message", "hint"}}`` so handlers can pass it straight
  back to the model.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MM_PACKAGE = "@metamask/agent-wallet"
MIN_CLI_VERSION = (6, 2, 0)
DEFAULT_TIMEOUT = 90
MAX_RAW_OUTPUT = 4000

_VERSION_RE = re.compile(r"@metamask/agent-wallet/(\d+)\.(\d+)\.(\d+)")


# ---------------------------------------------------------------------------
# Environment / binary resolution
# ---------------------------------------------------------------------------

def node_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Process environment for spawning ``mm``: Hermes-managed Node first, warnings silenced."""
    env: Dict[str, str] = dict(os.environ)
    try:  # Prefer the Node tree Hermes manages, when present.
        from hermes_constants import with_hermes_node_path  # type: ignore
        env = with_hermes_node_path(env)
    except Exception:  # pragma: no cover - hermes not importable in unit tests
        pass
    opts = env.get("NODE_OPTIONS", "")
    if "--no-warnings" not in opts:
        env["NODE_OPTIONS"] = f"{opts} --no-warnings".strip()
    if extra:
        env.update(extra)
    return env


def _candidate_bins(name: str) -> List[Path]:
    home = Path.home()
    return [
        home / ".local" / "bin" / name,
        home / ".npm-global" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
    ]


def find_mm() -> Optional[str]:
    """Absolute path of the ``mm`` binary, or ``None`` when it is not installed."""
    env = node_env()
    found = shutil.which("mm", path=env.get("PATH"))
    if found:
        return found
    for cand in _candidate_bins("mm"):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def find_npm() -> Optional[str]:
    """Absolute path of ``npm`` (Hermes-managed first), or ``None``."""
    try:
        from hermes_constants import find_node_executable  # type: ignore
        managed = find_node_executable("npm")
        if managed:
            return managed
    except Exception:  # pragma: no cover
        pass
    env = node_env()
    return shutil.which("npm", path=env.get("PATH"))


def is_installed() -> bool:
    return find_mm() is not None


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def error(code: str, message: str, hint: str = "", **extra: Any) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if hint:
        err["hint"] = hint
    err.update(extra)
    return {"ok": False, "error": err}


def _json_objects(text: str) -> List[Any]:
    """Every JSON value found in *text*: one per line (NDJSON) or concatenated / pretty-printed objects."""
    found: List[Any] = []
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = text.find("{", idx)
        if idx < 0:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        found.append(obj)
        idx = end
    return found


def parse_output(stdout: str, stderr: str = "", returncode: int = 0) -> Dict[str, Any]:
    """Turn raw CLI output into the ``{"ok": ...}`` envelope.

    In ``--json`` mode ``mm`` may write NDJSON ``{"_notice": {...}}`` lines (MFA pauses, per-step
    outcomes) before the final envelope; they are collected under ``notices``. Stray non-JSON lines
    are ignored; error envelopes on stderr are honoured."""
    notices: List[Dict[str, Any]] = []
    envelope: Optional[Dict[str, Any]] = None
    bare: Optional[Any] = None
    for stream in ((stdout or "").strip(), (stderr or "").strip()):
        if not stream:
            continue
        for obj in _json_objects(stream):
            if isinstance(obj, dict) and "_notice" in obj and isinstance(obj["_notice"], dict):
                notices.append(obj["_notice"])
            elif isinstance(obj, dict) and "ok" in obj and envelope is None:
                envelope = obj
            elif bare is None:
                bare = obj
        if envelope is not None:
            break
    if envelope is None and bare is not None:
        envelope = {"ok": True, "data": bare}
    if envelope is not None:
        if notices:
            envelope["notices"] = notices
        return envelope
    text = (stdout or "").strip()
    raw = (text or (stderr or "").strip())[:MAX_RAW_OUTPUT]
    if returncode == 0 and text:
        return {"ok": True, "data": {"raw": raw}, **({"notices": notices} if notices else {})}
    return error(
        "MM_UNPARSEABLE_OUTPUT" if text else "MM_COMMAND_FAILED",
        f"mm exited with code {returncode} and no JSON payload.",
        "Run the command again; if it keeps failing, check `mm doctor`.",
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------

def run(
    args: List[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    env_extra: Optional[Dict[str, str]] = None,
    json_output: bool = True,
) -> Dict[str, Any]:
    """Run ``mm <args> --json`` and return the parsed envelope. Never raises."""
    mm = find_mm()
    if mm is None:
        return error(
            "MM_NOT_INSTALLED",
            "The MetaMask Agent Wallet CLI (mm) is not installed.",
            "Call mm_setup with action='install_cli' (the user will be asked to approve).",
        )
    cmd = [mm, *args]
    if json_output and not any(a in ("--json", "--format", "-f") or a.startswith("--format=") for a in args):
        cmd.append("--json")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=node_env(env_extra),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return error("MM_TIMEOUT", f"mm {' '.join(args[:3])} did not finish within {timeout}s.",
                     "Retry, or raise the plugin's command_timeout setting.")
    except OSError as exc:
        return error("MM_SPAWN_FAILED", f"Could not start mm: {exc}")
    result = parse_output(proc.stdout, proc.stderr, proc.returncode)
    if not result.get("ok"):
        logger.debug("mm %s failed: %s", args[:2], result.get("error"))
    return result


def version() -> Optional[str]:
    """Installed CLI version as ``'6.2.0'``, or ``None``."""
    mm = find_mm()
    if mm is None:
        return None
    try:
        proc = subprocess.run([mm, "--version"], capture_output=True, text=True, timeout=30,
                              env=node_env(), stdin=subprocess.DEVNULL)
    except (subprocess.TimeoutExpired, OSError):
        return None
    match = _VERSION_RE.search(proc.stdout + proc.stderr)
    return ".".join(match.groups()) if match else None


def version_ok(ver: Optional[str]) -> bool:
    if not ver:
        return False
    try:
        parts = tuple(int(p) for p in ver.split(".")[:3])
    except ValueError:
        return False
    return parts >= MIN_CLI_VERSION


def install_cli(timeout: int = 600) -> Dict[str, Any]:
    """``npm install -g @metamask/agent-wallet@latest``. Returns the envelope with the version."""
    npm = find_npm()
    if npm is None:
        return error("NPM_NOT_FOUND", "npm is not available, so the mm CLI cannot be installed.",
                     "Install Node.js 22.18+ (https://nodejs.org) and try again.")
    try:
        proc = subprocess.run([npm, "install", "-g", f"{MM_PACKAGE}@latest"], capture_output=True,
                              text=True, timeout=timeout, env=node_env(), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return error("NPM_TIMEOUT", "npm install took too long.", "Retry once; check the network.")
    except OSError as exc:
        return error("NPM_SPAWN_FAILED", f"Could not start npm: {exc}")
    if proc.returncode != 0:
        return error("NPM_INSTALL_FAILED", "npm install exited with an error.",
                     "See the raw output; a permissions problem on the global prefix is the usual cause.",
                     raw=(proc.stderr or proc.stdout)[-MAX_RAW_OUTPUT:])
    ver = version()
    return {"ok": True, "data": {"installed": ver is not None, "version": ver, "binary": find_mm()}}


# ---------------------------------------------------------------------------
# Small helpers shared by tools
# ---------------------------------------------------------------------------

def data(envelope: Dict[str, Any], default: Any = None) -> Any:
    return envelope.get("data", default) if envelope.get("ok") else default


def csv(values: Any) -> Optional[str]:
    """Normalise a list/int/str of chain ids (or similar) to the comma-separated form mm expects."""
    if values is None or values == "" or values == []:
        return None
    if isinstance(values, (list, tuple)):
        return ",".join(str(v).strip() for v in values if str(v).strip())
    return str(values).strip()
