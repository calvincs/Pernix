"""Pernix — Harness-side helpers that act on the agent's tool stream.

These modules implement *passive* interventions: the harness watches the
agent's tool inputs/outputs and injects targeted hints when known
anti-patterns appear. The agent is free to ignore the hint, but at least
it sees the suggestion at the moment it's most useful.
"""
