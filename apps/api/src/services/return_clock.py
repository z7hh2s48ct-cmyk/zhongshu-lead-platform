from datetime import timedelta

from ..core.models import Assignment
from ..core.time import as_utc


def appeal_deadline(assignment: Assignment):
    """Retain resumed windows while normalizing old workday-based deadlines."""
    if assignment.appeal_resumed_at is not None:
        return as_utc(assignment.appeal_deadline_at)
    claimed_at = as_utc(assignment.claimed_at)
    return claimed_at + timedelta(hours=48) if claimed_at else None
