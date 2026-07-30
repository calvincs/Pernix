"""Framed JSON over stream sockets for the RLM engine.

Wire format: 4-byte big-endian length prefix + UTF-8 JSON payload, adapted from
the Recursive Language Models reference implementation
(https://github.com/alexzhang13/rlm, MIT License, Copyright (c) 2025 Alex Zhang).

Used on both unix domain sockets in a run dir:
  exec.sock — parent -> child code cells and results (one persistent connection)
  llm.sock  — child -> parent sub-LLM brokering (connection per request)

This module must stay importable by ``child_runner.py`` — stdlib only.
"""

import json
import socket
import struct

# A corrupt or hostile length prefix must not OOM either side.
MAX_FRAME_BYTES = 64 * 1024 * 1024

_LEN = struct.Struct(">I")


class FrameError(Exception):
    """Malformed frame (bad length prefix or non-JSON payload)."""


def send_frame(sock: socket.socket, payload: dict) -> None:
    data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise FrameError(f"frame of {len(data)} bytes exceeds {MAX_FRAME_BYTES} cap")
    sock.sendall(_LEN.pack(len(data)) + data)


def recv_frame(sock: socket.socket) -> dict:
    """Read one frame. Raises EOFError on clean close, FrameError on garbage.

    Honors the socket's timeout: ``socket.timeout`` from a partial read
    propagates to the caller (used by the parent's watchdog poll loop).
    """
    header = _recv_exact(sock, _LEN.size)
    (length,) = _LEN.unpack(header)
    if length > MAX_FRAME_BYTES:
        raise FrameError(f"declared frame of {length} bytes exceeds {MAX_FRAME_BYTES} cap")
    data = _recv_exact(sock, length)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FrameError(f"undecodable frame: {e}") from e
    if not isinstance(payload, dict):
        raise FrameError("frame payload must be a JSON object")
    return payload


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError("socket closed mid-frame" if buf else "socket closed")
        buf.extend(chunk)
    return bytes(buf)
