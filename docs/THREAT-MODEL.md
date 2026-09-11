# Threat model

Sounce stores the most personal thing on a person's phone: the notes they write
to themselves, transcripts of their voice, and the documents they forward. It
also acts on their behalf. This is what it defends against, and what it does
not.

## Assets

| Asset | Where it lives |
|---|---|
| Message text and voice transcripts | `messages` table |
| Derived memories with provenance | `memories` table |
| Media and documents | `data/media/`, `documents` table |
| WhatsApp session | `data/neonize.db` |
| Gemini API key, Maya key, Google refresh token | environment / `data/google_calendar_token.json` |

## Trust boundaries

**Trusted:** the owner's own self-chat, and the host the app runs on.

**Untrusted:** everything that arrives as content — message bodies, document
contents, transcribed audio, fetched web pages — and every provider response.

## Controls

### Only the owner can drive it
Every inbound message is checked against `OWNER_JID` *and* `is_self_chat` before
it is persisted (`MessageWorker._is_owner_message`), and again in `process`.
Messages from anyone else never enter the inbox.

### Content cannot become instructions
A document with no caption is summarised and handed back as a question; its
`proposed_actions` are discarded. Fetched web pages are stripped to text and
treated the same way. Neither can cause an action on its own — the owner has to
ask for one in their own words, and consequential actions still require
confirmation.

This is mitigation, not elimination. A sufficiently convincing prompt injection
inside a document could still influence how the model *phrases* a proposal. What
it cannot do is execute one: the confirmation gate and `DecisionEngine`
validation are deterministic and sit outside the model's reach.

### Links cannot reach the private network
`SafeWebFetcher` defends against SSRF at the point of connection, not before it:

* Scheme restricted to `http`/`https`; embedded credentials rejected.
* Ports restricted to 80 and 443.
* Hostnames must look like DNS names, which rejects decimal, octal and hex
  encodings of IP literals (`2130706433`, `0177.0.0.1`, `0x7f.0.0.1`).
* `_PinnedBackend` overrides httpcore's `connect_tcp`, resolves the host itself,
  rejects any non-global address, and dials the validated literal address. This
  is what closes DNS rebinding: validating with `getaddrinfo` and then letting
  the client resolve again is a time-of-check/time-of-use hole.
* TLS still verifies the original hostname, because httpcore performs
  `start_tls` with the name from the URL.
* Redirects are capped at 4 and every hop is re-validated.
* Responses are capped by bytes *and* by extracted characters, and limited to
  `text/html` and `text/plain`.

If the connection guard cannot be installed, `_build_client` raises rather than
falling back to an unprotected client. Failing closed matters more than
availability here. `tests/test_web_safety.py` covers each case.

### Confirmation before consequence
`create_reminder`, `create_timeline_event`, `cancel_reminder`,
`reschedule_reminder`, `forget_memory` and `purge_all_data` are all held as a
pending action with a TTL and a stage, in SQLite, and executed only after an
explicit yes. Only `store_memory` and `update_preference` execute directly, and
only the model can propose them — never a regex.

### Data lifecycle
* `sounce export` writes everything to a `0600` JSON file.
* `sounce purge --yes`, or asking in chat, erases every row and every media
  file. The in-chat path runs *after* the confirmation reply is delivered,
  because the purge removes the rows that reply was written against.
* `RETENTION_DAYS` (default 365) expires raw messages, transcripts, media and
  settled jobs. Memories, reminders, preferences and timeline events are kept —
  they are the product. Messages cited as the provenance of something still in
  use are never expired, so "how do you know that?" keeps working.
* Media deletion refuses any path that resolves outside `MEDIA_DIR`, because a
  stored path is data and data is untrusted.

## Known gaps

These are real and currently unaddressed. They are listed rather than hidden.

| Gap | Impact | Why it is open |
|---|---|---|
| **No encryption at rest** | Anyone with the disk or a host backup reads everything | Needs a key-management story the single-owner deployment does not yet have. Use full-disk or volume encryption on the host. |
| **Secrets in environment variables** | A process dump or misconfigured log could expose them | `SecretStr` keeps them out of reprs and logs; a real secret manager is the next step |
| **No rate limiting on the model** | A flood of messages is a cost problem, not a safety one | The bounded queue and owner-only filter cap it in practice |
| **Prompt injection can shape phrasing** | Misleading proposal text | Cannot be eliminated; confirmation and validation contain the blast radius |
| **WhatsApp session file is a credential** | Stealing `data/neonize.db` grants access to the linked device | Protect the volume; revoke from WhatsApp's Linked Devices on suspicion |
| **Single owner assumed throughout** | Not safe for multi-tenant use as written | Out of scope by design |

## If something is compromised

1. Unlink the device: **WhatsApp → Settings → Linked Devices → Log out**.
2. Rotate `GEMINI_API_KEY`, `MAYA_API_KEY`, and the Google OAuth client.
3. Revoke the Calendar grant at <https://myaccount.google.com/permissions>.
4. `sounce export` for a record, then `sounce purge --yes`.
5. Delete `data/neonize.db` and re-pair.
