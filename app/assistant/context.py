from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from app.domain.reminders import format_local
from app.domain.retrieval import rank, trim_to_budget
from app.persistence.models import Message, PendingAction, User
from app.persistence.repositories import Repository

# How many candidates to pull before ranking, and how many survive into the prompt.
MEMORY_POOL = 200
MEMORY_LIMIT = 12
DOCUMENT_POOL = 30
DOCUMENT_LIMIT = 3
DOCUMENT_TEXT_CHARS = 2000
# Hard ceiling on the serialized context. Roughly 6k tokens, well inside the
# model's window while leaving room for the system prompt and the reply.
CONTEXT_CHAR_BUDGET = 24000


@dataclass(frozen=True, slots=True)
class AssistantContext:
    user_timezone: str
    current_time: str
    preferences: dict[str, str]
    memories: list[dict[str, Any]]
    reminders: list[dict[str, Any]]
    timeline_events: list[dict[str, Any]]
    documents: list[dict[str, Any]]
    pending_action: dict[str, Any] | None
    recent_messages: list[dict[str, Any]]

    def as_prompt(self) -> str:
        return json.dumps(self._payload(), ensure_ascii=False)

    def _payload(self) -> dict[str, Any]:
        return {
            "timezone": self.user_timezone,
            "current_time": self.current_time,
            "preferences": self.preferences,
            "memories": self.memories,
            "upcoming_reminders": self.reminders,
            "upcoming_timeline_events": self.timeline_events,
            "recent_documents": self.documents,
            "pending_action": self.pending_action,
            "recent_messages": self.recent_messages,
        }


def build_context(
    repository: Repository,
    user: User,
    pending: PendingAction | None,
    query: str | None = None,
) -> AssistantContext:
    """Assemble the prompt context, ranked against ``query`` and size-bounded.

    ``query`` is the message being handled. Passing it turns memory and document
    selection from "most recent" into "most relevant, plus the most recent few",
    which is what keeps the prompt useful as the store grows.
    """
    now_local = datetime.now(timezone.utc).astimezone(ZoneInfo(user.timezone))

    memory_candidates = repository.list_memories(user.id, limit=MEMORY_POOL)
    memories = [
        {
            "id": item.id,
            "kind": item.kind,
            "category": item.category,
            "content": item.content,
            "source": _message_source(repository, item.source_message_id),
        }
        for item in rank(
            query,
            memory_candidates,
            lambda item: f"{item.category} {item.kind} {item.content}",
            limit=MEMORY_LIMIT,
        )
    ]
    reminders = [
        {
            "id": item.id,
            "title": item.title,
            "category": item.category,
            "due_at_local": format_local(item.due_at, user.timezone),
            "recurrence": (
                f"every {item.recurrence_interval} {item.recurrence_frequency}"
                if item.recurrence_frequency
                else None
            ),
        }
        for item in repository.list_upcoming_reminders(user.id)
    ]
    events = [
        {
            "title": item.title,
            "category": item.category,
            "starts_at_local": format_local(item.starts_at, user.timezone),
            "ends_at_local": format_local(item.ends_at, user.timezone),
        }
        for item in repository.list_upcoming_timeline_events(user.id)
    ]
    document_candidates = repository.list_recent_documents(user.id, limit=DOCUMENT_POOL)
    documents = [
        {
            "id": item.id,
            "filename": item.filename,
            "summary": item.summary,
            "extracted_text": (item.extracted_text or "")[:DOCUMENT_TEXT_CHARS],
            "document_type": item.document_type,
            "dates": item.extracted_dates,
            "amounts": item.extracted_amounts,
            "entities": item.extracted_entities,
            "source": _message_source(repository, item.source_message_id),
        }
        for item in rank(
            query,
            document_candidates,
            lambda item: f"{item.filename} {item.document_type or ''} {item.summary or ''}",
            limit=DOCUMENT_LIMIT,
            keep_recent=1,
        )
    ]
    pending_payload: dict[str, Any] | None = None
    if pending is not None:
        pending_payload = {"action_type": pending.action_type, **pending.payload}
    recent_messages = [
        {
            "direction": message.direction,
            "type": message.message_type,
            "text": message.text or message.transcript,
        }
        for message in repository.list_recent_messages(user.id)
        if message.text or message.transcript
    ]
    context = AssistantContext(
        user_timezone=user.timezone,
        current_time=f"{now_local.isoformat()} ({now_local.strftime('%A')})",
        preferences=repository.get_preferences(user.id),
        memories=memories,
        reminders=reminders,
        timeline_events=events,
        documents=documents,
        pending_action=pending_payload,
        recent_messages=recent_messages,
    )
    _enforce_budget(context)
    return context


def _enforce_budget(context: AssistantContext) -> None:
    """Shrink the context in place until it fits, sacrificing the least useful first.

    Documents go before memories, and memories before recent messages, because a
    dropped memory costs recall while a dropped conversation turn costs coherence
    on the very next reply.
    """

    def length() -> int:
        return len(context.as_prompt())

    if length() <= CONTEXT_CHAR_BUDGET:
        return
    for document in context.documents:
        document["extracted_text"] = str(document.get("extracted_text") or "")[:500]
    trim_to_budget(
        [context.documents, context.memories, context.recent_messages],
        length,
        CONTEXT_CHAR_BUDGET,
    )


def _message_source(repository: Repository, message_id: int) -> dict[str, Any] | None:
    message = repository.session.get(Message, message_id)
    if message is None:
        return None
    return {
        "message_id": message.whatsapp_message_id,
        "text": message.text,
        "transcript": message.transcript,
        "created_at": message.created_at.isoformat(),
    }
