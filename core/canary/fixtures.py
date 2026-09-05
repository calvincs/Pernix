"""Pernix — generated canary fixtures (trust-loop hardening W5, plan §5).

A saturated suite proves nothing. Every hand-written canary ships its answer
in the repository — the expected ERROR count sits in the gate command, the
seed files never change, and a model that has seen the transcript once can
pass it forever without doing the work. That is exactly the failure mode a
regression floor must not have.

So a canary directory may carry a ``generate.py`` beside its CANARY.md:

    def generate(seed: int) -> dict:
        return {
            "prompt": "...",                                   # the task
            "files": {"logs/app.log": "..."},                  # workspace seed
            "gates": [{"name": "...", "command": "...",        # the scoring
                       "watch_paths": ["answer.txt"]}],
        }

The runner picks a fresh random seed per run and takes prompt, files and
gates from that one call. The expected value is computed inside the
generator and written *into the gate command literally* — it exists nowhere
the agent can reach: not in the workspace (input files only), not in the
prompt, and not in a tool result (``list_gates`` is off the canary
allowlist). Only the seed is persisted, in ``gate_results_json``, which is
enough to reproduce a failed run by hand and useless to a model that has
memorised last week's answer.

TRUST: ``generate.py`` is imported and executed in-process, the same trust
level as the gate shell commands it produces (``core/gates.py`` jails the
cwd, not the command). Generated canaries are therefore a hand-authored,
repository-reviewed surface — auto-admission never writes one.
"""

from __future__ import annotations

import importlib.util
import logging
import random
import sys
from dataclasses import replace
from pathlib import Path

from core.canary.parser import CanaryDef

logger = logging.getLogger("pernix.canary")

# Seeds are logged and pasted into bug reports, so keep them short enough to
# read aloud and wide enough that repeats are not a pattern.
SEED_MAX = 2_000_000_000


class FixtureError(Exception):
    """Raised when a generate.py is missing, unimportable, or returns junk."""


def pick_seed() -> int:
    """A fresh seed per run. Deliberately not derived from the canary name or
    the clock: two runs an hour apart must not share a fixture."""
    return random.randrange(1, SEED_MAX)


def load_generate(path: Path):
    """Import one generate.py and return its ``generate`` callable.

    Loaded off sys.modules under a per-directory name: nothing is cached, so
    an edited generator takes effect on the next run and two canaries can
    never collide in the module table.
    """
    if not path.is_file():
        raise FixtureError(f"{path}: no generator file")
    mod_name = "pernix_canary_gen_" + path.parent.name.replace("-", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise FixtureError(f"{path}: not importable")
    module = importlib.util.module_from_spec(spec)
    # No .pyc beside the generator: data/canaries/ is a content directory the
    # operator edits and the API writes into, not a package tree, and a
    # __pycache__ appearing there on the first sweep reads as suite residue.
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # noqa: BLE001 — any import-time failure is the same failure
        raise FixtureError(f"{path}: import failed: {e}") from e
    finally:
        sys.dont_write_bytecode = dont_write
    fn = getattr(module, "generate", None)
    if not callable(fn):
        raise FixtureError(f"{path}: defines no callable generate(seed)")
    return fn


def validate_spec(spec, where: str) -> dict:
    """Check one generate() return value against the same invariants
    parse_canary_md enforces on a hand-written file."""
    if not isinstance(spec, dict):
        raise FixtureError(f"{where}: generate() must return a dict, got {type(spec).__name__}")

    prompt = str(spec.get("prompt") or "").strip()
    if not prompt:
        raise FixtureError(f"{where}: generate() returned no prompt")

    raw_gates = spec.get("gates")
    if not isinstance(raw_gates, list) or not raw_gates:
        raise FixtureError(f"{where}: generate() must return a non-empty gates list")
    gates: list[dict] = []
    for i, g in enumerate(raw_gates):
        if not isinstance(g, dict) or not g.get("name") or not g.get("command"):
            raise FixtureError(f"{where}: gates[{i}] needs 'name' and 'command'")
        wp = g.get("watch_paths") or []
        if isinstance(wp, str):
            wp = [wp]
        gates.append({"name": str(g["name"]), "command": str(g["command"]), "watch_paths": [str(p) for p in wp]})

    raw_files = spec.get("files") or {}
    if not isinstance(raw_files, dict):
        raise FixtureError(f"{where}: generate() 'files' must be a mapping of relative_path -> content")
    files: dict[str, str] = {}
    for rel, content in raw_files.items():
        rel = str(rel)
        if Path(rel).is_absolute() or ".." in Path(rel).parts:
            raise FixtureError(f"{where}: files key '{rel}' must be a workspace-relative path")
        files[rel] = str(content)

    return {"prompt": prompt, "gates": gates, "files": files}


def generate_variant(canary: CanaryDef, seed: int) -> CanaryDef:
    """The seeded instance of a generated canary.

    Everything the generator owns (prompt, files, gates) is replaced;
    everything the file owns (name, timeout, tags, flaky, parked, covers,
    probe fields) survives untouched.
    """
    path = canary.generator_path
    if path is None:
        raise FixtureError(f"canary '{canary.name}' is marked generated but has no generate.py on disk")
    spec = validate_spec(load_generate(path)(int(seed)), f"{canary.name}/generate.py")
    return replace(canary, prompt=spec["prompt"], gates=spec["gates"], files=spec["files"])


def generation_record(seed: int) -> dict:
    """The row appended to gate_results_json so a run is reproducible.

    Shaped like a gate payload on purpose: the Self-checks tab and the
    canary_status tool both iterate this list, and a foreign shape would
    either render as a blank line or raise on the missing 'name'.
    """
    return {
        "kind": "generation",
        "name": "generated fixture",
        "command": f"generate(seed={seed})",
        "passed": True,
        "output_tail": "",
        "seed": int(seed),
        "generated": True,
    }
