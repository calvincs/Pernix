"""The RLM child REPL process.

Runs model-written code cells in a persistent namespace, brokering every
sub-LLM call back to the parent over a unix socket — the child never holds
credentials. Adapted from the Recursive Language Models reference
implementation (https://github.com/alexzhang13/rlm, MIT License,
Copyright (c) 2025 Alex Zhang): the safe-builtins table, ``_AnswerDict``,
and scaffold-restore semantics come from its LocalREPL; the out-of-process
``llm_query`` stubs from its docker exec script.

HARD CONSTRAINT: stdlib only, zero sibling imports (the frame protocol is
inlined below). The child runs under the workspace venv interpreter, which
does not have Pernix's dependencies installed, and the package directory is
deliberately stripped from sys.path.

The restricted builtins are a behavioral guardrail, not a security boundary —
trivially escapable by design. Real containment: scrubbed env (no secrets),
rlimits + setsid applied by the parent at spawn, brokered/budgeted LLM
access, and kill-ability. See docs/internals/rlm.md.
"""

import sys

# Run-as-a-script puts THIS directory at sys.path[0], where our types.py /
# parsing.py would shadow the stdlib for every subsequent import (including
# model code's). Strip it before anything else is imported.
_HERE_DIR = __file__.rsplit("/", 1)[0]
sys.path[:] = [p for p in sys.path if p not in ("", ".", _HERE_DIR)]

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import signal  # noqa: E402
import socket  # noqa: E402
import struct  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402

# Child-local copy of the frame protocol (see protocol.py) so this file has
# zero sibling imports and stays runnable with the package dir off sys.path.
_LEN = struct.Struct(">I")
_MAX_FRAME_BYTES = 64 * 1024 * 1024


def send_frame(sock, payload):
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(data) > _MAX_FRAME_BYTES:
        raise ValueError(f"frame of {len(data)} bytes exceeds {_MAX_FRAME_BYTES} cap")
    sock.sendall(_LEN.pack(len(data)) + data)


def recv_frame(sock):
    header = _recv_exact(sock, _LEN.size)
    (length,) = _LEN.unpack(header)
    if length > _MAX_FRAME_BYTES:
        raise ValueError(f"declared frame of {length} bytes exceeds {_MAX_FRAME_BYTES} cap")
    return json.loads(_recv_exact(sock, length).decode("utf-8"))


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed mid-frame" if buf else "socket closed")
        buf.extend(chunk)
    return bytes(buf)


# Parent enforces its own (shorter) deadlines and always answers, even if only
# with an error frame; these exist so a vanished parent can't hang us forever.
_LLM_STUB_TIMEOUT = 900.0
_RLM_STUB_TIMEOUT = 3900.0

# Cap on the answer["content"] draft shipped back with each exec_result, so a
# runaway draft can't blow up the result frame.
_DRAFT_CAP_CHARS = 200_000

_SAFE_BUILTINS = {
    # Core types and functions
    "print": print,
    "len": len,
    "str": str,
    "int": int,
    "float": float,
    "list": list,
    "dict": dict,
    "set": set,
    "frozenset": frozenset,
    "tuple": tuple,
    "bool": bool,
    "type": type,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "enumerate": enumerate,
    "zip": zip,
    "map": map,
    "filter": filter,
    "sorted": sorted,
    "reversed": reversed,
    "range": range,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
    "any": any,
    "all": all,
    "pow": pow,
    "divmod": divmod,
    "chr": chr,
    "ord": ord,
    "hex": hex,
    "bin": bin,
    "oct": oct,
    "repr": repr,
    "ascii": ascii,
    "format": format,
    "hash": hash,
    "id": id,
    "iter": iter,
    "next": next,
    "slice": slice,
    "callable": callable,
    "hasattr": hasattr,
    "getattr": getattr,
    "setattr": setattr,
    "delattr": delattr,
    "dir": dir,
    "vars": vars,
    "bytes": bytes,
    "bytearray": bytearray,
    "memoryview": memoryview,
    "complex": complex,
    "object": object,
    "super": super,
    "property": property,
    "staticmethod": staticmethod,
    "classmethod": classmethod,
    "__import__": __import__,
    "open": open,
    # Exceptions
    "Exception": Exception,
    "BaseException": BaseException,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "KeyError": KeyError,
    "IndexError": IndexError,
    "AttributeError": AttributeError,
    "FileNotFoundError": FileNotFoundError,
    "OSError": OSError,
    "IOError": IOError,
    "RuntimeError": RuntimeError,
    "NameError": NameError,
    "ImportError": ImportError,
    "ModuleNotFoundError": ModuleNotFoundError,
    "StopIteration": StopIteration,
    "GeneratorExit": GeneratorExit,
    "KeyboardInterrupt": KeyboardInterrupt,
    "ZeroDivisionError": ZeroDivisionError,
    "UnicodeDecodeError": UnicodeDecodeError,
    "UnicodeEncodeError": UnicodeEncodeError,
    "AssertionError": AssertionError,
    "NotImplementedError": NotImplementedError,
    "ArithmeticError": ArithmeticError,
    "OverflowError": OverflowError,
    "RecursionError": RecursionError,
    "MemoryError": MemoryError,
    "LookupError": LookupError,
    "Warning": Warning,
    # Blocked (present-but-None so the failure mode is an obvious TypeError)
    "input": None,
    "eval": None,
    "exec": None,
    "compile": None,
    "globals": None,
    "locals": None,
    "exit": None,
    "quit": None,
    "breakpoint": None,
    "help": None,
}


