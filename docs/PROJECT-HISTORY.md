# Sounce

> Bas bol do. Baaki yaad rahega.

## Current Implementation Status

The text-first product through reminders, timeline context, persistent memory, documents,
links, conversational preferences, and multilingual responses is implemented. It includes
one-time and recurring reminders, rescheduling, provenance-aware context, structured
document extraction, and conflict-aware alternative scheduling. Voice and optional provider
code remain available but are validated separately from the text-first contract.

## Implementation Plan

Sounce will replace the existing PBCTF bot functionality. The project will remain a single deployable Python application, while keeping WhatsApp, AI, persistence, and scheduling behind clear internal boundaries.

The first milestone will implement one complete interaction:

```text
WhatsApp self-chat text
        |
        v
Message normalization and deduplication
        |
        v
Gemini structured interpretation
        |
        v
Confirmation or clarification
        |
        v
Deterministic database operation
        |
        v
WhatsApp response
```

Example:

```text
User: Kal 5 baje Ramesh ko call karna.

Gemini interpretation:
{
  "intent": "create_reminder",
  "action": "Call Ramesh",
  "scheduled_at": "2026-08-09T17:00:00+05:30",
  "requires_confirmation": true
}

Sounce:
Kal shaam 5 baje Ramesh ko call karne ka reminder laga doon?

User: Haan

Application code:
Creates the reminder and schedules delivery.
```

Gemini interprets the human request. Python validates and executes it.

## Target Structure

```text
sounce/
|-- app/
|   |-- main.py
|   |-- config.py
|   |-- transport/
|   |   |-- base.py
|   |   |-- neonize_adapter.py
|   |   `-- normalizer.py
|   |-- assistant/
|   |   |-- service.py
|   |   |-- prompts.py
|   |   |-- schemas.py
|   |   |-- context.py
|   |   `-- decision_engine.py
|   |-- domain/
|   |   |-- messages.py
|   |   |-- memories.py
|   |   |-- reminders.py
|   |   |-- preferences.py
|   |   |-- timeline.py
|   |   `-- documents.py
|   |-- persistence/
|   |   |-- database.py
|   |   |-- models.py
|   |   `-- repositories.py
|   |-- providers/
|   |   |-- gemini.py
|   |   |-- maya.py
|   |   `-- google_calendar.py
|   `-- workers/
|       |-- message_worker.py
|       `-- reminder_worker.py
|-- migrations/
|-- tests/
|-- data/
|-- .env.example
|-- Dockerfile
|-- pyproject.toml
`-- README.md
```

## Technical Choices

- Python monolith: one application and one deployment unit.
- Neonize: V1 WhatsApp transport, isolated behind an adapter.
- Gemini: multilingual and multimodal interpretation with structured output.
- SQLite: V1 application database for a single running instance.
- SQLAlchemy and Alembic: persistence and schema migrations.
- Pydantic: validation for configuration and Gemini decisions.
- Database-backed workers: message processing and restart-safe reminder delivery.
- Maya: optional text-to-speech provider with text fallback.
- Google Calendar: optional provider added after core reminders are reliable.

## Core Database Model

### Users

```text
id
whatsapp_jid
timezone
created_at
```

Although V1 is initially single-user, every record should be scoped to an owner.

### Messages

```text
id
whatsapp_message_id
user_id
direction
message_type
text
transcript
media_path
created_at
```

The WhatsApp message ID must be unique to prevent duplicate processing.

### Memories

```text
id
user_id
kind
category
content
source_message_id
status
confidence
created_at
updated_at
```

Example:

```text
kind: personal_fact
category: health
content: User reports a penicillin allergy
```

### Reminders

```text
id
user_id
title
due_at
timezone
status
source_message_id
delivered_at
created_at
```

### Timeline Events

```text
id
user_id
title
starts_at
ends_at
source_message_id
status
```

Timeline events enable deterministic overlap detection.

### Preferences

```text
id
user_id
key
value
source_message_id
updated_at
```

Examples:

```text
preferred_language = hi
response_modality = text
quiet_hours_start = 22:00
```

### Pending Actions

```text
id
user_id
action_type
payload
source_message_id
status
expires_at
```

Pending actions preserve conversational state:

```text
User: Kal reminder lagana.
Assistant: Kis waqt?
User: Shaam 5 baje.
Assistant: Reminder laga doon?
User: Haan.
```

### Documents

```text
id
user_id
filename
mime_type
storage_path
summary
extracted_text
source_message_id
created_at
```

## Message Processing Flow

```text
Neonize event
    |
    v
Self-chat and owner validation
    |
    v
Ignore known outbound messages
    |
    v
Deduplicate by WhatsApp message ID
    |
    v
Convert to an internal InboundMessage
    |
    v
Store the original message
    |
    v
Transcribe audio or extract a document when needed
    |
    v
Load relevant preferences, timeline, and memories
    |
    v
Ask Gemini for a structured interpretation
    |
    v
Validate the interpretation with Pydantic
    |
    v
Apply deterministic rules
    |
    v
Execute, request confirmation, or clarify
    |
    v
Send a response through the transport adapter
```

The Neonize callback should put messages onto an internal queue. It should not run Gemini, transcription, or document processing directly.

## Self-Chat Safety

Self-chat is the first technical feasibility check. Neonize behavior must be verified for:

- self-chat JIDs
- `IsFromMe` values
- inbound and outbound message IDs
- text, document, image, and voice-note wrappers
- quoted messages
- reconnect and replay behavior

