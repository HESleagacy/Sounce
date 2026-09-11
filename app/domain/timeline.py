from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.domain.reminders import as_utc


@dataclass(frozen=True, slots=True)
class Interval:
    title: str
    starts_at: datetime
    ends_at: datetime


def overlaps(
    new_start: datetime,
    new_end: datetime,
    existing_start: datetime,
    existing_end: datetime,
) -> bool:
    """Deterministic overlap rule from the project contract."""
    return as_utc(new_start) < as_utc(existing_end) and as_utc(new_end) > as_utc(existing_start)


def point_in_interval(point: datetime, starts_at: datetime, ends_at: datetime) -> bool:
    point = as_utc(point)
    return as_utc(starts_at) <= point < as_utc(ends_at)


def find_conflicts(
    intervals: list[Interval],
    new_start: datetime,
    new_end: datetime | None = None,
) -> list[Interval]:
    """Find conflicting intervals for an event range or a point-in-time reminder."""
    conflicts = []
    for interval in intervals:
        if new_end is None:
            if point_in_interval(new_start, interval.starts_at, interval.ends_at):
                conflicts.append(interval)
        elif overlaps(new_start, new_end, interval.starts_at, interval.ends_at):
            conflicts.append(interval)
    return conflicts
