"""Pernix — Tool/skill performance dataclass.

Snooze writes success/failure counters via db.upsert_signal; the Skills and
Tools UI sections read them for display. No longer fed to scout as PREFER/AVOID
guidance — counters are purely observational.
"""

from __future__ import annotations

from dataclasses import dataclass

# Failure ratio at or above this threshold is flagged as a poor performer.
POOR_PERFORMER_THRESHOLD: float = 0.20


@dataclass
class Signal:
    """One row from the scout_signals table."""

    signal_type: str
    subject: str
    reinforcements: int = 0  # total observations (uses)
    successes: int = 0
    failures: int = 0
    first_seen_at: str = ""  # ISO8601
    last_reinforced_at: str = ""  # ISO8601

    @property
    def is_poor_performer(self) -> bool:
        """True when failure ratio >= POOR_PERFORMER_THRESHOLD."""
        if self.reinforcements <= 0:
            return False
        return (self.failures / self.reinforcements) >= POOR_PERFORMER_THRESHOLD

    def to_display(self) -> dict:
        """Compact dict for embedding in skills/tools API responses."""
        return {
            "uses": self.reinforcements,
            "failures": self.failures,
            "is_poor_performer": self.is_poor_performer,
        }


def from_row(row: dict) -> Signal:
    """Build a Signal from a db.get_signal / get_top_signals row (dict)."""
    return Signal(
        signal_type=row["signal_type"],
        subject=row["subject"],
        reinforcements=int(row.get("reinforcements") or 0),
        successes=int(row.get("successes") or 0),
        failures=int(row.get("failures") or 0),
        first_seen_at=row.get("first_seen_at") or "",
        last_reinforced_at=row.get("last_reinforced_at") or "",
    )
