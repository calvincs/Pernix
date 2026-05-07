"""Pernix — Skill registry with discovery index and NLP search."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from core.skills.parser import SkillParseError, parse_skill_md

logger = logging.getLogger("pernix.skills.registry")


# ---------------------------------------------------------------------------
# Synonym map for natural language skill discovery
# ---------------------------------------------------------------------------

SKILL_SYNONYMS: dict[str, list[str]] = {
    "search": ["web", "find", "lookup", "query", "research", "internet"],
    "write": ["create", "save", "output", "generate", "make", "author"],
    "read": ["open", "load", "view", "inspect", "check", "show", "get"],
    "run": ["execute", "shell", "command", "terminal", "bash", "process"],
    "git": ["version", "commit", "diff", "vcs", "source", "repo", "branch"],
    "workflow": ["process", "procedure", "pipeline", "steps", "guide"],
    "debug": ["troubleshoot", "diagnose", "fix", "investigate", "error"],
    "deploy": ["release", "publish", "ship", "launch", "build"],
    "test": ["verify", "validate", "check", "evaluate", "qa", "assess"],
    "analyze": ["review", "audit", "examine", "inspect", "study"],
    "data": ["database", "sql", "csv", "json", "parse", "transform"],
    "api": ["endpoint", "rest", "http", "request", "response", "service"],
    "skill": ["capability", "expertise", "domain", "knowledge", "guide"],
    "code": ["programming", "develop", "software", "implement", "engineer"],
}

# Skill co-occurrence: discovering one surfaces related skills
SKILL_COOCCURRENCE: dict[str, list[str]] = {}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class SkillDef:
    """Definition of a registered skill (L1 metadata + path)."""

    name: str
    description: str
    path: Path  # Absolute path to skill directory
    tags: list[str] = field(default_factory=list)
    version: str = "1.0"


@dataclass
class SkillSummary:
    """Lightweight skill info returned by discover_skills."""

    name: str
    description: str
    tags: list[str]
    has_scripts: bool = False
    has_references: bool = False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "has_scripts": self.has_scripts,
            "has_references": self.has_references,
        }


# ---------------------------------------------------------------------------
# Tokenization and search (duplicated from ToolIndex pattern)
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Split text into searchable tokens."""
    return {w.lower() for w in re.findall(r"[a-zA-Z]+", text) if len(w) > 2}


def _expand_synonyms(tokens: set[str]) -> set[str]:
    """Expand tokens with synonym matches."""
    expanded = set(tokens)
    for token in tokens:
        for key, syns in SKILL_SYNONYMS.items():
            if token == key or token in syns:
                expanded.add(key)
                expanded.update(syns)
    return expanded


# ---------------------------------------------------------------------------
# Skill Index
# ---------------------------------------------------------------------------


@dataclass
class _SkillIndexEntry:
    name: str
    description: str
    tags: list[str]
    has_scripts: bool
    has_references: bool
    name_tokens: set[str] = field(default_factory=set)
    desc_tokens: set[str] = field(default_factory=set)
    tag_tokens: set[str] = field(default_factory=set)

    def to_summary(self) -> SkillSummary:
        return SkillSummary(
            name=self.name,
            description=self.description,
            tags=self.tags,
            has_scripts=self.has_scripts,
            has_references=self.has_references,
        )


