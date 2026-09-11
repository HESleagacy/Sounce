"""Turns a user message into an executed decision.

Policy for deterministic parsing
--------------------------------
Gemini decides. The regex helpers in this module are allowed to do exactly two
things, and nothing else:

1. **Fill gaps.** Complete fields the model left empty on an action it already
   proposed, or rescue an unmistakably scheduling-shaped message the model
   failed to act on. Every action they touch is in ``CONFIRM_ACTIONS``, so the
   user still sees and approves the result before anything is written.
2. **Stand in for an unavailable provider.** ``_fallback_decision`` runs only
   when the provider raises.

They may never write a memory or a preference. Those execute directly, without
confirmation, and a regex is not strong enough evidence to mutate state the user
will later rely on. An earlier version of this file short-circuited Gemini with
a table of hardcoded Hinglish phrases that did exactly that; it made the demo
look good and the system dishonest.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.assistant.context import build_context
from app.assistant.decision_engine import DecisionEngine, ExecutionResult
from app.assistant.schemas import AssistantDecision, ProposedAction
from app.domain.reminders import parse_natural_interval, parse_natural_schedule
from app.domain.retrieval import score, tokenize
from app.persistence.models import Message, User
from app.persistence.repositories import Repository
from app.providers.gemini import DecisionProvider

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MediaResult:
    execution: ExecutionResult
    transcript: str | None
    document_summary: str | None
    document_extracted_text: str | None
    document_type: str | None
    document_dates: list[str]
    document_amounts: list[str]
    document_entities: list[str]
    detected_languages: list[str]


class AssistantService:
    def __init__(self, provider: DecisionProvider, decision_engine: DecisionEngine) -> None:
        self._provider = provider
        self._decision_engine = decision_engine

    def handle_text(
        self,
        text: str,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> ExecutionResult:
        provenance_response = self._memory_provenance_response(text, repository, user)
        if provenance_response is not None:
            return ExecutionResult(provenance_response, 0)
        pending = repository.get_pending_action(user.id)
        context = build_context(repository, user, pending, query=text)
        try:
            decision = self._provider.interpret(text, context.as_prompt())
        except Exception:
            log.exception("Decision provider failed; falling back to deterministic parsing")
            decision = self._fallback_decision(text, user)
        decision = self._normalize_timeline(text, decision, user)
        decision = self._normalize_reschedule(text, decision, repository, user)
        return self._decision_engine.execute(decision, repository, user, source_message, pending)

    def handle_audio(
        self,
        audio: bytes,
        mime_type: str,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> MediaResult:
        pending = repository.get_pending_action(user.id)
        # A voice note has no text to rank against until it is transcribed, so
        # the context falls back to recency for this turn.
        context = build_context(repository, user, pending)
        decision = self._provider.interpret_audio(audio, mime_type, context.as_prompt())
        execution = self._decision_engine.execute(decision, repository, user, source_message, pending)
        detected_languages = getattr(decision, "detected_languages", [])
        if detected_languages:
            try:
                response = self._provider.localize_response(
                    execution.response,
                    detected_languages[0],
                )
                execution = ExecutionResult(response, execution.executed_actions)
            except Exception:
                log.exception("Audio response localization failed; using the original response")
        return self._media_result(execution, decision)

    def handle_document(
        self,
        data: bytes,
        mime_type: str,
        filename: str,
        caption: str | None,
        repository: Repository,
        user: User,
        source_message: Message,
    ) -> MediaResult:
        pending = repository.get_pending_action(user.id)
        context = build_context(repository, user, pending, query=caption or filename)
        decision = self._provider.interpret_document(data, mime_type, filename, caption, context.as_prompt())
        if not caption:
            summary = decision.document_summary or "Document process ho gaya hai."
            decision = decision.model_copy(
                update={
                    "intent": "document_received",
                    "response": f"Maine document padha: {summary}\n\nAap iske saath kya karna chahte hain?",
                    "proposed_actions": [],
                    "missing_fields": [],
                }
            )
        execution = self._decision_engine.execute(decision, repository, user, source_message, pending)
        return self._media_result(execution, decision)

    @staticmethod
    def _media_result(execution: ExecutionResult, decision: AssistantDecision) -> MediaResult:
        return MediaResult(
            execution=execution,
            transcript=decision.transcript,
            document_summary=decision.document_summary,
            document_extracted_text=decision.document_extracted_text,
            document_type=decision.document_type,
            document_dates=decision.document_dates,
            document_amounts=decision.document_amounts,
            document_entities=decision.document_entities,
            detected_languages=getattr(decision, "detected_languages", []),
        )

    @staticmethod
    def _normalize_reschedule(
        text: str,
        decision: AssistantDecision,
        repository: Repository,
        user: User,
    ) -> AssistantDecision:
        if not re.search(r"reschedul|time\s+badal|samay\s+badal|shift|aage\s+kar", text, re.IGNORECASE):
            return decision
        reminders = repository.list_upcoming_reminders(user.id)
        parsed = parse_natural_schedule(text, user.timezone)
        actions = [
            action
            for action in decision.proposed_actions
            if action.action_type in {"create_reminder", "reschedule_reminder"}
        ]
        target_id = actions[0].reminder_id if actions else None
        if target_id is None and len(reminders) == 1:
            target_id = reminders[0].id
        if target_id is None:
            return decision.model_copy(
                update={
                    "intent": "clarify",
                    "response": "Kaunsa reminder reschedule karna hai?",
                    "proposed_actions": [],
                    "missing_fields": ["reminder_id"],
                }
            )
        if not actions:
            if parsed is None:
                return decision.model_copy(
                    update={
                        "intent": "clarify",
                        "response": "Reminder ka naya date aur time kya hona chahiye?",
                        "proposed_actions": [],
                        "missing_fields": ["scheduled_at"],
                    }
                )
            actions = [
                ProposedAction(
                    action_type="reschedule_reminder",
                    reminder_id=target_id,
                    scheduled_at=parsed.isoformat(),
                )
            ]
            decision = decision.model_copy(
                update={"response": "Reminder ko naye samay par reschedule kar doon?"}
            )
        normalized = actions[0].model_copy(
            update={
                "action_type": "reschedule_reminder",
                "reminder_id": target_id,
                "recurrence_frequency": None,
                "scheduled_at": actions[0].scheduled_at or (parsed.isoformat() if parsed else None),
            }
        )
        return decision.model_copy(
            update={
                "intent": "reschedule_reminder",
                "proposed_actions": [normalized],
                "missing_fields": [] if normalized.scheduled_at else ["scheduled_at"],
            }
        )

    @staticmethod
    def _fallback_decision(text: str, user: User) -> AssistantDecision:
        """Deterministic decision used only when the provider is unavailable.

        Conservative by construction: it can propose a reminder or a timeline
        event, both of which require confirmation, and otherwise it says it did
        not understand. It never proposes a direct-execution action.
        """
        interval = parse_natural_interval(text, user.timezone)
        if interval is not None and re.search(r"appointment|event|meeting|commitment", text, re.IGNORECASE):
            starts_at, ends_at = interval
            return AssistantDecision(
                intent="create_timeline_event",
                response="Main abhi poori tarah samajh nahi paya, lekin yeh timeline mein add kar doon?",
                proposed_actions=[
                    ProposedAction(
                        action_type="create_timeline_event",
                        title=text.strip()[:80],
                        starts_at=starts_at.isoformat(),
                        ends_at=ends_at.isoformat(),
                    )
                ],
            )
        scheduled_at = parse_natural_schedule(text, user.timezone)
        if scheduled_at is not None and re.search(r"remind|reminder|yaad", text, re.IGNORECASE):
            return AssistantDecision(
                intent="create_reminder",
                response="Main abhi poori tarah samajh nahi paya, lekin is samay ka reminder laga doon?",
                proposed_actions=[
                    ProposedAction(
                        action_type="create_reminder",
                        title=text.strip()[:80],
                        scheduled_at=scheduled_at.isoformat(),
                    )
                ],
            )
        return AssistantDecision(
            intent="answer_question",
            response=("Abhi main aapka message samajh nahi paya. Thodi der baad dobara bhejein."),
        )

    @staticmethod
    def _normalize_timeline(
        text: str,
        decision: AssistantDecision,
        user: User,
    ) -> AssistantDecision:
        interval = parse_natural_interval(text, user.timezone)
        if interval is None or not re.search(r"appointment|event|meeting|commitment", text, re.IGNORECASE):
            return decision
        starts_at, ends_at = interval
        existing = next(
            (action for action in decision.proposed_actions if action.action_type == "create_timeline_event"),
            None,
        )
        title = (
            existing.title
            if existing and existing.title
            else ("Doctor appointment" if re.search(r"doctor", text, re.IGNORECASE) else "Timeline event")
        )
        action = (existing or ProposedAction(action_type="create_timeline_event")).model_copy(
            update={
                "title": title,
                "starts_at": existing.starts_at if existing and existing.starts_at else starts_at.isoformat(),
                "ends_at": existing.ends_at if existing and existing.ends_at else ends_at.isoformat(),
                "event_category": existing.event_category
                if existing and existing.event_category
                else ("medical_appointment" if re.search(r"doctor", text, re.IGNORECASE) else "personal"),
            }
        )
        return decision.model_copy(
            update={
                "intent": "create_timeline_event",
                "response": decision.response or f"'{title}' timeline mein add kar doon?",
                "proposed_actions": [action],
                "missing_fields": [],
            }
        )

    @staticmethod
    def _memory_provenance_response(
        text: str,
        repository: Repository,
        user: User,
    ) -> str | None:
        if not re.search(
            r"kaise\s+pata|how\s+do\s+you\s+know|why\s+do\s+you\s+think|source|kab\s+bataya",
            text,
            re.IGNORECASE,
        ):
            return None
        memories = repository.list_memories(user.id)
        if not memories:
            return "Mere paas is baat ki koi stored source nahi hai."
        query_tokens = tokenize(text)
        ranked = sorted(
            ((score(query_tokens, tokenize(item.content)), item) for item in memories),
            key=lambda row: row[0],
            reverse=True,
        )
        best_score, memory = ranked[0]
        if best_score <= 0:
            # Nothing in the store actually matches the question. Citing the most
            # recent memory anyway would be a confident wrong answer, so hand the
            # turn back to the model instead.
            return None
        source = repository.session.get(Message, memory.source_message_id)
        if source is None:
            return "Yeh memory stored hai, lekin iska original source message nahi mila."
        original = source.text or source.transcript or "[media message]"
        date = source.created_at.strftime("%d %b %Y")
        return f"Aapne {date} ko kaha tha: “{original}”\n\nIsi source se maine yaad rakha: {memory.content}"
