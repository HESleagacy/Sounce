# Sounce

> Bas bol do. Baaki yaad rahega.

## Product

Sounce turns WhatsApp's **Message Yourself** chat into a multilingual personal
memory and timeline. People continue sending themselves thoughts, reminders, links,
documents, appointments, and personal facts. Sounce understands those messages,
asks when context is missing, remembers useful information with provenance, and performs
confirmed actions through deterministic application code.

The interface is conversation. There are no mandatory dashboards, forms, date pickers,
language selectors, or prompt conventions.

## Core Loop

```text
Text / document / link
          |
          v
Understand intent and context
          |
          v
Enough information? -- no --> Ask one natural question
          |
         yes
          |
          v
Remember or propose an action
          |
          v
Confirm consequential actions
          |
          v
Deterministic execution and delivery
```

## Text-First V1

- Owner-only WhatsApp self-chat through Neonize
- English, Hindi, and Hinglish conversation
- One-time and recurring reminders
- Reminder listing, cancellation, and rescheduling
- Persistent timeline events and deterministic overlap detection
- Conflict-aware suggestions after an existing event ends
- Personal facts and routines with original-message provenance
- Conversational language, modality, quiet-hour, and category reminder preferences
- PDF, image, and plain-text extraction
- Structured document types, dates, amounts, and entities
- Safe public-link ingestion and summarization
- Conservative proactive behavior limited to due reminders and scheduling conflicts

Voice input, Maya-powered voice output, and Google Calendar synchronization are required
V1 capabilities layered on top of this text-first contract.

## Reliability Principle

Gemini handles fuzzy human interpretation: language, intent, transcription, document
understanding, and clarification. Python owns authoritative timestamps, timezones,
confirmation state, database writes, recurrence, overlap detection, deduplication, and
delivery.

> The LLM understands the human. Deterministic code executes the contract.

## Product Hypothesis

Will people naturally use an intelligent WhatsApp self-chat if it understands what they
already put there and reliably remembers the useful parts?

The V1 demonstration proves four behaviors:

1. **Remember:** "Mera doctor Dr Sharma hai" is stored with its source.
2. **Remind:** a natural request is interpreted, confirmed, scheduled, and delivered.
3. **Configure:** preferences such as quiet hours change through conversation.
4. **Understand context:** an unexplained document is summarized, then the assistant asks
   what the user wants instead of executing embedded instructions.

## Deliberate Limits

V1 is a single-instance Python application using SQLite. It is not a medical decision
system, general autonomous agent, native mobile application, real-time voice system, or
replacement for WhatsApp. Neonize is a replaceable V1 transport rather than the product.