The transport must maintain message-ID deduplication and identify known outbound messages so Sounce never responds to its own responses.

Only the configured owner JID should be accepted in V1.

## Gemini Contract

Gemini returns typed decisions rather than unrestricted application instructions.

```python
class AssistantDecision(BaseModel):
    intent: Literal[
        "create_reminder",
        "store_memory",
        "update_preference",
        "answer_question",
        "document_received",
        "clarify",
        "confirm_action",
    ]
    response: str
    proposed_actions: list[ProposedAction]
    missing_fields: list[str]
```

Initially supported actions:

- `create_reminder`
- `store_memory`
- `update_preference`
- `create_timeline_event`
- `cancel_reminder`
- `forget_memory`

Gemini must never directly:

- write to the database
- schedule jobs
- send WhatsApp messages
- calculate authoritative timestamps
- mark reminders as delivered
- mutate Google Calendar

## Confirmation Policy

### Confirm Before Execution

- Creating or changing reminders
- Cancelling reminders
- Creating or changing Calendar events
- Ambiguous or consequential operations

### Execute Directly When Explicit

- Language preference changes
- Response modality changes
- Quiet-hour changes
- Informational questions

### Store Personal Facts Conservatively

Clear personal facts can be stored with their original message as provenance.

```text
User: Mujhe penicillin se allergy hai.

Assistant:
Theek hai, main yaad rakhunga ki aapne bataya hai ki aapko penicillin se allergy hai.
```

The stored fact and response should indicate that the user reported it rather than presenting it as medically verified.

## Reminder Worker

A database-backed polling worker is sufficient for V1:

```text
Every 10 seconds
    |
    v
Find pending reminders where due_at <= now
    |
    v
Atomically mark the reminder as delivering
    |
    v
Apply quiet-hours policy
    |
    v
Send the WhatsApp message
    |
    v
Mark the reminder as delivered
```

The database, not an in-memory timer, owns reminder state. This makes delivery recoverable after an application restart.

During quiet hours, the worker moves delivery to the next permitted time unless the user explicitly requests an exception.

## Conflict Detection

Time conflicts are calculated with deterministic Python logic:

```python
new_start < existing_end and new_end > existing_start
```

Example:

```text
New reminder: tomorrow at 5:00 PM
Existing event: 4:30 PM to 5:30 PM

Sounce:
Kal 5 baje aapki doctor appointment bhi hai. Ramesh ko call karne ka reminder appointment ke baad rakh doon?
```

Gemini can produce the natural-language response, but application code must calculate the overlap.

## Implementation Phases

### Phase 1: Foundation

- Remove the existing PBCTF features and card assets.
- Create the new package structure.
- Add typed configuration.
- Add SQLite, SQLAlchemy, and migrations.
- Isolate Neonize behind a transport interface.
- Verify self-chat behavior before implementing AI processing.
- Add owner allowlisting, outbound-message protection, and deduplication.
- Add tests for message normalization and routing.

### Phase 2: Text, Memory, and Preferences

- Integrate Gemini.
- Add the structured decision schema.
- Store messages and source-linked memories.
- Implement conversational preferences.
- Support clarification and pending actions.
- Match the user's language and conversational style.

### Phase 3: Reminders and Timeline

- Interpret dates relative to the user's timezone.
- Add confirmation workflows.
- Implement the persistent reminder worker.
- Support listing, cancelling, and rescheduling reminders.
- Apply quiet hours.
- Add basic overlap detection.

### Phase 4: Voice Notes

- Download WhatsApp audio.
- Send audio to Gemini for transcription and interpretation.
- Store the transcript with the original message provenance.
- Keep responses text-first.

### Phase 5: Documents

Initial formats:

- PDF
- common image formats
- plain text

Capabilities:

- bounded and validated downloads
- text extraction and OCR
- document summaries
- date, amount, and entity extraction
- clarification before creating reminders
- retrieval with source references

### Phase 6: Optional Integrations

- Add Maya TTS through a provider interface with text fallback.
- Encode generated audio as a WhatsApp-compatible voice note.
- Add Google Calendar OAuth.
- Add Calendar synchronization only after reminders are reliable.

### Phase 7: Production Hardening

- Structured logging
- Retry and timeout policies
- Health endpoint
- Database backups
- Privacy controls
- Forget and export commands
- Pinned dependencies
- Continuous integration
- Automated unit and integration tests

## First Deliverable

The first release should contain:

1. Clean replacement of the current application.
2. Neonize self-chat adapter.
3. SQLite message persistence.
4. Gemini text understanding.
5. Memory storage with source references.
6. Conversational preferences.
7. One-time reminders with confirmation.
8. Restart-safe reminder delivery.
9. Hinglish, Hindi, and English responses.
10. Tests for deduplication, confirmation, and scheduling.

Voice, documents, Maya, and Google Calendar follow after this text-based loop works reliably.

## V1 Success Criteria

The initial product hypothesis is validated when these interactions work naturally and reliably:

### Remember

```text
Mera doctor Dr Sharma hai.
```

The fact is stored with a reference to its source message.

### Remind

```text
Kal 5 baje Ramesh ko call karna.
```

The request is interpreted, confirmed, scheduled, and delivered at the correct time.

### Configure

```text
Raat 10 ke baad reminders mat bhejna.
```

The preference is changed through conversation.

### Understand Context

The user uploads a document without instructions. Sounce understands what it can, describes it briefly, and asks what the user wants instead of guessing.

## Guiding Principle

> The LLM understands the human. Deterministic code executes the contract.
