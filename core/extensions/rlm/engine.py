"""The RLM iteration loop.

Root LLM writes ```repl``` blocks -> child executes them -> truncated output
feeds the next turn -> until model code sets answer["ready"] = True or a cap
trips. Never returns empty-handed: iteration cap triggers one answer-synthesis
call, and every abnormal exit carries the best partial answer.

Loop adapted from the Recursive Language Models reference implementation
(https://github.com/alexzhang13/rlm, MIT License, Copyright (c) 2025
Alex Zhang), with their client stack replaced by injected chat callables
(Pernix's LLMClient in production, scripted fakes in tests) and USD/token
budget tracking replaced by Pernix's caps.

Blocking by design: runs on a tool-executor thread, NEVER on the event loop
(guarded, following core/extensions/candor/bridge.py).
"""

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path

from core.extensions.rlm.broker import LLMBroker, SubcallLedger, SubChatFn
from core.extensions.rlm.child_env import ChildREPL, StagedContext
from core.extensions.rlm.parsing import find_code_blocks, format_iteration
from core.extensions.rlm.prompts import (
    BUDGET_NOTICE,
    NO_BLOCK_NUDGE,
    build_system_messages,
    build_turn_prompt,
)
from core.extensions.rlm.types import (
    CellResult,
    RLMBudgetExhausted,
    RLMCancelled,
    RLMCaps,
    RLMRunError,
    RLMRunResult,
    RLMTimeout,
)

logger = logging.getLogger(__name__)

# root_chat seam: (messages, timeout) -> response text.
RootChatFn = Callable[[list[dict], float], str]

# Root history sliding window (v1 stand-in for upstream's LLM compaction).
ROOT_HISTORY_MAX_CHARS = 400_000
# Skip the final synthesis call when less wall clock than this remains.
MIN_SYNTHESIS_SECONDS = 30.0
# Per-field cap in payloads.jsonl (full root responses and sub-call
# prompt/response pairs — the durable knowledge store a continuation reloads;
# the trace keeps only previews). Generous: the point is completeness.
PAYLOAD_FIELD_CAP_CHARS = 300_000
# Liveness pulse cadence while a root call / cell is in flight. Heartbeats go
# to progress_fn only, never trace.jsonl — the trace records signal (what the
# run did), not the fact that time passed (dream journal precedent).
HEARTBEAT_INTERVAL_SECONDS = 10.0

_SYNTHESIS_MSG = {
    "role": "user",
    "content": (
        "You are out of REPL turns. Based on everything above, provide your best "
        "final answer to the original query now, as plain text (no ```repl``` block)."
    ),
}


