"""Pernix — Workflow registry: scan and serve workflow definitions."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from core.workflows.parser import StepDef, WorkflowDef, WorkflowParseError, parse_workflow_md

logger = logging.getLogger("pernix.workflows.registry")


@dataclass
class WorkflowSummary:
    """Lightweight workflow info for listings."""

    name: str
    description: str
    tags: list[str]
    version: str
    step_count: int
    step_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "version": self.version,
            "step_count": self.step_count,
            "step_ids": self.step_ids,
        }


class WorkflowRegistry:
    """Central registry for all workflows. Thread-safe for concurrent access."""

    def __init__(self):
        self._workflows: dict[str, WorkflowDef] = {}
        self._lock = threading.RLock()

    def scan(self, wf_dir: Path | None = None) -> int:
        """Scan filesystem for workflows, parsing WORKFLOW.md files.

        Returns the number of workflows found.
        """
        if wf_dir is None:
            wf_dir = Path("data/workflows")

        if not wf_dir.is_dir():
            logger.debug("Workflows directory does not exist: %s", wf_dir)
            return 0

        count = 0
        new_workflows: dict[str, WorkflowDef] = {}

        for wf_dir_entry in sorted(wf_dir.iterdir()):
            if not wf_dir_entry.is_dir() or wf_dir_entry.name.startswith((".", "_")):
                continue

            if wf_dir_entry.is_symlink():
                logger.warning("Skipping symlink workflow directory: %s", wf_dir_entry.name)
                continue

            wf_md = wf_dir_entry / "WORKFLOW.md"
            if not wf_md.exists():
                logger.debug("Skipping %s: no WORKFLOW.md", wf_dir_entry.name)
                continue

            try:
                frontmatter, body = parse_workflow_md(wf_md)
                steps: list[StepDef] = frontmatter["_parsed_steps"]
                wf = WorkflowDef(
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                    path=wf_dir_entry.resolve(),
                    tags=frontmatter.get("tags", []),
                    version=frontmatter.get("version", "1.0"),
                    steps=steps,
                    body=body,
                )
                new_workflows[wf.name] = wf
                count += 1
                logger.debug("Scanned workflow: %s (%d steps)", wf.name, len(steps))
            except (WorkflowParseError, OSError) as e:
                logger.warning("Failed to parse workflow in %s: %s", wf_dir_entry, e)

        with self._lock:
            self._workflows = new_workflows

        logger.info("Workflow registry: %d workflows scanned", count)
        return count

    def rescan(self, wf_dir: Path | None = None) -> int:
        """Re-scan workflows directory. Thread-safe."""
        with self._lock:
            self._workflows.clear()
        return self.scan(wf_dir)

    def get(self, name: str) -> WorkflowDef | None:
        with self._lock:
            return self._workflows.get(name)

    def exists(self, name: str) -> bool:
        with self._lock:
            return name in self._workflows

    def all_workflows(self) -> list[WorkflowDef]:
        with self._lock:
            return list(self._workflows.values())

    def all_summaries(self) -> list[WorkflowSummary]:
        with self._lock:
            return [
                WorkflowSummary(
                    name=wf.name,
                    description=wf.description,
                    tags=wf.tags,
                    version=wf.version,
                    step_count=len(wf.steps),
                    step_ids=[s.id for s in wf.steps],
                )
                for wf in self._workflows.values()
            ]


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_workflow_registry: WorkflowRegistry | None = None
_workflow_registry_lock = threading.Lock()


def get_workflow_registry() -> WorkflowRegistry:
    global _workflow_registry
    if _workflow_registry is None:
        with _workflow_registry_lock:
            if _workflow_registry is None:
                _workflow_registry = WorkflowRegistry()
    return _workflow_registry
