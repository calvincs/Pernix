"""Dataclasses and error hierarchy for the RLM engine.

Adapted (much slimmed) from the Recursive Language Models reference
implementation (https://github.com/alexzhang13/rlm, MIT License,
Copyright (c) 2025 Alex Zhang).
"""

from dataclasses import dataclass, field


@dataclass
class RLMCaps:
    """Runaway-prevention caps for one run. Resolved from settings by the tool
    glue; tests construct these directly."""

    max_iterations: int = 20
    max_subcalls: int = 50
    max_concurrent_subcalls: int = 3
    timeout_seconds: float = 900.0
    max_depth: int = 1


@dataclass
class CellResult:
    """Outcome of executing one ```repl``` block in the child."""

    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    # Set the first time model code flips answer["ready"] = True.
    final_answer: str | None = None
    # "name:type" entries for non-scaffold variables, for the model's benefit.
    var_names: list[str] = field(default_factory=list)


@dataclass
class RLMRunResult:
    """What a completed (or capped/cancelled) run hands back to the tool."""

    answer: str
    # completed | iteration_cap | timeout | cancelled | budget_exhausted | failed
    status: str
    iterations: int = 0
    subcalls: int = 0
    duration: float = 0.0
    # True when `answer` is a best-effort partial rather than a submitted answer.
    partial: bool = False
    error: str = ""


class RLMRunError(Exception):
    """Base class. `partial_answer` carries whatever the run salvaged."""

    def __init__(self, message: str, partial_answer: str | None = None):
        super().__init__(message)
        self.partial_answer = partial_answer


class RLMChildDied(RLMRunError):
    """The child REPL process exited or its socket went away mid-run."""


class RLMTimeout(RLMRunError):
    """Run wall clock (`rlm_timeout_seconds`) expired."""


class RLMCancelled(RLMRunError):
    """The owning session requested cancellation."""


class RLMBudgetExhausted(RLMRunError):
    """The session's LLM scheduler budget ran out mid-run."""
