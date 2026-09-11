# Sounce

> Bas bol do. Baaki yaad rahega.
> *(Just say it. The rest will be remembered.)*

A private, multilingual WhatsApp **self-chat assistant**. It turns WhatsApp's
"Message Yourself" chat into a personal memory, reminder, and timeline manager.
Speak English, Hindi, Hinglish, or mix them freely.

**The design idea:** Gemini interprets the human. Deterministic Python owns the
contract — every timestamp, database write, confirmation, conflict check, and
delivery. The model never mutates state; it returns a typed decision that
application code validates before anything happens.

---

## What it does

| Capability | Example |
|---|---|
| Remember facts, with provenance | *"Mera doctor Dr Sharma hai"* → later: *"Tumhe kaise pata?"* answers with the original message and its date |
| Reminders, one-off and recurring | *"Har Sunday subah 10 baje medicines order karna"* |
| Timeline events with conflict detection | Overlaps are flagged with an alternative time |
| Preferences | *"Raat 10 ke baad reminders mat bhejna"* (quiet hours) |
| Documents and links | Send a PDF or paste a URL → summary with dates, amounts, entities |
| Voice notes | Transcribed, interpreted, and answered in the same language |
| Google Calendar sync | Confirmed reminders and events, through a durable outbox |
| Export and erase | `sounce export`, `sounce purge`, or just ask it in chat |

Consequential actions are always confirmed before they happen.

## Reliability, concretely

This is a personal assistant, so "mostly delivers" is not a feature. The
guarantees, and the tests that hold them:

| Property | Mechanism | Test |
|---|---|---|
| No accepted message is ever dropped | Durable inbox committed before the in-memory queue | `test_inbox.py` |
| Crashed work is picked back up | Lease expiry + recovery sweep | `test_inbox.py` |
| A delivered reminder is never re-delivered | Unique `(reminder_id, occurrence_key)` ledger | `test_reminder_delivery.py` |
| A crash mid-send costs ≤1 duplicate | Claim committed before the send | `test_reminder_delivery.py` |
| Calendar outages never diverge local state | Transactional outbox with backoff | `test_scheduling.py` |
| Links can't reach the private network | Connection-level address pinning | `test_web_safety.py` |
| Schema matches the models | Migration drift check | `test_migrations.py` |
| Every message reaches the model | Regression guard against demo hardcoding | `test_message_routing.py` |

150 tests, 79% coverage, `ruff` and `mypy` clean.

```bash
pytest --cov=app
ruff check . && ruff format --check . && mypy
```

## Quick start

```bash
cp .env.example .env     # set OWNER_JID and GEMINI_API_KEY
docker compose up --build
```

Scan the QR code with **WhatsApp → Settings → Linked Devices → Link a Device**.
You pair once; the session persists in `data/`.

Full setup, health endpoints, backup, and recovery runbooks:
**[docs/OPERATIONS.md](docs/OPERATIONS.md)**

## Documentation

| Document | What's in it |
|---|---|
| [Architecture](docs/ARCHITECTURE.md) | The LLM/deterministic boundary, delivery semantics, the inbox and outbox, retrieval |
| [Threat model](docs/THREAT-MODEL.md) | Controls, and an honest list of known gaps |
| [Operations](docs/OPERATIONS.md) | Running it, health checks, backup/restore, incident runbooks |
| [Product](docs/PRODUCT.md) | What it is for and where the limits are |
| [Demo](docs/DEMO.md) | Walkthrough script |

## Stack

Python 3.10+ · [Neonize](https://github.com/krypton-byte/neonize) (WhatsApp) ·
Gemini 2.5 Flash · SQLAlchemy 2 + Alembic · Pydantic v2 · Maya TTS (optional) ·
Google Calendar (optional) · Docker

## Status

V1, single-owner, running in production for its author. Not multi-tenant and
not intended to be.

## License

[GNU General Public License v3.0](LICENSE)
