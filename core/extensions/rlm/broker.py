"""Sub-LLM call broker: serves the child's llm_query / rlm_query requests.

A ThreadingUnixStreamServer on <run_dir>/llm.sock. Every budget decision
(sub-call count, concurrency, model allowlist, recursion depth) happens here,
parent-side — child-supplied values are never trusted.

Adapted from the Recursive Language Models reference implementation's
LMHandler (https://github.com/alexzhang13/rlm, MIT License,
Copyright (c) 2025 Alex Zhang), re-pointed at an injected chat callable so
Pernix's LLMClient (or a test fake) does the actual completion.
"""

import logging
import socketserver
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core.extensions.rlm.protocol import FrameError, recv_frame, send_frame
from core.extensions.rlm.types import RLMBudgetExhausted, RLMCaps

logger = logging.getLogger(__name__)

# Per-sub-call ceiling (upstream's in-container value); further bounded by the
# run's remaining wall clock.
SUBCALL_TIMEOUT = 600.0

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

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def exhausted(self) -> bool:
        with self._lock:
            return self._count >= self.limit


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
        allowed_models: set[str] | None = None,
        deadline: float | None = None,
        trace_fn: Callable[[dict], None] | None = None,
        rlm_fn: Callable[[str, str | None], str] | None = None,
    ):
        self.sock_path = Path(sock_path)
        self._sub_chat = sub_chat
        self._caps = caps
        self.ledger = ledger
        self._allowed_models = allowed_models or set()
        self._deadline = deadline
        self._trace_fn = trace_fn
        self._rlm_fn = rlm_fn

        self._sem = threading.Semaphore(caps.max_concurrent_subcalls)
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
            fn = self._resolve_fn(ftype)
            ok, text = self._call_one(fn, str(frame.get("prompt", "")), model)
            return {"ok": True, "response": text} if ok else {"ok": False, "error": text}
        if ftype in ("llm_batched", "rlm_batched"):
            fn = self._resolve_fn(ftype.removesuffix("_batched"))
            prompts = [str(p) for p in frame.get("prompts") or []]
            futures = [self._pool.submit(self._call_one, fn, p, model) for p in prompts]
            # Per-item failures come back as inline "Error: ..." strings, the
            # same contract the child's batched stub (and upstream) presents.
            responses = [text if ok else f"Error: {text}" for ok, text in (f.result() for f in futures)]
            return {"ok": True, "responses": responses}
        return {"ok": False, "error": f"unknown request type: {ftype}"}

    def _resolve_fn(self, kind: str):
        # rlm_query degrades to a plain sub-LLM call until recursion is
        # enabled (rlm_fn set at depth < max_depth) — upstream's fallback.
        if kind == "rlm" and self._rlm_fn is not None:
            return lambda prompt, model, _timeout: self._rlm_fn(prompt, model)
        return self._sub_chat

    def _call_one(self, fn, prompt: str, model: str | None) -> tuple[bool, str]:
        if model is not None and self._allowed_models and model not in self._allowed_models:
            return False, f"model '{model}' is not allowed for this run"
        if not self.ledger.try_debit():
            return False, f"sub-call budget exhausted ({self.ledger.limit} calls used)"
        timeout = SUBCALL_TIMEOUT
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                return False, "run wall clock expired"
            timeout = min(timeout, remaining + 30.0)
        start = time.monotonic()
        self._touch(+1)
        try:
            with self._sem:
                response = fn(prompt, model, timeout)
            ok, detail = True, ""
            return True, response
        except RLMBudgetExhausted as e:
            self.budget_exhausted = True
            ok, detail = False, str(e)
            return False, str(e)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
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