class SkillIndex:
    """Search index for skill discovery."""

    def __init__(self):
        self._entries: dict[str, _SkillIndexEntry] = {}

    def rebuild(self, skills: dict[str, SkillDef]) -> None:
        """Rebuild index from registry. A skill that fails to index is skipped."""
        self._entries.clear()
        for name, skill in skills.items():
            try:
                entry = _SkillIndexEntry(
                    name=name,
                    description=skill.description,
                    tags=list(skill.tags),
                    has_scripts=(skill.path / "scripts").is_dir(),
                    has_references=(skill.path / "references").is_dir(),
                    name_tokens=_tokenize(name.replace("-", " ")),
                    desc_tokens=_tokenize(skill.description),
                    tag_tokens={str(t).lower() for t in skill.tags},
                )
            except Exception as e:
                logger.warning("Failed to index skill '%s' (skipped): %s", name, e)
                continue
            self._entries[name] = entry

    def search(self, query: str, limit: int = 10, exclude: set[str] | None = None) -> list[SkillSummary]:
        """Search skills by natural language query.

        exclude: optional set of skill names to skip. Used by SkillRegistry to
        filter out disabled skills (and their co-occurring siblings) so they
        never surface to scout, discover_skills, or any other consumer.
        """
        excl = exclude or set()
        query_tokens = _tokenize(query)
        expanded = _expand_synonyms(query_tokens)

        scored: list[tuple[_SkillIndexEntry, float]] = []
        for entry in self._entries.values():
            if entry.name in excl:
                continue
            score = 0.0
            # Name match (strongest signal)
            score += len(expanded & entry.name_tokens) * 3.0
            # Tag match (strong signal)
            score += len(expanded & entry.tag_tokens) * 2.0
            # Description word overlap
            score += len(expanded & entry.desc_tokens) * 1.0

            if score > 0:
                scored.append((entry, score))

        scored.sort(key=lambda x: -x[1])

        # Apply co-occurrence (also filtered against exclude set so a disabled
        # sibling can't be promoted by an enabled neighbor's match)
        results: list[SkillSummary] = []
        seen: set[str] = set()
        for entry, _score in scored[:limit]:
            if entry.name not in seen:
                results.append(entry.to_summary())
                seen.add(entry.name)
                for co_name in SKILL_COOCCURRENCE.get(entry.name, []):
                    if co_name not in seen and co_name in self._entries and co_name not in excl:
                        results.append(self._entries[co_name].to_summary())
                        seen.add(co_name)

        return results[:limit]


# ---------------------------------------------------------------------------
# Skill Registry
# ---------------------------------------------------------------------------


