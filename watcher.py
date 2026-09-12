"""Background watcher for MetaMask requests that wait on a human (AWAITING_MFA).

One daemon thread per polling id runs ``mm wallet requests watch <id>`` (the CLI does the polling,
up to 600 s per call) and, when the job finishes or the wait budget is exhausted, injects a short
notice into the conversation through ``ctx.inject_message``. Best effort by design: any failure is
logged, never raised, and the model can always ask ``mm_requests`` for the live state.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from . import jobs
from . import mm_client as mm

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 5
WATCH_CALL_TIMEOUT = 600          # seconds per `mm wallet requests watch` call (CLI max)
TOTAL_BUDGET = 3600               # give up after an hour of waiting

_ctx: Any = None
_active: Dict[str, threading.Thread] = {}
_lock = threading.Lock()
_stop = threading.Event()


def configure(ctx: Any) -> None:
    global _ctx
    _ctx = ctx
    try:
        ctx.on_unload(_stop.set)
    except Exception:  # pragma: no cover
        pass


def active_ids() -> list:
    with _lock:
        return sorted(_active)


def start(polling_id: str, *, intent: str = "", session_key: Optional[str] = None) -> bool:
    """Start watching *polling_id* unless already watched or at capacity. Returns True when started."""
    if not polling_id or _stop.is_set():
        return False
    with _lock:
        _active.update({k: t for k, t in _active.items() if t.is_alive()})
        if polling_id in _active or len(_active) >= MAX_CONCURRENT:
            return False
        thread = threading.Thread(target=_run, args=(polling_id, intent, session_key),
                                  name=f"metamask-watch-{polling_id[:8]}", daemon=True)
        _active[polling_id] = thread
    thread.start()
    return True


def _run(polling_id: str, intent: str, session_key: Optional[str]) -> None:
    deadline = time.monotonic() + TOTAL_BUDGET
    outcome: Optional[Dict[str, Any]] = None
    try:
        while not _stop.is_set() and time.monotonic() < deadline:
            result = mm.run(["wallet", "requests", "watch", polling_id, "--wallet-timeout", str(WATCH_CALL_TIMEOUT)],
                            timeout=WATCH_CALL_TIMEOUT + 30)
            summary = jobs.summarize(result, intent)
            code = str((result.get("error") or {}).get("code", "")).upper() if not result.get("ok") else ""
            if code in {"JOB_TIMEOUT", "RELAY_TIMEOUT", "MM_TIMEOUT"}:
                continue  # still waiting on the human; keep watching
            if code == "REQUEST_NOT_FOUND":
                outcome = summary
                break
            if result.get("ok") and jobs.is_pending(result):
                time.sleep(5)
                continue
            outcome = summary
            break
        if outcome is None:
            outcome = {"ok": False, "intent": intent, "polling_id": polling_id, "status": "STILL_PENDING",
                       "note": "Stopped watching after one hour; ask mm_requests for the live state."}
        _notify(polling_id, outcome, session_key)
    except Exception:  # pragma: no cover - never let a watcher crash the host
        logger.warning("metamask-wallet watcher failed for %s", polling_id, exc_info=True)
    finally:
        with _lock:
            _active.pop(polling_id, None)


def format_notice(polling_id: str, outcome: Dict[str, Any]) -> str:
    what = outcome.get("intent") or f"request {polling_id}"
    st = outcome.get("status") or ("done" if outcome.get("ok") else "failed")
    parts = [f"MetaMask Agent Wallet update — {what}: {st}."]
    if outcome.get("tx_hash"):
        parts.append(f"Tx {outcome['tx_hash']}")
    if outcome.get("explorer_url"):
        parts.append(outcome["explorer_url"])
    if outcome.get("signature"):
        parts.append("Signature ready (ask mm_requests to see it).")
    if outcome.get("failure_reason"):
        parts.append(f"Reason: {outcome['failure_reason']}")
    err = outcome.get("error")
    if isinstance(err, dict) and err.get("message"):
        parts.append(f"Error {err.get('code', '')}: {err['message']}")
    parts.append("Tell the user in one sentence.")
    return " ".join(str(p) for p in parts)


def _notify(polling_id: str, outcome: Dict[str, Any], session_key: Optional[str]) -> None:
    if _ctx is None:
        return
    text = format_notice(polling_id, outcome)
    try:
        delivered = _ctx.inject_message(text, role="system", session_key=session_key)
    except Exception:
        delivered = False
        logger.debug("metamask-wallet: inject_message raised", exc_info=True)
    if not delivered:
        logger.info("metamask-wallet: %s", text)
