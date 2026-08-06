---
name: json-transform
prompt: |
  The workspace file data.json holds a list of order records. Produce
  output.json in the workspace root: a JSON object with exactly two keys —
  "shipped_total": the sum of "amount" over records whose status is
  "shipped", and "customers": the sorted list of unique "customer" values
  across ALL records. Use plain JSON (no comments, no trailing text).
gates:
  - name: output_valid
    command: python3 -c "import json; d=json.load(open('output.json')); assert d['shipped_total']==175, d; assert d['customers']==['acme','globex','initech'], d; print('OK')"
    watch_paths: [output.json]
timeout: 600
tags: [data, transform, coding]
flaky: false
last_reviewed: 2026-08-06
files:
  data.json: |
    [
      {"id": 1, "customer": "acme", "status": "shipped", "amount": 50},
      {"id": 2, "customer": "globex", "status": "pending", "amount": 10},
      {"id": 3, "customer": "acme", "status": "shipped", "amount": 75},
      {"id": 4, "customer": "initech", "status": "cancelled", "amount": 99},
      {"id": 5, "customer": "globex", "status": "shipped", "amount": 50}
    ]
---

Structured-data category: read, filter, aggregate, emit machine-checkable
output. The gate validates values, not formatting, so any JSON writer works.
Expected: shipped orders 1+3+5 → 50+75+50 = 175; customers sorted unique.