class SkillRegistry:
    """Central registry for all skills. Thread-safe for concurrent access.

    Owns the disabled-skill set so every consumer (scout, builtin tools,
    workflows, agent loop) gets consistent filtering through one set of
    methods. Persisted to ``data/skills/.disabled.json`` as a sorted JSON
    array — same on-disk format the API router historically used.

    Disabling takes effect on the next scout run; mid-turn agent loops keep
    a previously auto-injected skill in their system prompt for the rest of
    that turn.
    """

    def __init__(self):
        self._skills: dict[str, SkillDef] = {}
        self._invalid: set[str] = set()  # Skills that failed pre-flight validation
        self._disabled: set[str] = set()  # User-toggled-off skills
        self._disabled_path: Path | None = None  # Set when scan() learns the dir
        self.index = SkillIndex()
        self._lock = threading.RLock()

    def scan(self, skills_dir: Path | None = None) -> int:
        """Scan filesystem for skills, parsing L1 frontmatter only.

        Returns the number of skills found.
        """
        if skills_dir is None:
            skills_dir = Path("data/skills")

        if not skills_dir.is_dir():
            logger.debug("Skills directory does not exist: %s", skills_dir)
            return 0

        count = 0
        with self._lock:
            for skill_dir in sorted(skills_dir.iterdir()):
                if not skill_dir.is_dir() or skill_dir.name.startswith((".", "_")):
                    continue

                # Reject symlinks to prevent directory traversal
                if skill_dir.is_symlink():
                    logger.warning("Skipping symlink skill directory: %s", skill_dir.name)
                    continue

                skill_md = skill_dir / "SKILL.md"
                if not skill_md.exists():
                    logger.debug("Skipping %s: no SKILL.md", skill_dir.name)
                    continue

                try:
                    frontmatter, _body = parse_skill_md(skill_md)
                    skill = SkillDef(
                        name=frontmatter["name"],
                        description=frontmatter["description"],
                        path=skill_dir.resolve(),
                        tags=frontmatter.get("tags", []),
                        version=frontmatter.get("version", "1.0"),
                    )
                    self._skills[skill.name] = skill
                    # Pre-flight validation: warn and track invalid skills
                    issues = self._validate(skill)
                    if issues:
                        self._invalid.add(skill.name)
                        for issue in issues:
                            logger.warning("Skill '%s' validation: %s", skill.name, issue)
                    else:
                        self._invalid.discard(skill.name)
                    count += 1
                    logger.debug("Scanned skill: %s%s", skill.name, " [INVALID]" if issues else "")
                except (SkillParseError, OSError) as e:
                    logger.warning("Failed to parse skill in %s: %s", skill_dir, e)

            self.index.rebuild(self._skills)
            # Remember dir for save_disabled, then reload disabled state so
            # toggle persists across rescans (which happen on PUT/PATCH/DELETE).
            self._disabled_path = skills_dir / ".disabled.json"
            self._load_disabled()
            # Prune stale entries: a name in .disabled.json that no longer
            # corresponds to a skill on disk would otherwise lurk forever and
            # silently re-disable a future skill of the same name (e.g. one
            # created via create_skill or restored from backup). Persist the
            # pruned set so disk and memory stay consistent.
            stale = self._disabled - self._skills.keys()
            if stale:
                logger.info("Pruning %d stale disabled entries: %s", len(stale), sorted(stale))
                self._disabled -= stale
                self._save_disabled()
        logger.info(
            "Skill registry: %d skills scanned (%d invalid, %d disabled)",
            count,
            len(self._invalid),
            len(self._disabled),
        )
        return count

    def rescan(self, skills_dir: Path | None = None) -> int:
        """Re-scan skills directory (after new skills added). Thread-safe."""
        with self._lock:
            self._skills.clear()
            self._invalid.clear()
            # Note: don't clear _disabled — scan() reloads it from disk.
        return self.scan(skills_dir)

    # --- Enable/Disable -----------------------------------------------------

    def _load_disabled(self) -> None:
        """Read the on-disk disabled set into ``self._disabled``.

        Silently treats a missing or malformed file as "nothing disabled" so
        a corrupted ``.disabled.json`` never blocks the registry from
        starting.
        """
        if self._disabled_path is None or not self._disabled_path.exists():
            self._disabled = set()
            return
        try:
            data = json.loads(self._disabled_path.read_text(encoding="utf-8"))
            self._disabled = {str(n) for n in data}
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to read %s: %s — treating as empty", self._disabled_path, e)
            self._disabled = set()

    def _save_disabled(self) -> None:
        """Persist ``self._disabled`` to ``.disabled.json`` as a sorted array.

        Caller is responsible for ensuring ``self._disabled_path`` is set
        (which happens at the end of ``scan()``). Callers that mutate
        ``_disabled`` outside that contract will see ``RuntimeError`` from
        ``disable()`` / ``enable()``.
        """
        try:
            self._disabled_path.write_text(json.dumps(sorted(self._disabled)), encoding="utf-8")
        except OSError as e:
            logger.error("Failed to write %s: %s", self._disabled_path, e)

    def disable(self, name: str) -> None:
        """Mark a skill disabled and persist. Idempotent.

        Raises ``RuntimeError`` if called before ``scan()`` — without a known
        ``skills_dir`` the new entry would be lost on the next scan and the
        caller would silently lose state. Better to fail loudly.
        """
        if self._disabled_path is None:
            raise RuntimeError(
                "SkillRegistry.disable() called before scan() — "
                "no skills_dir is known, the toggle would not persist. "
                "Call scan(skills_dir) first."
            )
        with self._lock:
            self._disabled.add(name)
            self._save_disabled()

    def enable(self, name: str) -> None:
        """Mark a skill enabled and persist. Idempotent.

        Raises ``RuntimeError`` if called before ``scan()`` — see ``disable()``.
        """
        if self._disabled_path is None:
            raise RuntimeError(
                "SkillRegistry.enable() called before scan() — "
                "no skills_dir is known, the toggle would not persist. "
                "Call scan(skills_dir) first."
            )
        with self._lock:
            self._disabled.discard(name)
            self._save_disabled()

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def enabled_skills(self) -> list[SkillDef]:
        """Return only currently-enabled skills."""
        return [s for s in self._skills.values() if s.name not in self._disabled]

    def _validate(self, skill: SkillDef) -> list[str]:
        """Run pre-flight validation on a skill. Returns a list of issue strings (empty = valid)."""
        import py_compile
        import tempfile

        issues: list[str] = []

        scripts_dir = skill.path / "scripts"
        if not scripts_dir.exists():
            return issues  # No scripts/ dir is fine — skill may be docs-only

        script_files = [f for f in scripts_dir.iterdir() if f.is_file() and not f.name.startswith(".")]
        if not script_files:
            issues.append("scripts/ directory exists but contains no files")
            return issues

        for script in script_files:
            if script.stat().st_size == 0:
                issues.append(f"script '{script.name}' is empty")
                continue
            if script.suffix == ".py":
                tf_path = None
                try:
                    # Write to temp file so py_compile doesn't write .pyc into skill dir
                    with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tf:
                        tf.write(script.read_bytes())
                        tf_path = tf.name
                    py_compile.compile(tf_path, doraise=True)
                except py_compile.PyCompileError as e:
                    # Strip the temp path from the error message for clarity
                    msg = str(e).replace(tf_path or "", script.name)
                    issues.append(f"script '{script.name}' has syntax error: {msg}")
                except Exception as e:
                    issues.append(f"script '{script.name}' could not be checked: {e}")
                finally:
                    if tf_path:
                        try:
                            import os as _os

                            _os.unlink(tf_path)
                        except Exception:
                            pass

        return issues

    def is_valid(self, name: str) -> bool:
        """Return True if skill passed pre-flight validation (or has no scripts to validate)."""
        return name not in self._invalid

    def get(self, name: str) -> SkillDef | None:
        return self._skills.get(name)

    def exists(self, name: str) -> bool:
        return name in self._skills

    def all_skills(self) -> list[SkillDef]:
        return list(self._skills.values())

    def discover(self, query: str, limit: int = 10, include_disabled: bool = False) -> list[SkillSummary]:
        """Search for skills by natural language query.

        Disabled skills are excluded by default. Pass ``include_disabled=True``
        for the Explorer UI to surface toggled-off skills with their flag.
        """
        exclude = set() if include_disabled else self._disabled
        return self.index.search(query, limit=limit, exclude=exclude)

    def update_cooccurrence(self, mapping: dict[str, list[str]]) -> None:
        """Update the skill co-occurrence map at runtime.

        Called by snooze to populate SKILL_COOCCURRENCE from memory analysis.
        """
        global SKILL_COOCCURRENCE
        with self._lock:
            SKILL_COOCCURRENCE.update(mapping)

    def load_instructions(self, name: str, include_disabled: bool = False) -> str | None:
        """Read the SKILL.md body (L2 instructions) for a skill.

        Returns None if skill not found or disabled (unless ``include_disabled``
        is True — used by the Explorer UI's edit view).
        """
        skill = self._skills.get(name)
        if not skill:
            return None
        if not include_disabled and name in self._disabled:
            return None

        skill_md = skill.path / "SKILL.md"
        if not skill_md.exists():
            return None

        try:
            _frontmatter, body = parse_skill_md(skill_md)
            return body
        except (SkillParseError, OSError) as e:
            logger.warning("Failed to load instructions for skill '%s': %s", name, e)
            return None

    def list_resources(self, name: str, include_disabled: bool = False) -> dict:
        """List L3 resources (scripts, references, assets) for a skill.

        Returns dict with keys: scripts, references, assets — each a list of filenames.
        Returns empty dict if skill not found or disabled (unless
        ``include_disabled`` is True — used by the Explorer UI).
        """
        skill = self._skills.get(name)
        if not skill:
            return {}
        if not include_disabled and name in self._disabled:
            return {}

        resources: dict[str, list[str]] = {}
        for subdir in ("scripts", "references", "assets"):
            sub_path = skill.path / subdir
            if sub_path.is_dir():
                files = sorted(f.name for f in sub_path.iterdir() if f.is_file() and not f.name.startswith("."))
                if files:
                    resources[subdir] = files

        return resources

    def read_resource(self, name: str, resource_path: str, include_disabled: bool = False) -> str | None:
        """Read an L3 resource file from a skill.

        resource_path is relative to the skill directory (e.g. 'scripts/check.sh').
        Returns None if skill not found, disabled, or path is invalid.
        Rejects path traversal attempts.
        """
        skill = self._skills.get(name)
        if not skill:
            return None
        if not include_disabled and name in self._disabled:
            return None

        # Prevent path traversal
        if ".." in resource_path or resource_path.startswith("/"):
            logger.warning("Path traversal attempt blocked: %s", resource_path)
            return None

        file_path = (skill.path / resource_path).resolve()

        # Verify the resolved path is within the skill directory
        try:
            file_path.relative_to(skill.path)
        except ValueError:
            logger.warning("Path traversal attempt blocked: %s -> %s", resource_path, file_path)
            return None

        if not file_path.is_file():
            return None

        try:
            return file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("Failed to read resource %s/%s: %s", name, resource_path, e)
            return None


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_skill_registry: SkillRegistry | None = None
_skill_registry_lock = threading.Lock()


def get_skill_registry() -> SkillRegistry:
    global _skill_registry
    if _skill_registry is None:
        with _skill_registry_lock:
            if _skill_registry is None:
                _skill_registry = SkillRegistry()
    return _skill_registry