class _AnswerDict(dict):
    """REPL-visible dict where ``answer["ready"] = True`` signals completion."""

    def __init__(self, on_ready=None):
        super().__init__()
        super().__setitem__("content", "")
        super().__setitem__("ready", False)
        self._on_ready = on_ready

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key == "ready" and value and self._on_ready is not None:
            try:
                self._on_ready(self.get("content", ""))
            except Exception:
                pass


class ChildREPLRunner:
    def __init__(self, llm_sock_path: str, scaffold: str = "rlm"):
        self._llm_sock_path = llm_sock_path
        self._final_answer = None
        # "rlm" installs the sub-LLM stubs + answer dict; "plain" (session
        # kernels, adaptation plan 2a) omits them — there is no broker socket
        # to dial, and the stubs would hang against it. The restricted
        # builtins table stays in both modes.
        self.scaffold_mode = scaffold if scaffold in ("rlm", "plain") else "rlm"
        self.ns = {
            "__builtins__": dict(_SAFE_BUILTINS),
            "__name__": "__main__",
        }
        if self.scaffold_mode == "rlm":
            self._scaffold = {
                "llm_query": self._llm_query,
                "llm_query_batched": self._llm_query_batched,
                "rlm_query": self._rlm_query,
                "rlm_query_batched": self._rlm_query_batched,
                "SHOW_VARS": self._show_vars,
            }
            self.ns.update(self._scaffold)
            self.ns["answer"] = _AnswerDict(on_ready=self._capture_answer)
        else:
            self._scaffold = {}
        self._hidden = set(self.ns) | {"answer"}

    # ---- sub-LLM stubs (brokered to the parent; no credentials here) ----

    def _broker(self, payload: dict, timeout: float) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(self._llm_sock_path)
            send_frame(s, payload)
            return recv_frame(s)

    def _llm_query(self, prompt, model=None):
        try:
            reply = self._broker({"type": "llm", "prompt": str(prompt), "model": model}, _LLM_STUB_TIMEOUT)
            return reply.get("response") if reply.get("ok") else f"Error: {reply.get('error')}"
        except Exception as e:
            return f"Error: LM query failed - {e}"

    def _llm_query_batched(self, prompts, model=None):
        prompts = [str(p) for p in prompts]
        try:
            reply = self._broker({"type": "llm_batched", "prompts": prompts, "model": model}, _LLM_STUB_TIMEOUT)
            if reply.get("ok"):
                return reply.get("responses")
            return [f"Error: {reply.get('error')}"] * len(prompts)
        except Exception as e:
            return [f"Error: LM query failed - {e}"] * len(prompts)

    def _rlm_query(self, prompt, model=None):
        try:
            reply = self._broker({"type": "rlm", "prompt": str(prompt), "model": model}, _RLM_STUB_TIMEOUT)
            return reply.get("response") if reply.get("ok") else f"Error: {reply.get('error')}"
        except Exception as e:
            return f"Error: RLM query failed - {e}"

    def _rlm_query_batched(self, prompts, model=None):
        prompts = [str(p) for p in prompts]
        try:
            reply = self._broker({"type": "rlm_batched", "prompts": prompts, "model": model}, _RLM_STUB_TIMEOUT)
            if reply.get("ok"):
                return reply.get("responses")
            return [f"Error: {reply.get('error')}"] * len(prompts)
        except Exception as e:
            return [f"Error: RLM query failed - {e}"] * len(prompts)

    # ---- namespace helpers ----

    def _capture_answer(self, content):
        self._final_answer = str(content)

    def _visible_vars(self):
        return {k: type(v).__name__ for k, v in self.ns.items() if not k.startswith("_") and k not in self._hidden}

    def _show_vars(self):
        available = self._visible_vars()
        if not available:
            return "No variables created yet. Use ```repl``` blocks to create variables."
        return f"Available variables: {available}"

    def _restore_scaffold(self):
        """Undo model overwrites of scaffold names so the next cell still works."""
        self.ns["__builtins__"] = dict(_SAFE_BUILTINS)
        if self.scaffold_mode != "rlm":
            return  # plain mode: only the builtins guardrail is restored
        self.ns.update(self._scaffold)
        current = self.ns.get("answer")
        if not isinstance(current, _AnswerDict):
            replacement = _AnswerDict(on_ready=self._capture_answer)
            if isinstance(current, dict):
                for k, v in current.items():
                    dict.__setitem__(replacement, k, v)
                if current.get("ready") and self._final_answer is None:
                    self._final_answer = str(current.get("content", ""))
            self.ns["answer"] = replacement
        if "context_0" in self.ns:
            self.ns["context"] = self.ns["context_0"]

    # ---- frame handlers ----

    def load_context(self, frame: dict) -> dict:
        try:
            total = 0
            for item in frame.get("items", []):
                with open(item["path"], encoding="utf-8", errors="replace") as f:
                    value = json.load(f) if item.get("format") == "json" else f.read()
                self.ns[item["var"]] = value
                total += len(value) if isinstance(value, str) else sum(len(x) for x in value)
            for name, value in (frame.get("extra_vars") or {}).items():
                self.ns[name] = value
            if "context_0" in self.ns:
                self.ns["context"] = self.ns["context_0"]
            return {"type": "load_result", "ok": True, "chars": total}
        except Exception as e:
            return {"type": "load_result", "ok": False, "error": f"{type(e).__name__}: {e}"}

    def snapshot(self, frame: dict) -> dict:
        """Serialize the namespace ONE top-level name at a time (adaptation
        plan 2a): one unpicklable object (socket, handle) is skipped and
        reported instead of aborting the whole snapshot. The file is written
        by this process directly (atomically, tmp+rename) so payload size is
        never bounded by the frame cap; the reply carries only names."""
        path = frame.get("path", "")
        max_bytes = int(frame.get("max_bytes") or 256 * 1024 * 1024)
        try:
            import dill
        except ImportError:
            return {
                "type": "snapshot_result",
                "ok": False,
                "error": "dill not importable in the kernel interpreter",
            }
        import types as _types

        stored: dict[str, bytes] = {}
        skipped: dict[str, str] = {}
        total = 0
        for name in sorted(self.ns):
            if name.startswith("_") or name in self._hidden:
                continue
            value = self.ns[name]
            if isinstance(value, _types.ModuleType):
                skipped[name] = "module (re-import on revival)"
                continue
            try:
                blob = dill.dumps(value)
            except (Exception, RecursionError) as e:
                skipped[name] = f"{type(e).__name__}: {e}"[:200]
                continue
            if total + len(blob) > max_bytes:
                skipped[name] = f"size cap ({len(blob)} bytes would exceed {max_bytes} total)"
                continue
            stored[name] = blob
            total += len(blob)
        try:
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                dill.dump(stored, f)
            os.replace(tmp, path)
        except Exception as e:
            return {"type": "snapshot_result", "ok": False, "error": f"{type(e).__name__}: {e}"}
        return {
            "type": "snapshot_result",
            "ok": True,
            "stored": sorted(stored),
            "skipped": skipped,
            "bytes": total,
        }

    def restore(self, frame: dict) -> dict:
        """Load a snapshot per-name: one stale/unloadable blob fails alone."""
        path = frame.get("path", "")
        try:
            import dill
        except ImportError:
            return {
                "type": "restore_result",
                "ok": False,
                "error": "dill not importable in the kernel interpreter",
            }
        try:
            with open(path, "rb") as f:
                blobs = dill.load(f)
        except Exception as e:
            return {"type": "restore_result", "ok": False, "error": f"{type(e).__name__}: {e}"}
        restored: list[str] = []
        failed: dict[str, str] = {}
        for name, blob in blobs.items():
            if name in self._hidden:
                continue  # never clobber scaffold/guardrail names
            try:
                self.ns[name] = dill.loads(blob)
                restored.append(name)
            except Exception as e:
                failed[name] = f"{type(e).__name__}: {e}"[:200]
        return {"type": "restore_result", "ok": True, "restored": sorted(restored), "failed": failed}

    def execute(self, frame: dict) -> dict:
        code = frame.get("code", "")
        start = time.monotonic()
        self._final_answer = None
        old_out, old_err = sys.stdout, sys.stderr
        out_buf, err_buf = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr = out_buf, err_buf
        try:
            code_obj = compile(code, "<cell>", "exec")
            exec(code_obj, self.ns, self.ns)
        except (Exception, KeyboardInterrupt, SystemExit) as e:
            err_buf.write(_format_cell_error(e))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        self._restore_scaffold()
        final = self._final_answer
        self._final_answer = None
        # Write-as-you-go: the current answer["content"], ready or not. The
        # parent persists it each cell, so a killed run salvages accumulated
        # findings instead of losing them with this process.
        try:
            draft = str(self.ns["answer"].get("content", "") or "")
        except Exception:
            draft = ""
        return {
            "type": "exec_result",
            "id": frame.get("id"),
            "stdout": out_buf.getvalue(),
            "stderr": err_buf.getvalue(),
            "final_answer": final,
            "draft": draft[:_DRAFT_CAP_CHARS] if draft.strip() else None,
            "duration": time.monotonic() - start,
            "var_names": [f"{k}:{t}" for k, t in sorted(self._visible_vars().items())],
        }


