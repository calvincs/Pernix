---
name: multi-file-rename
prompt: 'Across the workspace files lib.py and main.py, rename the function

  fetch_data to load_records everywhere (definition and all call sites).

  Do not change any other behavior. When done, run main.py to confirm it

  still prints RESULT=42.

  '
gates:
- name: still_runs
  command: python3 main.py | grep -qx 'RESULT=42'
  watch_paths:
  - lib.py
  - main.py
- name: old_name_gone
  command: '! grep -rn ''fetch_data'' lib.py main.py'
  watch_paths:
  - lib.py
  - main.py
- name: new_name_present
  command: grep -q 'def load_records' lib.py
  watch_paths:
  - lib.py
timeout: 600
tags:
- coding
- refactor
- multi-file
flaky: false
last_reviewed: '2026-08-19'
files:
  lib.py: "def fetch_data(source):\n    # pretend this hits a database\n    return\
    \ {\"local\": 40, \"remote\": 2}[source]\n\n\ndef combine():\n    return fetch_data(\"\
    local\") + fetch_data(\"remote\")\n"
  main.py: 'from lib import combine, fetch_data


    total = combine()

    assert fetch_data("remote") == 2

    print(f"RESULT={total}")'
cadence: 12
---

Multi-file refactor category: a rename must be complete (no stale call
sites — including the import in main.py), behavior-preserving (RESULT=42),
and verified by running the code, not by eyeballing the diff.
