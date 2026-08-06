---
name: file-edit-bug
prompt: |
  The file calc.py in the workspace has a bug: its add() function returns the
  wrong result. Run check.py to see the failure, fix the bug in calc.py
  (do not modify check.py), and re-run check.py to confirm it prints OK.
gates:
  - name: check_passes
    command: python3 check.py
    watch_paths: [calc.py]
  - name: check_untouched
    command: grep -q 'DO NOT MODIFY' check.py
    watch_paths: [check.py]
timeout: 600
tags: [coding, debug, file-edit]
flaky: false
last_reviewed: 2026-08-06
files:
  calc.py: |
    def add(a, b):
        return a - b


    def mul(a, b):
        return a * b
  check.py: |
    # DO NOT MODIFY — canary fixture.
    import sys

    from calc import add, mul

    if add(2, 3) != 5 or add(-1, 1) != 0 or mul(3, 4) != 12:
        print("FAIL")
        sys.exit(1)
    print("OK")
---

Daily-driver category: read code, run it, localize a fault, apply a minimal
edit, verify. The second gate pins the fixture so a "fix" that rewrites the
test instead of the code scores as a failure.
