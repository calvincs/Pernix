"""Pernix — Scout dataclasses: ScoutReport and SessionBrief."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.llm.types import TokenUsage


@dataclass
class DeliverableSpec:
    """A single item scout expects the agent to produce.

    Used for the agent's work-completion contract: reflect checks these
    against actual outcomes to grade the turn, without re-reading the
    full transcript.
    """

    description: str = ""
    # Suggested execution hint for this specific item: inline | task | worker.
    # Overall `execution_mode` on the report applies to the plan as a whole;
    # per-item hints are used when the plan mixes modes.
    execution_hint: str = "inline"


@dataclass
class SessionBrief:
    """Compact session state for scout consumption. ~200-400 tokens.

    Built deterministically from structured state — no conversation content.
    """

    session_id: str
    title: str = "New session"
    turn_count: int = 0
    session_type: str = "normal"
    phase: str | None = None
    compaction_summary: str | None = None  # max 500 chars
    recent_messages: list[str] = field(default_factory=list)  # last 3: "role: first 200 chars"
    tools_used_recently: list[str] = field(default_factory=list)
    feature_state: dict | None = None
    context_utilization: float = 0.0
    is_fresh: bool = True

    def to_prompt_text(self) -> str:
        """Format as text for the scout's input."""
        lines = [f"Session: {self.title} (type={self.session_type})"]
        if self.is_fresh:
            lines.append("Status: Fresh session (first turn)")
        else:
            lines.append(f"Status: Turn {self.turn_count}, context {self.context_utilization:.0%} used")

        if self.recent_messages:
            lines.append("Recent messages:")
            for m in self.recent_messages[-3:]:
                lines.append(f"  {m}")

        if self.tools_used_recently:
            lines.append(f"Tools used recently: {', '.join(self.tools_used_recently)}")

        if self.feature_state:
            passed = sum(1 for v in self.feature_state.values() if v == "passed")
            total = len(self.feature_state)
            lines.append(f"Features: {passed}/{total} passed")

        if self.compaction_summary:
            lines.append(f"Previous summary: {self.compaction_summary[:500]}")

        return "\n".join(lines)


