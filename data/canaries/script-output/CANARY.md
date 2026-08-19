---
name: script-output
prompt: "Write a Python script stats.py in the workspace root that reads\nnumbers.txt\
  \ (one integer per line), and prints exactly two lines:\n  sum=<sum of the numbers>\n\
  \  max=<largest number>\nThen run it and confirm the output. Do not hardcode the\
  \ answers — the\nscript must actually read numbers.txt.\n"
gates:
- name: output_correct
  command: python3 stats.py | grep -qx 'sum=137' && python3 stats.py | grep -qx 'max=89'
  watch_paths:
  - stats.py
- name: reads_input
  command: grep -q 'numbers.txt' stats.py
  watch_paths:
  - stats.py
timeout: 600
tags:
- coding
- scripting
flaky: false
last_reviewed: '2026-08-19'
files:
  numbers.txt: '12

    7

    89

    -3

    32'
cadence: 12
---

Write-then-verify scripting category. The reads_input gate is a cheap
honesty check: printing the two known lines without reading the fixture
passes the first gate but not the second. Expected: 12+7+89-3+32 = 137.
