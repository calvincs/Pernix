"""Generated fixture for the `gen-json-transform` canary (trust-loop W5).

Structured-data category: read, filter, aggregate, emit machine-checkable
output. The static `json-transform` canary always sums to 175 for the same
three customers; this one draws a fresh customer set, a fresh record list
and therefore a fresh pair of expected values on every run.

The trap the original was built around is preserved and guaranteed: at
least one customer appears ONLY on non-shipped records, so a solution that
derives the customer list from the shipped subset gets the sum right and the
list wrong.
"""

from __future__ import annotations

import json
import random

_CUSTOMERS = (
    "acme",
    "globex",
    "initech",
    "umbrella",
    "soylent",
    "hooli",
    "vandelay",
    "stark",
    "wayne",
    "tyrell",
)
_STATUSES = ("shipped", "pending", "cancelled", "refunded")


def generate(seed: int) -> dict:
    rng = random.Random(seed)
    customers = sorted(rng.sample(_CUSTOMERS, rng.randrange(3, 6)))

    # One record per customer first, so the "customers" answer is exactly the
    # drawn set; then extras for volume.
    plan: list[tuple[str, str]] = [(c, rng.choice(_STATUSES)) for c in customers]
    for _ in range(rng.randrange(2, 7)):
        plan.append((rng.choice(customers), rng.choice(_STATUSES)))

    # Floors: at least one shipped record (a zero sum is a weak assertion),
    # and at least one customer that never ships (the aggregation trap).
    if not any(s == "shipped" for _c, s in plan):
        plan[rng.randrange(len(plan))] = (rng.choice(customers), "shipped")
    ghost = rng.choice(customers)
    plan = [(c, s if c != ghost else rng.choice(("pending", "cancelled", "refunded"))) for c, s in plan]
    if not any(s == "shipped" for _c, s in plan):
        other = [c for c in customers if c != ghost] or customers
        plan[rng.randrange(len(plan))] = (rng.choice(other), "shipped")

    rng.shuffle(plan)
    records = [
        {"id": i + 1, "customer": c, "status": s, "amount": rng.randrange(5, 200)} for i, (c, s) in enumerate(plan)
    ]

    # Ground truth read back off the rendered records, exactly as the prompt
    # defines it — never off the plan above.
    shipped_total = sum(r["amount"] for r in records if r["status"] == "shipped")
    all_customers = sorted({r["customer"] for r in records})

    prompt = (
        "The workspace file data.json holds a list of order records. Produce\n"
        "output.json in the workspace root: a JSON object with exactly two keys —\n"
        '"shipped_total": the sum of "amount" over records whose status is\n'
        '"shipped", and "customers": the sorted list of unique "customer" values\n'
        "across ALL records (not just the shipped ones). Use plain JSON (no\n"
        "comments, no trailing text).\n"
    )

    check = (
        "import json; d=json.load(open('output.json')); "
        f"assert d['shipped_total']=={shipped_total}, d; "
        f"assert d['customers']=={all_customers!r}, d; "
        "print('OK')"
    )

    return {
        "prompt": prompt,
        "files": {"data.json": json.dumps(records, indent=2) + "\n"},
        "gates": [
            {
                "name": "output_valid",
                "command": f'python3 -c "{check}"',
                "watch_paths": ["output.json"],
            }
        ],
    }
