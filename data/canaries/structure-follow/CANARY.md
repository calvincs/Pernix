---
name: structure-follow
prompt: |
  Set up this exact structure in the workspace:
  1. A directory named reports/
  2. Inside it, a file summary.md whose FIRST line is exactly:
     # Weekly Summary
  3. The file must also contain a section line exactly:
     ## Open Items
  4. A top-level file named .status containing the single word: ready
gates:
  - name: dir_and_files
    command: test -d reports && test -f reports/summary.md && test -f .status
    watch_paths: [reports/, .status]
  - name: first_line
    command: head -n 1 reports/summary.md | grep -qx '# Weekly Summary'
    watch_paths: [reports/summary.md]
  - name: section_present
    command: grep -qx '## Open Items' reports/summary.md
    watch_paths: [reports/summary.md]
  - name: status_ready
    command: grep -qx 'ready' .status
    watch_paths: [.status]
timeout: 300
tags: [instruction-following, file-write]
flaky: false
last_reviewed: 2026-08-06
---

Precise multi-step instruction following: four independent requirements,
four independent gates. Partial completion shows up as a specific failing
gate name, which makes drift diagnosable ("it stopped creating dotfiles")
rather than a mush of "the canary failed".
