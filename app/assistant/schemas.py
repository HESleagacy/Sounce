from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Intent = Literal[
    "create_reminder",
    "cancel_reminder",
    "reschedule_reminder",
    "create_timeline_event",
    "forget_memory",
    "store_memory",
    "update_preference",
    "purge_all_data",
    "answer_question",
    "document_received",
    "clarify",
    "confirm_action",
]
ActionType = Literal[
    "create_reminder",
    "store_memory",
    "update_preference",
    "create_timeline_event",
    "cancel_reminder",
    "reschedule_reminder",
    "forget_memory",
    "purge_all_data",
]


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    content: str | None = None
    kind: str | None = None
    category: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    key: str | None = None
    value: str | None = None
    title: str | None = None
    scheduled_at: str | None = Field(
        default=None,
        description="ISO 8601 datetime with offset for create_reminder",
    )
    starts_at: str | None = Field(
        default=None,
        description="ISO 8601 start datetime for create_timeline_event",
    )
    ends_at: str | None = Field(
        default=None,
        description="ISO 8601 end datetime for create_timeline_event",
    )
    reminder_id: int | None = Field(
        default=None,
        description="Existing reminder id for cancel_reminder",
    )
    memory_id: int | None = Field(
        default=None,
        description="Existing memory id for forget_memory",
    )
    recurrence_frequency: Literal["daily", "weekly", "monthly"] | None = None
    recurrence_interval: int = Field(default=1, ge=1, le=365)
    event_category: str | None = None


class AssistantDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent
    response: str = Field(min_length=1)
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    transcript: str | None = Field(
        default=None,
        description="Verbatim transcript when the input is audio",
    )
    document_summary: str | None = Field(
        default=None,
        description="Short summary when the input is a document or image",
    )
    document_extracted_text: str | None = Field(
        default=None,
        description="Extracted text or OCR content when the input is a document or image",
    )
    document_type: str | None = None
    document_dates: list[str] = Field(default_factory=list)
    document_amounts: list[str] = Field(default_factory=list)
    document_entities: list[str] = Field(default_factory=list)


class AudioDecision(AssistantDecision):
    transcript: str = Field(
        min_length=1,
        description="Verbatim transcript of the complete voice note",
    )
    detected_languages: list[str] = Field(
        min_length=1,
        description="BCP-47 language codes detected in the voice note, including mixed languages",
    )


class LocalizedResponse(BaseModel):
    response: str = Field(min_length=1)