def _format_cell_error(e: BaseException) -> str:
    """Traceback showing only the model's own <cell> frames, not runner internals."""
    lines = []
    for fs in traceback.extract_tb(e.__traceback__):
        if fs.filename == "<cell>":
            lines.append(f'  File "<cell>", line {fs.lineno}, in {fs.name}\n')
            if fs.line:
                lines.append(f"    {fs.line}\n")
    lines.extend(traceback.format_exception_only(type(e), e))
    return "".join(lines)


def _arm_parent_watch():
    """Best-effort: die with the parent PROCESS.

    This used to be prctl(PR_SET_PDEATHSIG, SIGKILL) — which is scoped to the
    spawning THREAD, not the process (documented in prctl(2), easy to miss).
    Any child spawned from a short-lived thread was SIGKILLed the moment that
    thread exited, with the parent process alive and the child mid-protocol:
    the parent's next round-trip surfaced as "connection lost mid-cell:
    Connection reset by peer" with an empty child.log, because SIGKILL leaves
    no time to write one. Production mostly dodged it by luck — repl kernels
    ride the immortal pernix-tool pool threads and result-binding rides the
    loop's default executor — but the two test_repl_tool binding tests spawn
    inside asyncio.run(), whose teardown retires the executor thread, and
    they died on exactly this (~0.8s after the round, exit -9).

    A ppid poll has the semantics the docstring always claimed: when the
    parent PROCESS dies we are reparented (init or the nearest subreaper),
    getppid() changes, and we exit. The 2s cadence is fine for orphan
    hygiene — the socket-timeout watchdogs already stop a vanished parent
    from hanging a cell (see the frame-timeout notes above).
    """
    parent = os.getppid()
    if parent <= 1:  # already orphaned at startup; nothing to watch
        return

    def _watch():
        while True:
            time.sleep(2.0)
            if os.getppid() != parent:
                os._exit(0)

    threading.Thread(target=_watch, daemon=True, name="parent-watch").start()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exec-sock", required=True)
    parser.add_argument("--llm-sock", required=True)
    parser.add_argument("--scaffold", choices=("rlm", "plain"), default="rlm")
    args = parser.parse_args()

    _arm_parent_watch()
    # Default handler so the parent's SIGINT raises KeyboardInterrupt in a
    # running cell (aborting the cell, preserving the namespace).
    signal.signal(signal.SIGINT, signal.default_int_handler)

    runner = ChildREPLRunner(args.llm_sock, scaffold=args.scaffold)

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.connect(args.exec_sock)
    send_frame(conn, {"type": "hello", "pid": os.getpid()})

    while True:
        try:
            frame = recv_frame(conn)
        except KeyboardInterrupt:
            continue  # stray interrupt between cells (cell already finished)
        except (EOFError, ConnectionError, OSError):
            return 0  # parent went away — self-reap
        ftype = frame.get("type")
        if ftype == "exec":
            reply = runner.execute(frame)
        elif ftype == "load_context":
            reply = runner.load_context(frame)
        elif ftype == "snapshot":
            reply = runner.snapshot(frame)
        elif ftype == "restore":
            reply = runner.restore(frame)
        elif ftype == "shutdown":
            send_frame(conn, {"type": "bye"})
            return 0
        else:
            reply = {"type": "error", "error": f"unknown frame type: {ftype}"}
        # A watchdog SIGINT can race the end of a cell and land here instead of
        # inside exec; the reply must still go out or the parent hangs.
        for _ in range(2):
            try:
                send_frame(conn, reply)
                break
            except KeyboardInterrupt:
                continue
            except (ConnectionError, OSError):
                return 0


if __name__ == "__main__":
    sys.exit(main())
