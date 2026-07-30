"""RLM (Recursive Language Models) engine for Pernix.

Inference-time scaffold for processing inputs far beyond the model's context
window (paper: arXiv 2512.24601): the input lives as a `context` variable in a
sandboxed child REPL; the root model iteratively writes ```repl``` code to
slice/analyze it and delegates to sub-LLM calls brokered by the parent.

Core adapted from https://github.com/alexzhang13/rlm (MIT License,
Copyright (c) 2025 Alex Zhang) — extracted and rewritten for Pernix, not a
dependency. See docs/internals/rlm.md.

The agent-facing `rlm_process` tool and its `register()` hook arrive with the
tool-wiring phase; this package is inert until `settings.rlm_enabled` is on.
"""
