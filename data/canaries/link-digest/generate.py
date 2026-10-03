"""Generated fixture for the `link-digest` canary.

The most common real request — "read this link and summarise it" — with no
internet involved. The article is generated per seed and published by the
runner on Pernix's own /workspace route (`serve:` in CANARY.md), so the
agent has to fetch it over HTTP with `http_get`; it is not in the task
workspace. Title, author and the key figure all move with the seed, and the
page carries a decoy figure (an earlier, revised estimate) so "first number
on the page" fails where "read the article" passes.

The expected values never leave this module except inside the gate
commands. {{SERVE_BASE}} is substituted by the runner in the prompt and the
gates alike.
"""

from __future__ import annotations

import random
import shlex

_ADJECTIVES = ("Quiet", "Hidden", "Northern", "Restless", "Patient", "Shifting", "Lantern-lit", "Unlikely")
_SUBJECTS = ("Grid", "Harbour", "Orchard", "Archive", "Reservoir", "Observatory", "Canal", "Foundry")
_PLACES = ("Varneholm", "Ostrava Ridge", "Callow Bay", "Mirefield", "Tessaly", "Brannock", "Quillon")
_FIRST = ("Ines", "Tomasz", "Adaeze", "Yuki", "Rafael", "Marit", "Oluwaseun", "Leopold")
_LAST = ("Harrow", "Okonkwo", "Lindqvist", "Ferreira", "Nakashima", "Dvorak", "Achterberg")
_UNITS = ("megawatts", "tonnes", "kilolitres", "hectares")


def generate(seed: int) -> dict:
    rng = random.Random(seed)
    title = f"The {rng.choice(_ADJECTIVES)} {rng.choice(_SUBJECTS)} of {rng.choice(_PLACES)}"
    author = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
    unit = rng.choice(_UNITS)
    key = f"{rng.randrange(100, 1000)}.{rng.randrange(1, 10)}"
    decoy = key
    while decoy == key:
        decoy = f"{rng.randrange(100, 1000)}.{rng.randrange(1, 10)}"

    html = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title></head>
<body>
<nav><a href="/">Home</a> | <a href="/archive">Archive</a> | <a href="/about">About</a></nav>
<article>
<h1>{title}</h1>
<p class="byline">By {author}</p>
<p>Survey crews spent the season measuring what the region actually holds.
An earlier estimate of {decoy} {unit} was revised after the second survey.</p>
<p><strong>Key figure:</strong> {key} {unit}, the revised total reported this year.</p>
<p>Officials expect the number to be reviewed again next spring.</p>
</article>
<footer>Subscribe for more field reports.</footer>
</body>
</html>
"""

    prompt = (
        "Fetch {{SERVE_BASE}}/article.html with the http_get tool and read the\n"
        "article. Then write summary.md in the workspace root containing: the\n"
        "article's title, its author, the key figure it reports (the number\n"
        "with its unit), a one-paragraph summary, and the source URL you fetched.\n"
    )

    def grep(name: str, needle: str) -> dict:
        return {"name": name, "command": f"grep -qiF {shlex.quote(needle)} summary.md", "watch_paths": ["summary.md"]}

    return {
        "prompt": prompt,
        "files": {"article.html": html},
        "gates": [
            grep("title", title),
            grep("author", author),
            grep("key_figure", key),
            grep("source_url", "{{SERVE_BASE}}/article.html"),
        ],
    }
