---
name: file-create
prompt: |
  Create a file named hello.txt in the workspace root containing exactly this
  single line (no extra whitespace, no trailing commentary in the file):

  Hello from Pernix canary.
gates:
  - name: file_exact
    command: grep -qx 'Hello from Pernix canary.' hello.txt
    watch_paths: [hello.txt]
timeout: 300
tags: [file-write, instruction-following, sentinel]
flaky: false
last_reviewed: 2026-08-06
---

The simplest possible canary: one tool call, one exact deliverable. If this
fails, something fundamental broke (tool dispatch, workspace override, or
instruction following) — treat any regression here as a pipeline problem,
not a model problem.
