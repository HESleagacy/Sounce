from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"


class GoogleCalendarProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._calendar_id = calendar_id
        self._timeout_seconds = timeout_seconds

    def create_event(self, title: str, starts_at: datetime, ends_at: datetime, timezone_name: str) -> str:
        response = httpx.post(
            self._events_url(),
            headers=self._headers(),
            json=self._event_body(title, starts_at, ends_at, timezone_name),
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        event_id = response.json()["id"]
        log.info("Created Google Calendar event %s for %r", event_id, title)
        return event_id

    def update_event(
        self,
        event_id: str,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        timezone_name: str,
    ) -> None:
        response = httpx.patch(
            f"{self._events_url()}/{quote(event_id, safe='')}",
            headers=self._headers(),
            json=self._event_body(title, starts_at, ends_at, timezone_name),
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        log.info("Updated Google Calendar event %s", event_id)

    def delete_event(self, event_id: str) -> None:
        response = httpx.delete(
            f"{self._events_url()}/{quote(event_id, safe='')}",
            headers=self._headers(),
            timeout=self._timeout_seconds,
        )
        if response.status_code not in {204, 404, 410}:
            response.raise_for_status()
        log.info("Deleted Google Calendar event %s", event_id)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token()}"}

    def _access_token(self) -> str:
        response = httpx.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        return response.json()["access_token"]

    def _events_url(self) -> str:
        return EVENTS_URL.format(calendar_id=quote(self._calendar_id, safe=""))

    @staticmethod
    def _event_body(
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        timezone_name: str,
    ) -> dict[str, object]:
        return {
            "summary": title,
            "start": {"dateTime": starts_at.isoformat(), "timeZone": timezone_name},
            "end": {"dateTime": ends_at.isoformat(), "timeZone": timezone_name},
            "extendedProperties": {"private": {"managedBy": "sounce"}},
        }


def load_refresh_token(configured_token: str, token_path: Path) -> str:
    if configured_token:
        return configured_token
    if not token_path.exists():
        return ""
    payload = json.loads(token_path.read_text(encoding="utf-8"))
    return str(payload.get("refresh_token", ""))


def build_calendar_provider(
    client_id: str,
    client_secret: str,
    refresh_token: str,
    calendar_id: str,
    token_path: Path | None = None,
) -> GoogleCalendarProvider | None:
    token = load_refresh_token(refresh_token, token_path or Path("data/google_calendar_token.json"))
    if not client_id or not client_secret or not token:
        return None
    return GoogleCalendarProvider(client_id, client_secret, token, calendar_id)
