# Architecture

## The one idea

Gemini interprets the human. Deterministic Python owns the contract.

The model decides *what the user meant*. It never writes to the database, never
computes a timestamp, never decides whether an action is safe to execute. It
returns a typed `AssistantDecision`, and `DecisionEngine` validates every field
of it against real state — ownership, pending-action stage, parseable times,
future-dated schedules, timeline conflicts — before anything is persisted.

That boundary is the project. Everything below is what it takes to make the
deterministic half actually trustworthy.

```
WhatsApp (Neonize)
      |
      v
 Normalizer ──► inbound_jobs (durable inbox)
                     |
                     v
              Message Worker ──► Assistant Service ──► Gemini
                     |                  |
                     |                  v
                     |           Decision Engine  (validates, confirms, writes)
                     v                  |
                  SQLite  ◄─────────────┤
                     |                  └──► calendar_ops (outbox) ──► Calendar Worker ──► Google
                     v
            Reminder Worker ──► reminder_deliveries (ledger) ──► WhatsApp reply
```

## The deterministic-parsing policy

Regex helpers exist in `app/assistant/service.py`. They are allowed to do two
things and nothing else:

1. **Fill gaps.** Complete fields the model left empty on an action it already
   proposed, or rescue an unmistakably scheduling-shaped message the model
   failed to act on. Everything they touch is a confirm-first action, so the
   user still approves the result.
2. **Stand in for an unavailable provider.** `_fallback_decision` runs only when
   the provider raises.

They may **never** write a memory or a preference. Those execute directly,
without confirmation, and a regex is not strong enough evidence to mutate state
the user will later rely on.

This policy exists because an earlier version violated it. `_canonical_text_decision`
short-circuited Gemini entirely with a table of Hinglish regexes tuned to the
demo script, and wrote preferences and memories from those matches. The demo
passed while the assistant was not interpreting anything.
`tests/test_message_routing.py::test_every_text_message_reaches_the_model` is
the regression guard.

## Durable inbox

`MessageWorker.enqueue` commits the message to `inbound_jobs` **before** handing
it to the in-memory queue. The queue is a latency optimisation, not the system
of record.

| Failure | Old behaviour | Now |
|---|---|---|
| Queue full | Logged an error, dropped the message | Job is committed; the recovery sweep picks it up |
| Process crash mid-processing | Message lost | Lease expires, job returns to `queued` |
| Repeated processing failure | Retried forever or lost | Backoff, then dead-lettered, and the owner is told |

Recovery runs at startup and after roughly ten idle seconds.

## Reminder delivery

Sending a WhatsApp message is an external side effect that cannot join the
database transaction, so exactly-once is not available. The honest guarantees:

* An occurrence whose delivery **committed** is never sent again. The unique
  `(reminder_id, occurrence_key)` row in `reminder_deliveries` is the guard, and
  `occurrence_key` is the UTC due time, so each firing of a recurring reminder
  is its own occurrence.
* A crash in the window between the send and its commit costs **at most one**
  duplicate, capped by the attempt budget.
* A crash anywhere else costs nothing: the lease expires, the reminder returns
  to `pending`, and it is retried.

The ambiguous window resolves towards resending, because for this product a
missed reminder is worse than a repeated one. `tests/test_reminder_delivery.py`
exercises each of these paths, including a simulated crash between send and
commit.

The claim is committed in its own transaction *before* the send. An earlier
version put the claim, the send, and the result in one transaction, which meant
the `delivering` state was never durable and a crash silently rolled back to
`pending` — producing duplicates with no record that anything had happened.

## Calendar outbox

The decision engine never calls Google inline. It writes a `calendar_ops` row in
the same transaction as the local change, so the two cannot diverge: either both
land or neither does. `CalendarWorker` drains ops in insertion order with
exponential backoff, writes the remote event id back to the entity, and tracks
`calendar_sync_status` (`pending` / `synced` / `failed`).

Update and delete read the event id from the entity at drain time rather than
from the op payload. That is what lets a reschedule be queued while its create
is still in flight.

Calendar being down never blocks a reminder: the user is told the reminder
exists, and the sync catches up later.

## Context retrieval

Loading the 30 most recent memories is fine at 30 memories and useless at 3,000
— the relevant fact gets pushed out by recency. `app/domain/retrieval.py` scores
candidates by lexical overlap with the incoming message, weighted by token
length and normalised by candidate length, with a few recent items always pinned
so follow-up turns stay coherent.

This is deliberately not embeddings. Lexical retrieval is debuggable, free,
needs no extra service, and for one person's own phrasing it is a strong
baseline. Replace it when a measurement says it is the thing failing — not
because the word "memory" appeared.

The assembled context is hard-capped at `CONTEXT_CHAR_BUDGET`. When it does not
fit, documents are sacrificed before memories, and memories before recent
messages, because a dropped memory costs recall while a dropped turn costs
coherence on the very next reply.

## Where the boundaries are

| Module | Owns |
|---|---|
| `transport/` | WhatsApp I/O and normalisation into `InboundMessage` |
| `assistant/` | Prompting, the decision schema, validation, confirmation |
| `domain/` | Pure logic: time parsing, recurrence, overlap, retrieval |
| `persistence/` | Models, migrations, repositories |
| `providers/` | Gemini, Maya TTS, Google Calendar, safe web fetching |
| `workers/` | The four background loops |
| `privacy.py` | Export, erase, retention |

`MessageTransport`, `DecisionProvider`, `CalendarSync` and `TTSProvider` are
Protocols, so each external service is replaceable without turning a
single-user assistant into twelve services for the sake of a diagram.
