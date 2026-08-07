"""Sub-LLM call broker: serves the child's llm_query / rlm_query requests.

A ThreadingUnixStreamServer on <run_dir>/llm.sock. Every budget decision
(sub-call count, concurrency, model allowlist, recursion depth) happens here,
parent-side — child-supplied values are never trusted.

Adapted from the Recursive Language Models reference implementation's
LMHandler (https://github.com/alexzhang13/rlm, MIT License,
Copyright (c) 2025 Alex Zhang), re-pointed at an injected chat callable so
Pernix's LLMClient (or a test fake) does the actual completion.
"""

import contextlib
import logging
import socketserver
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from core.extensions.rlm.protocol import FrameError, recv_frame, send_frame
from core.extensions.rlm.types import RLMBudgetExhausted, RLMCaps

logger = logging.getLogger(__name__)

# Per-sub-call ceiling (upstream's in-container value); further bounded by the
# run's remaining wall clock.
SUBCALL_TIMEOUT = 600.0

# Failures that mean the request never reached the model: nothing was
# generated, so the sub-call ledger debit is handed back (see _call_one).
# Everything else - HTTP status errors, malformed responses, a model that
# answered badly - keeps its debit, because the ledger caps *model work*.
TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    ConnectionError,  # builtin: refused / reset / aborted sockets
)

# sub_chat seam: (prompt, model, timeout) -> response text. Raises on failure;
# raises RLMBudgetExhausted when the session's LLM budget is gone.
SubChatFn = Callable[[str, str | None, float], str]


class SubcallLedger:
    """Counts every sub-LLM call in a run (shared across recursion depths)."""

    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._count = 0

    def try_debit(self) -> bool:
        with self._lock:
            if self._count >= self.limit:
                return False
            self._count += 1
            return True

    def refund(self) -> None:
        """Give a debit back - for a sub-call that failed before reaching the
        model, so a flaky transport doesn't quietly eat the run's budget."""
        with self._lock:
            self._count = max(0, self._count - 1)

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._count >= self.limit


class SubcallLimiter:
    """Bounds in-flight sub-LLM calls across EVERY recursion depth at once.

    Owned by the root engine and handed down to each nested engine's broker,
    exactly like the shared SubcallLedger. Before this existed every nested
    engine minted its own semaphore, so peak concurrency was
    max_concurrent_subcalls ** max_depth (27 in-flight calls at the shipped
    depth=3 / concurrency=3) instead of max_concurrent_subcalls.

    Invariant that keeps this deadlock-free: a holder of a slot must never
    wait on work that needs a slot. Hence the rlm_query recursion wrapper
    takes no slot - only leaf sub-LLM calls do.
    """

    def __init__(self, limit: int):
        self.limit = max(1, int(limit))
        self._sem = threading.Semaphore(self.limit)

    def __enter__(self) -> "SubcallLimiter":
        self._sem.acquire()
        return self

    def __exit__(self, *exc_info) -> bool:
        self._sem.release()
        return False


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        broker: LLMBroker = self.server.broker  # type: ignore[attr-defined]
        try:
            frame = recv_frame(self.request)
        except (EOFError, FrameError, OSError):
            return
        try:
            reply = broker.dispatch(frame)
        except Exception as e:  # never let a bug kill the handler silently
            logger.exception("RLM broker dispatch failed")
            reply = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        try:
            send_frame(self.request, reply)
        except (OSError, FrameError):
            pass  # child gave up waiting; its stub already returned an error


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True


