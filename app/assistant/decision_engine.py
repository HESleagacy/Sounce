from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from app.assistant.schemas import AssistantDecision, ProposedAction
from app.domain.reminders import as_utc, ensure_future, format_local, parse_user_datetime
from app.domain.timeline import Interval, find_conflicts
from app.persistence.models import Message, PendingAction, User
from app.persistence.repositories import Repository

log = logging.getLogger(__name__)

PREFERENCE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DIRECT_ACTIONS = {"store_memory", "update_preference"}
CONFIRM_ACTIONS = {
    "create_reminder",
    "create_timeline_event",
    "cancel_reminder",
    "reschedule_reminder",
    "forget_memory",
    "purge_all_data",
}
DEFAULT_EVENT_MINUTES = 60


class CalendarSync(Protocol):
    def create_event(self, title: str, starts_at: datetime, ends_at: datetime, timezone_name: str) -> str: ...

    def update_event(
        self, event_id: str, title: str, starts_at: datetime, ends_at: datetime, timezone_name: str
    ) -> None: ...

    def delete_event(self, event_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    response: str
    executed_actions: int
    # Erasing everything cannot happen inside this transaction: the reply still
    # has to be written against rows the purge is about to remove. The caller
    # performs it after the confirmation has been delivered.
    purge_requested: bool = False


class ValidationFailure(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class DecisionEngine:
    def __init__(self, pending_action_ttl_minutes: int, calendar: CalendarSync | None = None) -> None:
        self._pending_action_ttl_minutes = pending_action_ttl_minutes
        # The engine never calls Calendar itself; it only needs to know whether
        # syncing is configured so it can decide to write outbox rows. The live
        # client belongs to CalendarWorker, which drains those rows with retries.
        self._calendar_enabled = calendar is not None

    def execute(
        self,
        decision: AssistantDecision,
        repository: Repository,
        user: User,
        source_message: Message,
        pending: PendingAction | None,
    ) -> ExecutionResult:
        if decision.intent == "confirm_action":
            return self._execute_confirmed(decision, repository, user, source_message, pending)

        if decision.intent == "clarify" or decision.missing_fields:
            partial = [action.model_dump(mode="json") for action in decision.proposed_actions]
            action_type = (
                decision.proposed_actions[0].action_type if decision.proposed_actions else decision.intent
            )
            repository.replace_pending_action(
                user_id=user.id,
                source_message_id=source_message.id,
                action_type=action_type,
                payload={
                    "stage": "clarify",
                    "proposed_actions": partial,
                    "missing_fields": decision.missing_fields,
                },
                ttl_minutes=self._pending_action_ttl_minutes,
            )
            return ExecutionResult(decision.response, 0)

        direct = [a for a in decision.proposed_actions if a.action_type in DIRECT_ACTIONS]
        deferred = [a for a in decision.proposed_actions if a.action_type in CONFIRM_ACTIONS]

        try:
            deferred, conflict_response = self._prepare_deferred(deferred, repository, user)
        except ValidationFailure as failure:
            repository.resolve_pending_action(pending)
            return ExecutionResult(failure.user_message, 0)

        executed = self._execute_direct(direct, repository, user, source_message)
        repository.resolve_pending_action(pending)

        if deferred:
            repository.replace_pending_action(
                user_id=user.id,
                source_message_id=source_message.id,
                action_type="confirm",
                payload={
                    "stage": "confirm",
                    "proposed_actions": [a.model_dump(mode="json") for a in deferred],
                },
                ttl_minutes=self._pending_action_ttl_minutes,
            )
            return ExecutionResult(conflict_response or decision.response, executed)

        return ExecutionResult(decision.response, executed)

    def _execute_confirmed(
        self,
        decision: AssistantDecision,
        repository: Repository,
        user: User,
        source_message: Message,
        pending: PendingAction | None,
    ) -> ExecutionResult:
        if pending is None or pending.payload.get("stage") != "confirm":
            return ExecutionResult(
                "Action abhi poori tarah samajh nahi aayi. Date aur time dobara batayein.",
                0,
            )
        actions = [ProposedAction.model_validate(raw) for raw in pending.payload.get("proposed_actions", [])]
        try:
            actions, _conflict_response = self._prepare_deferred(actions, repository, user)
        except ValidationFailure as failure:
            repository.resolve_pending_action(pending)
            return ExecutionResult(failure.user_message, 0)

        executed = 0
        action_source = repository.session.get(Message, pending.source_message_id) or source_message
        for action in actions:
            executed += self._execute_confirmed_action(action, repository, user, action_source)
        repository.resolve_pending_action(pending)
        purge_requested = any(action.action_type == "purge_all_data" for action in actions)
        return ExecutionResult(decision.response, executed, purge_requested=purge_requested)

    def _execute_direct(
        self,
        actions: list[ProposedAction],
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> int:
        executed = 0
        for action in actions:
            if action.action_type == "store_memory":
                if not action.content:
                    raise ValueError("store_memory requires content")
                repository.add_memory(
                    user_id=user.id,
                    source_message_id=source_message.id,
                    kind=action.kind or "personal_fact",
                    category=action.category or "general",
                    content=action.content,
                    confidence=action.confidence,
                )
                executed += 1
            elif action.action_type == "update_preference":
                key = action.key or ""
                if not PREFERENCE_KEY.fullmatch(key) or action.value is None:
                    raise ValueError(f"Invalid preference key: {key!r}")
                repository.set_preference(user.id, source_message.id, key, action.value)
                preferences = repository.get_preferences(user.id)
                if key == "quiet_hours_start" and "quiet_hours_end" not in preferences:
                    repository.set_preference(user.id, source_message.id, "quiet_hours_end", "07:00")
                elif key == "quiet_hours_end" and "quiet_hours_start" not in preferences:
                    repository.set_preference(user.id, source_message.id, "quiet_hours_start", "22:00")
                executed += 1
        return executed

    def _execute_confirmed_action(
        self,
        action: ProposedAction,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> int:
        if action.action_type == "create_reminder":
            due_at = parse_user_datetime(action.scheduled_at or "", user.timezone)
            reminder = repository.add_reminder(
                user_id=user.id,
                source_message_id=source_message.id,
                title=action.title or "",
                due_at=due_at,
                timezone_name=user.timezone,
                recurrence_frequency=action.recurrence_frequency,
                recurrence_interval=action.recurrence_interval,
                category=action.category,
            )
            self._enqueue_calendar(
                repository,
                user,
                op="create",
                entity_type="reminder",
                entity=reminder,
                payload={
                    "title": action.title or "",
                    "starts_at": due_at.isoformat(),
                    "ends_at": (due_at + timedelta(minutes=DEFAULT_EVENT_MINUTES)).isoformat(),
                    "timezone": user.timezone,
                },
            )
            return 1
        if action.action_type == "create_timeline_event":
            starts_at = parse_user_datetime(action.starts_at or "", user.timezone)
            ends_at = parse_user_datetime(action.ends_at or "", user.timezone)
            event = repository.add_timeline_event(
                user_id=user.id,
                source_message_id=source_message.id,
                title=action.title or "",
                starts_at=starts_at,
                ends_at=ends_at,
                category=action.event_category,
            )
            self._create_category_reminder(action, repository, user, source_message, starts_at)
            self._enqueue_calendar(
                repository,
                user,
                op="create",
                entity_type="timeline_event",
                entity=event,
                payload={
                    "title": action.title or "",
                    "starts_at": starts_at.isoformat(),
                    "ends_at": ends_at.isoformat(),
                    "timezone": user.timezone,
                },
            )
            return 1
        if action.action_type == "cancel_reminder":
            if action.reminder_id is None:
                raise ValidationFailure("Yeh reminder ab pending nahi hai, isliye cancel nahi ho paya.")
            existing = repository.get_reminder(user.id, action.reminder_id)
            if existing is None or not repository.cancel_reminder(user.id, action.reminder_id):
                raise ValidationFailure("Yeh reminder ab pending nahi hai, isliye cancel nahi ho paya.")
            if existing.calendar_event_id or existing.calendar_sync_status == "pending":
                self._enqueue_calendar(
                    repository,
                    user,
                    op="delete",
                    entity_type="reminder",
                    entity=existing,
                    payload={},
                )
            return 1
        if action.action_type == "reschedule_reminder":
            due_at = parse_user_datetime(action.scheduled_at or "", user.timezone)
            if action.reminder_id is None:
                raise ValidationFailure("Yeh reminder pending nahi mila, isliye reschedule nahi hua.")
            existing = repository.get_reminder(user.id, action.reminder_id)
            if existing is None or not repository.reschedule_reminder(user.id, action.reminder_id, due_at):
                raise ValidationFailure("Yeh reminder pending nahi mila, isliye reschedule nahi hua.")
            if existing.calendar_event_id or existing.calendar_sync_status == "pending":
                self._enqueue_calendar(
                    repository,
                    user,
                    op="update",
                    entity_type="reminder",
                    entity=existing,
                    payload={
                        "title": existing.title,
                        "starts_at": due_at.isoformat(),
                        "ends_at": (due_at + timedelta(minutes=DEFAULT_EVENT_MINUTES)).isoformat(),
                        "timezone": user.timezone,
                    },
                )
            return 1
        if action.action_type == "forget_memory":
            if action.memory_id is None or not repository.forget_memory(user.id, action.memory_id):
                raise ValidationFailure("Yeh yaad nahi mili, isliye kuch delete nahi hua.")
            return 1
        if action.action_type == "purge_all_data":
            # Deliberately a no-op here. See ExecutionResult.purge_requested.
            return 1
        return 0

    def _prepare_deferred(
        self,
        actions: list[ProposedAction],
        repository: Repository,
        user: User,
    ) -> tuple[list[ProposedAction], str | None]:
        prepared: list[ProposedAction] = []
        conflict_response: str | None = None
        intervals = [
            Interval(title=event.title, starts_at=as_utc(event.starts_at), ends_at=as_utc(event.ends_at))
            for event in repository.list_upcoming_timeline_events(user.id, limit=50)
        ]
        for action in actions:
            if action.action_type == "create_reminder":
                if not action.title:
                    raise ValidationFailure("Reminder ka title samajh nahi aaya, dobara batayein.")
                due_at = self._parse_or_fail(action.scheduled_at, user.timezone)
                if not ensure_future(due_at):
                    raise ValidationFailure("Yeh samay pehle hi guzar chuka hai. Naya samay batayein.")
                conflicts = find_conflicts(intervals, due_at)
                if conflicts:
                    conflict = conflicts[0]
                    action = action.model_copy(update={"scheduled_at": as_utc(conflict.ends_at).isoformat()})
                    conflict_response = self._conflict_question(conflict, action.title, user.timezone)
            elif action.action_type == "create_timeline_event":
                if not action.title:
                    raise ValidationFailure("Event ka title samajh nahi aaya, dobara batayein.")
                starts_at = self._parse_or_fail(action.starts_at, user.timezone)
                ends_at = self._parse_or_fail(action.ends_at, user.timezone)
                if ends_at <= starts_at:
                    raise ValidationFailure("Event ka end time start ke baad hona chahiye.")
                conflicts = find_conflicts(intervals, starts_at, ends_at)
                if conflicts:
                    conflict = conflicts[0]
                    duration = ends_at - starts_at
                    suggested_start = as_utc(conflict.ends_at)
                    action = action.model_copy(
                        update={
                            "starts_at": suggested_start.isoformat(),
                            "ends_at": (suggested_start + duration).isoformat(),
                        }
                    )
                    conflict_response = self._conflict_question(conflict, action.title, user.timezone)
            elif action.action_type == "cancel_reminder":
                if action.reminder_id is None or repository.get_reminder(user.id, action.reminder_id) is None:
                    raise ValidationFailure("Yeh reminder nahi mila. Pehle apne reminders ki list dekh lein.")
            elif action.action_type == "reschedule_reminder":
                if action.reminder_id is None or repository.get_reminder(user.id, action.reminder_id) is None:
                    raise ValidationFailure("Yeh reminder nahi mila. Pehle apne reminders ki list dekh lein.")
                due_at = self._parse_or_fail(action.scheduled_at, user.timezone)
                if not ensure_future(due_at):
                    raise ValidationFailure("Naya samay pehle hi guzar chuka hai. Dobara batayein.")
            elif action.action_type == "forget_memory":
                if action.memory_id is None or repository.get_memory(user.id, action.memory_id) is None:
                    raise ValidationFailure("Yeh yaad nahi mili. Pehle apni memories ki list dekh lein.")
            prepared.append(action)
        return prepared, conflict_response

    @staticmethod
    def _parse_or_fail(value: str | None, timezone_name: str) -> datetime:
        try:
            return parse_user_datetime(value or "", timezone_name)
        except ValueError as exc:
            raise ValidationFailure("Samay samajh nahi aaya. Date aur time dobara batayein.") from exc

    @staticmethod
    def _conflict_question(conflict: Interval, title: str | None, timezone_name: str) -> str:
        end = format_local(conflict.ends_at, timezone_name)
        return (
            f"Us samay '{conflict.title}' bhi hai. "
            f"'{title or 'Naya kaam'}' ko uske baad, {end} par rakh doon?"
        )

    def _create_category_reminder(
        self,
        action: ProposedAction,
        repository: Repository,
        user: User,
        source_message: Message,
        starts_at: datetime,
    ) -> None:
        if not action.event_category:
            return
        key = f"{action.event_category}_reminder_minutes"
        raw_minutes = repository.get_preferences(user.id).get(key)
        if raw_minutes is None:
            return
        try:
            minutes = int(raw_minutes)
        except ValueError:
            log.warning("Ignoring invalid %s preference: %r", key, raw_minutes)
            return
        due_at = starts_at - timedelta(minutes=minutes)
        if ensure_future(due_at):
            repository.add_reminder(
                user_id=user.id,
                source_message_id=source_message.id,
                title=f"Upcoming: {action.title}",
                due_at=due_at,
                timezone_name=user.timezone,
            )

    def _enqueue_calendar(
        self,
        repository: Repository,
        user: User,
        op: str,
        entity_type: str,
        entity: object,
        payload: dict[str, object],
    ) -> None:
        """Record a Calendar operation in the same transaction as the local change.

        This is the whole point of the outbox: the op row and the reminder row
        commit together, so Calendar can be down for an hour without the two
        drifting apart, and a remote create can never be lost by a failure to
        persist its event id.
        """
        if not self._calendar_enabled:
            return
        repository.enqueue_calendar_op(
            user_id=user.id,
            op=op,
            entity_type=entity_type,
            entity_id=entity.id,  # type: ignore[attr-defined]
            payload=payload,
        )
        entity.calendar_sync_status = "pending"  # type: ignore[attr-defined]
