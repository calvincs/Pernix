"""Generated fixture for the `gen-file-create` canary (trust-loop W5).

Instruction-following at its simplest: one named file, one exact line. The
name and the line both move with the seed, and the workspace is seeded with
a near-miss decoy so "copy whatever is lying around" fails where "read the
instruction" passes.

The expected value never leaves this module except inside the gate command.
"""

from __future__ import annotations

import random
import shlex

_ADJECTIVES = ("amber", "brisk", "candid", "dormant", "eager", "fluent", "gilded", "humble", "ivory", "jovial")
_NOUNS = ("beacon", "cinder", "ledger", "meadow", "nectar", "outpost", "pylon", "quarry", "runlet", "shard")
_COLORS = ("crimson", "cobalt", "olive", "saffron", "slate", "teal", "violet")
_ANIMALS = ("badger", "falcon", "gecko", "heron", "lynx", "otter", "wombat")
_EXTENSIONS = ("txt", "md", "log")


def generate(seed: int) -> dict:
    rng = random.Random(seed)
    stem = f"{rng.choice(_ADJECTIVES)}-{rng.choice(_NOUNS)}"
    filename = f"{stem}-{rng.randrange(100, 1000)}.{rng.choice(_EXTENSIONS)}"
    token = f"{rng.choice(_COLORS)} {rng.choice(_ANIMALS)} {rng.randrange(1000, 10000)}"
    line = f"Pernix canary check: {token}."

    # A decoy that is close enough to be tempting and wrong enough to fail.
    decoy_token = f"{rng.choice(_COLORS)} {rng.choice(_ANIMALS)} {rng.randrange(1000, 10000)}"
    decoy = f"Pernix canary check: {decoy_token}.\n"

    prompt = (
        f"Create a file named {filename} in the workspace root containing exactly this\n"
        "single line (no extra whitespace, no trailing commentary in the file):\n"
        "\n"
        f"{line}\n"
        "\n"
        "The workspace already contains reference/sample.txt. It is a sample of the\n"
        "FORMAT only — its wording is not the line you were asked to write. Write the\n"
        "line above, exactly as given.\n"
    )

    return {
        "prompt": prompt,
        "files": {"reference/sample.txt": decoy},
        "gates": [
            {
                "name": "file_exact",
                "command": f"grep -qx {shlex.quote(line)} {shlex.quote(filename)}",
                "watch_paths": [filename],
            }
        ],
    }