@dataclass
class ScoutReport:
    """Structured output from scout agent. Budget: ~1,800 tokens max."""

    # Curated context sections
    identity: str = ""  # From SOUL.md, max ~300 tokens
    rules: str = ""  # From RULES.md, max ~300 tokens
    instructions: str = ""  # From SESSIONS.md, max ~300 tokens
    memory_context: str = ""  # Relevant memory entries, max ~500 tokens
    memory_queries_used: list[str] = field(default_factory=list)
    cross_session_context: str = ""  # Findings from other sessions, max ~500 tokens

    # Tool selection
    recommended_tools: list[str] = field(default_factory=list)
    tool_rationale: str = ""

    # Skill selection
    recommended_skills: list[str] = field(default_factory=list)
    skill_rationale: str = ""
    injected_skill: str = ""  # L2 body of top-1 skill (auto-injected by scout)
    injected_skill_name: str = ""  # Name of the auto-injected skill

    # Model recommendation (for tasks needing special capabilities)
    recommended_model: str = ""
    model_rationale: str = ""

    # Session orientation
    session_state: str = ""  # Max ~200 tokens
    approach_guidance: str = ""  # Max ~500 tokens

    # Deliverables plan (new in Phase 2d — the agent's work contract).
    # Reflect and the eval harness read this to check completion without
    # re-parsing the transcript. Empty list is legitimate for pure Q&A.
    deliverables_plan: list = field(default_factory=list)  # list[DeliverableSpec]
    # Overall execution mode suggestion: inline | tasks.
    # Defaults to "inline" — over-decomposing trivial requests is expensive,
    # and snooze will learn from outcomes when "tasks" actually helps.
    # Note: "workers" was removed (unfulfilled — agent never auto-spawned workers
    # from this directive). Deferred until properly implemented.
    execution_mode: str = "inline"

    # Viability flag set by scout self-validation (Phase 2a).
    # "verified": passed the validator on first or second submit.
    # "unverified": submitted with outstanding issues — agent proceeds with caution.
    # "pending": default before validation runs.
    viability: str = "pending"
    viability_notes: list = field(default_factory=list)  # validator issue strings (if any)

    # Metadata
    scout_model: str = ""
    scout_latency_ms: int = 0
    scout_tokens: TokenUsage = field(default_factory=TokenUsage)
    from_cache: bool = False
    from_fallback: bool = False
    # Why the deterministic fallback was used: "bypass" (cheap turn, scout
    # skipped on purpose) or "degraded" (scout ran and produced nothing usable).
    # Only the latter is worth warning the agent about.
    fallback_reason: str = ""

    def to_system_prompt_section(self) -> str:
        """Format as text for injection into the main agent's system prompt."""
        parts = []

        if self.identity:
            parts.append(f"[IDENTITY]\n{self.identity}")
        if self.rules:
            parts.append(f"[RULES]\n{self.rules}")
        if self.instructions:
            # Framing matters: SESSIONS.md is deployment config, and an unset
            # field there is NOT evidence that a fact is unknown. Without this
            # note the model reads placeholder lines ("Timezone: not set") as
            # ground truth and refuses tasks whose answer is sitting in
            # [RELEVANT MEMORY] directly below. Applied here rather than in the
            # file so it holds for every deployment's SESSIONS.md.
            parts.append(
                f"[INSTRUCTIONS]\n"
                f"(Deployment configuration. A blank or unset field below means "
                f"'not pinned in config' — never that the fact is unknown. "
                f"Defer to [RELEVANT MEMORY] for anything not pinned here.)\n"
                f"{self.instructions}"
            )
        if self.memory_context:
            parts.append(f"[RELEVANT MEMORY]\n{self.memory_context}")
        if self.cross_session_context:
            parts.append(f"[CROSS-SESSION CONTEXT]\n{self.cross_session_context}")
        if self.session_state:
            parts.append(f"[SESSION STATE]\n{self.session_state}")
        if self.recommended_model:
            parts.append(
                f"[RECOMMENDED MODEL]\n"
                f"Use model: {self.recommended_model}\n"
                f"Reason: {self.model_rationale}\n"
                f"Spawn a worker with this model for the task requiring it."
            )
        # Skill injection (hybrid: top-1 auto-injected, others by name)
        if self.injected_skill:
            parts.append(
                f"[ACTIVE SKILL: {self.injected_skill_name}]\n"
                f"The following skill has been loaded for this task. Follow its instructions:\n\n"
                f"{self.injected_skill}"
            )
        remaining_skills = [s for s in self.recommended_skills if s != self.injected_skill_name]
        if remaining_skills:
            skill_list = ", ".join(remaining_skills)
            parts.append(
                f"[AVAILABLE SKILLS]\n"
                f"Additional relevant skills: {skill_list}\n"
                f"Use load_skill(name) to get full instructions if needed."
            )

        if self.approach_guidance:
            parts.append(f"[APPROACH]\n{self.approach_guidance}")

        if self.deliverables_plan:
            mode = self.execution_mode or "inline"
            deliv_lines = [f"[DELIVERABLES — execution_mode: {mode}]"]
            for i, d in enumerate(self.deliverables_plan, 1):
                hint = f" ({d.execution_hint})" if d.execution_hint and d.execution_hint != mode else ""
                deliv_lines.append(f"  {i}. {d.description}{hint}")
            deliv_lines.append(
                "Reflect will check each item at turn end. Produce concrete evidence "
                "(file path, task completion, worker summary) so reflect can verify."
            )
            parts.append("\n".join(deliv_lines))

        if self.viability == "unverified" and self.viability_notes:
            parts.append(
                "[SCOUT NOTICE]\n"
                "Scout's plan was submitted without full self-validation. "
                "Outstanding concerns:\n- "
                + "\n- ".join(self.viability_notes)
                + "\nProceed with extra care and call discover_tools if recommendations look off."
            )

        if self.from_fallback and self.fallback_reason != "bypass":
            # Render whenever scout degraded, not only when identity/rules are
            # also missing. The deterministic fallback fills those in from
            # SOUL.md/RULES.md, so gating on them hid the notice exactly when
            # the agent most needed it: the tool list is core-only, and without
            # this the agent assumes the missing capabilities don't exist and
            # starts building its own.
            parts.append(
                "[SCOUT STATUS]\n"
                "Scout planning was unavailable for this turn — the tool list "
                "below is a conservative default, not a curated one. Before "
                "concluding a capability is missing, call discover_tools "
                "(and recall) to check what this deployment already provides."
            )

        return "\n\n".join(parts)

    def get_tool_names(self) -> list[str]:
        """Get validated tool names (caller should filter against registry)."""
        return list(self.recommended_tools)