class LLMBroker:
    def __init__(
        self,
        sock_path: Path,
        *,
        sub_chat: SubChatFn,
        caps: RLMCaps,
        ledger: SubcallLedger,
        limiter: "SubcallLimiter | None" = None,
        allowed_models: set[str] | None = None,
        deadline: float | None = None,
        trace_fn: Callable[[dict], None] | None = None,
        payload_fn: Callable[[dict], None] | None = None,
        rlm_fn: Callable[[str, str | None], str] | None = None,
    ):
        self.sock_path = Path(sock_path)
        self._sub_chat = sub_chat
        self._caps = caps
        self.ledger = ledger
        self._allowed_models = allowed_models or set()
        self._deadline = deadline
        self._trace_fn = trace_fn
        # Full prompt/response pairs for the run's durable knowledge store
        # (payloads.jsonl) — the trace records only previews, and sub-call
        # output is the one thing a continuation cannot recompute cheaply.
        self._payload_fn = payload_fn
        self._rlm_fn = rlm_fn

        # Shared across recursion depths when the engine passes one down; a
        # standalone broker (tests, depth-0-only runs) gets its own.
        self._limiter = limiter if limiter is not None else SubcallLimiter(caps.max_concurrent_subcalls)
        # Local fan-out worker pool. Thread count is per-broker, but the calls
        # those threads make all queue behind the shared limiter above, so
        # actual in-flight model work stays at the run-wide cap.
        self._pool = ThreadPoolExecutor(max_workers=caps.max_concurrent_subcalls, thread_name_prefix="rlm-subcall")
        self._state_lock = threading.Lock()
        self._in_flight = 0
        self._last_activity = time.monotonic()
        self.budget_exhausted = False
        self._server: _Server | None = None
        self._server_thread: threading.Thread | None = None

    # ---- lifecycle ----

    def start(self) -> None:
        self.sock_path.unlink(missing_ok=True)
        self._server = _Server(str(self.sock_path), _Handler)
        self._server.broker = self  # type: ignore[attr-defined]
        self._server_thread = threading.Thread(target=self._server.serve_forever, name="rlm-broker", daemon=True)
        self._server_thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        self._pool.shutdown(wait=False, cancel_futures=True)
        self.sock_path.unlink(missing_ok=True)

    # ---- watchdog surface (read by ChildREPL.execute_cell) ----

    def in_flight(self) -> int:
        with self._state_lock:
            return self._in_flight

    def last_activity(self) -> float:
        with self._state_lock:
            return self._last_activity

    def _touch(self, delta: int) -> None:
        with self._state_lock:
            self._in_flight += delta
            self._last_activity = time.monotonic()

    # ---- dispatch ----

    def dispatch(self, frame: dict) -> dict:
        ftype = frame.get("type")
        model = frame.get("model") or None
        if ftype in ("llm", "rlm"):
            fn, limited = self._resolve_fn(ftype)
            ok, text = self._call_one(fn, str(frame.get("prompt", "")), model, limited)
            return {"ok": True, "response": text} if ok else {"ok": False, "error": text}
        if ftype in ("llm_batched", "rlm_batched"):
            fn, limited = self._resolve_fn(ftype.removesuffix("_batched"))
            prompts = [str(p) for p in frame.get("prompts") or []]
            futures = [self._pool.submit(self._call_one, fn, p, model, limited) for p in prompts]
            # Per-item failures come back as inline "Error: ..." strings, the
            # same contract the child's batched stub (and upstream) presents.
            responses = [text if ok else f"Error: {text}" for ok, text in (f.result() for f in futures)]
            return {"ok": True, "responses": responses}
        return {"ok": False, "error": f"unknown request type: {ftype}"}

    def _resolve_fn(self, kind: str) -> tuple[Callable, bool]:
        """Return (callable, consumes_a_concurrency_slot).

        rlm_query degrades to a plain sub-LLM call until recursion is enabled
        (rlm_fn set while depth + 1 < caps.max_depth) — upstream's fallback.
        """
        if kind == "rlm" and self._rlm_fn is not None:
            # The recursion wrapper takes NO slot: it is a supervisor, not
            # model work. Holding one for the nested engine's whole run both
            # wasted a slot for minutes and — now that the limiter is shared
            # across depths — could starve the nested run that must finish
            # before the slot would be released.
            return (lambda prompt, model, _timeout: self._rlm_fn(prompt, model)), False
        return self._sub_chat, True

    def _call_one(self, fn, prompt: str, model: str | None, limited: bool = True) -> tuple[bool, str]:
        if model is not None and self._allowed_models and model not in self._allowed_models:
            return False, f"model '{model}' is not allowed for this run"
        timeout = SUBCALL_TIMEOUT
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return False, "run wall clock expired"
            timeout = min(timeout, remaining + 30.0)
        # Debit last among the pre-flight checks: the rejections above never
        # touched the model, so they must not spend sub-call budget.
        if not self.ledger.try_debit():
            return False, f"sub-call budget exhausted ({self.ledger.limit} calls used)"
        start = time.monotonic()
        self._touch(+1)
        response = None
        # Seeded before the try so the finally block can always trace: a
        # BaseException (KeyboardInterrupt, SystemExit, a cancelled future)
        # skips both except clauses, and unbound names in finally raised
        # NameError that masked the original failure.
        ok, detail = False, "sub-LLM call did not complete"
        try:
            with self._limiter if limited else contextlib.nullcontext():
                response = fn(prompt, model, timeout)
            ok, detail = True, ""
            return True, response
        except RLMBudgetExhausted as e:
            self.budget_exhausted = True
            ok, detail = False, str(e)
            return False, str(e)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
            if isinstance(e, TRANSPORT_ERRORS):
                # Never reached the model -> no tokens burned -> give the
                # budget back. Model-side failures keep their debit.
                self.ledger.refund()
                detail += " [transport failure: sub-call budget refunded]"
            return False, f"sub-LLM call failed - {e}"
        finally:
            self._touch(-1)
            if self._trace_fn is not None:
                self._trace_fn(
                    {
                        "type": "subcall",
                        "prompt_preview": prompt[:200],
                        "model": model,
                        "ok": ok,
                        "error": detail,
                        "duration": round(time.monotonic() - start, 3),
                    }
                )
            if self._payload_fn is not None:
                self._payload_fn(
                    {
                        "kind": "subcall",
                        "model": model,
                        "ok": ok,
                        "prompt": prompt,
                        "response": response if ok else None,
                        "error": detail,
                        "duration": round(time.monotonic() - start, 3),
                    }
                )
