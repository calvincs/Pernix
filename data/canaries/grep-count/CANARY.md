---
name: grep-count
prompt: |
  The logs/ directory in the workspace contains application log files. Count
  how many lines across ALL files in logs/ contain the substring "ERROR"
  (case-sensitive; any occurrence anywhere in the line counts). Write just
  that number as a single line to answer.txt in the workspace root.
gates:
  - name: answer_correct
    command: grep -qx '8' answer.txt
    watch_paths: [answer.txt]
timeout: 300
tags: [search, analysis, sentinel]
flaky: false
last_reviewed: 2026-08-06
files:
  logs/app.log: |
    2026-08-01 09:00:01 INFO service started
    2026-08-01 09:00:05 ERROR db connection refused
    2026-08-01 09:00:06 WARN retrying in 5s
    2026-08-01 09:00:11 ERROR db connection refused
    2026-08-01 09:00:16 INFO db connected
    2026-08-01 09:12:44 ERROR timeout on /api/report
  logs/worker.log: |
    2026-08-01 09:01:00 INFO worker pool up
    2026-08-01 09:03:12 ERROR job 442 failed: KeyError('user')
    2026-08-01 09:03:12 error lowercase should not count
    2026-08-01 09:05:00 ERROR job 443 failed: KeyError('user')
    2026-08-01 09:07:31 WARN queue depth 40
    2026-08-01 09:15:02 ERROR job 448 failed: timeout
  logs/audit.log: |
    2026-08-01 09:00:00 INFO audit trail enabled
    2026-08-01 09:20:15 ERROR permission denied for token rotation
    2026-08-01 09:21:00 INFO ERRORS-SUMMARY generated
---

Search-and-aggregate over seeded fixtures. Traps: a lowercase "error" line
(case-sensitive, must not count) and the ERRORS-SUMMARY line (substring
semantics, spelled out in the prompt — it counts). Expected: 7 clean ERROR
lines (3 app + 3 worker + 1 audit) + the ERRORS-SUMMARY line = 8.
