from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def parse_user_datetime(value: str, user_timezone: str) -> datetime:
    """Parse an ISO 8601 datetime from a Gemini decision into an aware UTC datetime.

    Naive values are interpreted in the user's timezone. Gemini interprets the
    human request; this function makes the timestamp authoritative.
    """
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(user_timezone))
    return parsed.astimezone(timezone.utc)


def ensure_future(due_at: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return due_at > now


def as_utc(value: datetime) -> datetime:
    """Attach UTC to naive datetimes loaded from SQLite."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def format_local(value: datetime, user_timezone: str) -> str:
    return as_utc(value).astimezone(ZoneInfo(user_timezone)).strftime("%d %b %Y, %I:%M %p")


WEEKDAYS = {
    "monday": 0,
    "somvar": 0,
    "tuesday": 1,
    "mangalvar": 1,
    "wednesday": 2,
    "budhvar": 2,
    "thursday": 3,
    "guruwar": 3,
    "guruvar": 3,
    "friday": 4,
    "shukravar": 4,
    "saturday": 5,
    "shanivar": 5,
    "sunday": 6,
    "ravivar": 6,
}


def parse_natural_schedule(
    text: str,
    user_timezone: str,
    now: datetime | None = None,
) -> datetime | None:
    """Deterministic fallback for common English, Hindi, and Hinglish times."""
    zone = ZoneInfo(user_timezone)
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    lowered = text.lower()

    relative = re.search(r"\b(\d+)\s*(minute|min|minutes|hour|hours|ghanta|ghante)\b", lowered)
    if relative and re.search(r"baad|after|later", lowered):
        amount = int(relative.group(1))
        in_hours = relative.group(2) in {"hour", "hours", "ghanta", "ghante"}
        delta = timedelta(hours=amount) if in_hours else timedelta(minutes=amount)
        return (local_now + delta).astimezone(timezone.utc)

    time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|baje)?\b", lowered)
    if not time_match:
        return None
    hour = int(time_match.group(1))
    minute = int(time_match.group(2) or 0)
    marker = time_match.group(3)
    if hour > 23 or minute > 59:
        return None
    evening = marker == "pm" or any(word in lowered for word in ("shaam", "raat", "dopahar"))
    morning = marker == "am" or "subah" in lowered
    if evening and hour < 12:
        hour += 12
    elif morning and not evening and hour == 12:
        # Midnight only when nothing in the phrase points at the evening, so
        # "subah 12" becomes 00:00 while "12 pm" stays at noon.
        hour = 0

    target_date = local_now.date()
    if "parso" in lowered or "day after tomorrow" in lowered:
        target_date += timedelta(days=2)
    elif re.search(r"\bkal\b|tomorrow", lowered):
        target_date += timedelta(days=1)
    else:
        weekday = next((value for name, value in WEEKDAYS.items() if name in lowered), None)
        if weekday is not None:
            days = (weekday - local_now.weekday()) % 7
            target_date += timedelta(days=days)

    candidate = datetime.combine(target_date, time(hour, minute), tzinfo=zone)
    if candidate <= local_now:
        candidate += timedelta(days=7 if any(name in lowered for name in WEEKDAYS) else 1)
    return candidate.astimezone(timezone.utc)


def parse_natural_interval(
    text: str,
    user_timezone: str,
    now: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    lowered = text.lower()
    match = re.search(
        r"\b(\d{1,2})(?::(\d{2}))?\s*(?:baje)?\s*(?:se|to|-)\s*"
        r"(\d{1,2})(?::(\d{2}))?\s*(?:baje)?\b",
        lowered,
    )
    if not match:
        return None
    start_hour = int(match.group(1))
    start_minute = int(match.group(2) or 0)
    end_hour = int(match.group(3))
    end_minute = int(match.group(4) or 0)
    if max(start_hour, end_hour) > 23 or max(start_minute, end_minute) > 59:
        return None
    period = ""
    if any(word in lowered for word in ("shaam", "raat", "dopahar")):
        period = " pm"
    elif "subah" in lowered:
        period = " am"
    start_text = re.sub(
        match.group(0),
        f"{start_hour}:{start_minute:02d}{period}",
        text,
        count=1,
    )
    starts_at = parse_natural_schedule(start_text, user_timezone, now)
    if starts_at is None:
        return None
    zone = ZoneInfo(user_timezone)
    local_start = starts_at.astimezone(zone)
    if period.strip() == "pm" and end_hour < 12:
        end_hour += 12
    elif period.strip() == "am" and end_hour == 12:
        end_hour = 0
    local_end = local_start.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if local_end <= local_start:
        local_end += timedelta(days=1)
    return starts_at, local_end.astimezone(timezone.utc)