class RLMEngine:
    def __init__(
        self,
        *,
        run_dir: Path,
        task: str,
        staged: StagedContext,
        root_chat: RootChatFn,
        sub_chat: SubChatFn,
        caps: RLMCaps,
        python_exe: str | None = None,
        address_space_limit: int | None = None,
        allowed_models: set[str] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        depth: int = 0,
        ledger: SubcallLedger | None = None,
        deadline: float | None = None,
        on_child_spawn: Callable | None = None,
        rlm_fn: Callable[[str, str | None], str] | None = None,
        progress_fn: Callable[[dict], None] | None = None,
        continuation_note: str | None = None,
    ):
        self.run_dir = Path(run_dir)
        self.task = task
        self.staged = staged
        self._root_chat = root_chat
        self._sub_chat = sub_chat
        self.caps = caps
        self._python_exe = python_exe
        self._as_limit = address_space_limit
        self._allowed_models = allowed_models
        self._cancel_check = cancel_check
        self.depth = depth
        self.ledger = ledger if ledger is not None else SubcallLedger(caps.max_subcalls)
        self._deadline = deadline
        # Called with the child Popen right after spawn — the tool uses it to
        # register session._active_process so cancel/dispatch-timeout kill paths work.
        self._on_child_spawn = on_child_spawn
        # Real rlm_query recursion: (prompt, model) -> answer, running a nested
        # engine on the broker handler thread. None -> rlm_query degrades to
        # llm_query (the broker's fallback). Settable after construction so the
        # tool glue can close over this engine when building the callback.
        self.rlm_fn = rlm_fn
        # Live-progress seam: called (best-effort, any thread) with every trace
        # event plus periodic {"type": "heartbeat"} pulses. The tool glue uses
        # it to feed SSE + mid-run DB counters; None = fully silent (tests,
        # dream probe, nested runs).
        self._progress_fn = progress_fn
        # Extra user-message paragraph for continue_from runs: tells the root
        # what run it continues and what the `prior` REPL variable holds.
        self._continuation_note = continuation_note
        # Set at run() start; nested runs share the remaining wall clock.
        self.deadline: float | None = None
        self._trace_lock = threading.Lock()
        self._trace_fh = None
        self._payload_fh = None
        self._best_partial: str | None = None
        # Latest non-empty answer["content"] snapshot (write-as-you-go). The
        # preferred salvage: deliberate accumulated findings, not the last
        # root response (which is usually a plan or a code block).
        self._draft: str | None = None
        self._iterations = 0
        self._heartbeat_stop: threading.Event | None = None

    # ---- public API ----

    def run(self) -> RLMRunResult:
        self._assert_off_loop()
        start = time.monotonic()
        deadline = self._deadline if self._deadline is not None else start + self.caps.timeout_seconds
        self.deadline = deadline

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._trace_fh = open(self.run_dir / "trace.jsonl", "a", encoding="utf-8")
        self._payload_fh = open(self.run_dir / "payloads.jsonl", "a", encoding="utf-8")

        # Child first: it resolves the socket directory (short-/tmp fallback
        # for deep run dirs), and the broker must bind exactly where the
        # child will dial.
        child_kwargs = {"python_exe": self._python_exe}
        if self._as_limit is not None:
            child_kwargs["address_space_limit"] = self._as_limit
        child = ChildREPL(self.run_dir, **child_kwargs)
        self.child = child  # exposed so the tool can register popen as the session's active process

        broker = LLMBroker(
            child.llm_sock_path,
            sub_chat=self._sub_chat,
            caps=self.caps,
            ledger=self.ledger,
            allowed_models=self._allowed_models,
            deadline=deadline,
            trace_fn=self._trace,
            payload_fn=self._payload,
            rlm_fn=self.rlm_fn,
        )

        result: RLMRunResult | None = None
        try:
            broker.start()
            self._start_heartbeat(broker, start)
            child.start()
            if self._on_child_spawn is not None and child.popen is not None:
                self._on_child_spawn(child.popen)
            child.load_context(self.staged)
            result = self._loop(child, broker, deadline, start)
        except RLMTimeout as e:
            result = self._salvage("timeout", e, start)
        except RLMCancelled as e:
            result = self._salvage("cancelled", e, start)
        except RLMBudgetExhausted as e:
            result = self._salvage("budget_exhausted", e, start)
        except RLMRunError as e:  # RLMChildDied, root-call failures
            result = self._salvage("failed", e, start)
        finally:
            if self._heartbeat_stop is not None:
                self._heartbeat_stop.set()
            child.cleanup()
            broker.stop()
            if result is not None:
                self._trace(
                    {
                        "type": "end",
                        "status": result.status,
                        "iterations": result.iterations,
                        "subcalls": result.subcalls,
                        "duration": round(result.duration, 3),
                        "answer_preview": result.answer[:500],
                    }
                )
            self._finalize_trace()  # unexpected exceptions propagate, but never leak the fh
        self._write_answer(result)
        return result

    # ---- the loop ----

    def _loop(self, child: ChildREPL, broker: LLMBroker, deadline: float, start: float) -> RLMRunResult:
        messages = build_system_messages(
            self.task,
            self.staged.context_type,
            self.staged.total_chars,
            self.staged.file_names,
            continuation=self._continuation_note,
        )
        budget_notified = False
        iterations = 0

        for i in range(self.caps.max_iterations):
            self._check_cancel()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RLMTimeout("run wall clock expired", partial_answer=self._best_partial)

            if (broker.budget_exhausted or self.ledger.exhausted) and not budget_notified:
                messages.append({"role": "user", "content": BUDGET_NOTICE})
                budget_notified = True
                self._trace({"type": "notice", "notice": "budget_exhausted", "iteration": i})

            messages.append(build_turn_prompt(i, self.caps.max_iterations))
            response = self._call_root(messages, remaining)
            iterations = self._iterations = i + 1
            if response and response.strip():
                self._best_partial = response
            self._trace({"type": "root", "iteration": i, "response_preview": response[:500]})
            self._payload({"kind": "root", "iteration": i, "content": response})

            blocks = find_code_blocks(response)
            if not blocks:
                messages.append({"role": "assistant", "content": response})
                messages.append({"role": "user", "content": NO_BLOCK_NUDGE})
                continue

            cells: list[CellResult] = []
            final_answer: str | None = None
            for block in blocks:
                cell = child.execute_cell(
                    block,
                    deadline=deadline,
                    cancel_check=self._cancel_check,
                    in_flight=broker.in_flight,
                    last_activity=broker.last_activity,
                )
                cells.append(cell)
                self._record_draft(cell.draft)
                self._trace(
                    {
                        "type": "cell",
                        "iteration": i,
                        "code": block[:4000],
                        "stdout_preview": cell.stdout[:2000],
                        "stderr_preview": cell.stderr[:2000],
                        "duration": round(cell.duration, 3),
                        "final": cell.final_answer is not None,
                    }
                )
                if cell.final_answer is not None:
                    final_answer = cell.final_answer
                    break

            if final_answer is not None:
                return RLMRunResult(
                    answer=final_answer,
                    status="completed",
                    iterations=iterations,
                    subcalls=self.ledger.count,
                    duration=time.monotonic() - start,
                )

            messages.extend(format_iteration(response, cells))
            self._trim_messages(messages)

        # Iteration cap: one synthesis call, then the draft, then best partial.
        answer = None
        remaining = deadline - time.monotonic()
        if remaining >= MIN_SYNTHESIS_SECONDS:
            try:
                answer = self._call_root(messages + [_SYNTHESIS_MSG], remaining)
                self._trace({"type": "synthesis", "response_preview": (answer or "")[:500]})
                self._payload({"kind": "synthesis", "content": answer or ""})
            except Exception as e:
                logger.warning("RLM synthesis call failed: %s", e)
        if not answer or not answer.strip():
            answer = self._draft or self._best_partial or "(no answer produced before the iteration cap)"
        return RLMRunResult(
            answer=answer,
            status="iteration_cap",
            iterations=iterations,
            subcalls=self.ledger.count,
            duration=time.monotonic() - start,
            partial=True,
        )

    # ---- helpers ----

    def _call_root(self, messages: list[dict], timeout: float) -> str:
        try:
            return self._root_chat(messages, timeout)
        except RLMBudgetExhausted:
            raise
        except Exception as e:
            raise RLMRunError(
                f"root model call failed: {type(e).__name__}: {e}",
                partial_answer=self._best_partial,
            ) from e

    def _check_cancel(self) -> None:
        if self._cancel_check is not None and self._cancel_check():
            raise RLMCancelled("session cancelled", partial_answer=self._best_partial)

    def _salvage(self, status: str, e: Exception, start: float) -> RLMRunResult:
        # Draft first: answer["content"] is the root's deliberate running
        # answer, while the exception's partial / last root response is
        # usually a plan or code block (session a45fa830cef9's timeout run
        # returned its next-batch ```repl``` plan as the "answer" while eight
        # chapters of findings died with the child).
        partial = self._draft or getattr(e, "partial_answer", None) or self._best_partial
        logger.info("RLM run ended early (%s): %s", status, e)
        # No placeholder text: an empty answer is how the tool layer knows to
        # return an Error:-prefixed result instead of a fake best-effort one.
        return RLMRunResult(
            answer=partial or "",
            status=status,
            iterations=self._iterations,
            subcalls=self.ledger.count,
            duration=time.monotonic() - start,
            partial=True,
            error=str(e),
        )

    @staticmethod
    def _trim_messages(messages: list[dict]) -> None:
        """Sliding window: keep the system + task head, elide oldest turns."""
        total = sum(len(m.get("content") or "") for m in messages)
        if total <= ROOT_HISTORY_MAX_CHARS or len(messages) <= 6:
            return
        dropped = 0
        while total > ROOT_HISTORY_MAX_CHARS and len(messages) > 6:
            removed = messages.pop(2)
            total -= len(removed.get("content") or "")
            dropped += 1
        messages.insert(
            2,
            {
                "role": "user",
                "content": (
                    f"[{dropped} earlier turn message(s) elided to fit your window. "
                    "Variables you created still exist in the REPL — use SHOW_VARS() if unsure.]"
                ),
            },
        )

    def _trace(self, event: dict) -> None:
        event = {"ts": round(time.time(), 3), "depth": self.depth, **event}
        if self._trace_fh is not None:
            with self._trace_lock:
                try:
                    self._trace_fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                    self._trace_fh.flush()
                except (OSError, ValueError):
                    pass  # trace is best-effort; never fail the run over it
        self._emit_progress(event)

    def _payload(self, event: dict) -> None:
        """Append to payloads.jsonl — full root/synthesis responses and
        sub-call prompt/response pairs. This is the run's durable knowledge
        store: everything that cost LLM time, so a continue_from run can
        reload it instead of paying for it again. Never emitted as progress
        (megabytes don't belong on the SSE stream). Called from the engine
        thread (root/synthesis) and broker handler threads (subcall)."""
        event = {"ts": round(time.time(), 3), **event}
        for key in ("content", "prompt", "response"):
            value = event.get(key)
            if isinstance(value, str) and len(value) > PAYLOAD_FIELD_CAP_CHARS:
                event[key] = (
                    value[:PAYLOAD_FIELD_CAP_CHARS] + f"... [{len(value) - PAYLOAD_FIELD_CAP_CHARS} chars truncated]"
                )
        if self._payload_fh is not None:
            with self._trace_lock:
                try:
                    self._payload_fh.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
                    self._payload_fh.flush()
                except (OSError, ValueError):
                    pass  # best-effort, like the trace

    def _record_draft(self, draft: str | None) -> None:
        """Persist the latest answer["content"] snapshot to draft.txt.

        Written every cell it changes, so a run killed mid-cell (wall clock,
        cancel, child death) still leaves its accumulated findings on disk —
        for salvage now and for continue_from later."""
        if not draft or not draft.strip() or draft == self._draft:
            return
        self._draft = draft
        try:
            (self.run_dir / "draft.txt").write_text(draft, encoding="utf-8")
        except OSError:
            logger.debug("failed to persist RLM draft in %s", self.run_dir)

    def _emit_progress(self, event: dict) -> None:
        if self._progress_fn is None:
            return
        try:
            self._progress_fn(event)
        except Exception:  # progress is observability, never control flow
            logger.debug("RLM progress_fn failed", exc_info=True)

    def _start_heartbeat(self, broker: LLMBroker, start: float) -> None:
        """Periodic liveness pulse while progress consumers are attached.

        Trace events land only when a step *completes*, so a 3-minute cell or
        slow root call otherwise reads as frozen. The pulse carries the
        broker's live counters (the one place all delegated work is visible).
        """
        if self._progress_fn is None:
            return
        stop = self._heartbeat_stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(HEARTBEAT_INTERVAL_SECONDS):
                self._emit_progress(
                    {
                        "ts": round(time.time(), 3),
                        "depth": self.depth,
                        "type": "heartbeat",
                        "iteration": self._iterations,
                        "subcalls": self.ledger.count,
                        "in_flight": broker.in_flight(),
                        "quiet_seconds": round(max(0.0, time.monotonic() - broker.last_activity()), 1),
                        "elapsed": round(time.monotonic() - start, 1),
                    }
                )

        threading.Thread(target=_beat, name="rlm-heartbeat", daemon=True).start()

    def _finalize_trace(self) -> None:
        with self._trace_lock:
            for attr in ("_trace_fh", "_payload_fh"):
                fh = getattr(self, attr)
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    setattr(self, attr, None)

    def _write_answer(self, result: RLMRunResult) -> None:
        try:
            body = result.answer or f"(no answer — run ended {result.status}: {result.error})"
            (self.run_dir / "answer.txt").write_text(body, encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _assert_off_loop() -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError("RLMEngine.run() must not be called on the event loop — use a tool executor thread")
