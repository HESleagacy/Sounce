# Operations

## Running it

```bash
cp .env.example .env          # set OWNER_JID and GEMINI_API_KEY at minimum
docker compose up --build
```

On first run a QR code appears. Scan it with **WhatsApp → Settings → Linked
Devices → Link a Device**. The session persists in `data/neonize.db`; you pair
once.

Migrations run automatically at startup (`alembic upgrade head`).

### Without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m app.main
```

## Health endpoints

Set `PORT` to enable the health server (Railway and most PaaS do this for you).

| Endpoint | Meaning | On failure |
|---|---|---|
| `GET /live` | The process is running | Restart the container |
| `GET /ready` | It can do its job right now | Take out of rotation; do **not** restart |

`/ready` returns `200` or `503` with a JSON body checking: database
reachability, migrations at head, all workers alive, durable backlog under
threshold, WhatsApp connected, and the decision provider configured.

```bash
curl -s localhost:8080/ready | jq
```

A `503` from `/ready` with `whatsapp: disconnected` is normal during a
reconnect and resolves itself. A `503` with `migrations: behind` means the
image is newer than the database and needs `alembic upgrade head`.

From the CLI, without a running server:

```bash
sounce health        # prints the same report; exits 1 when not ready
```

## Data lifecycle commands

```bash
sounce export                     # everything, to data/exports/ (mode 0600)
sounce export --out ~/backup.json
sounce retention                  # apply RETENTION_DAYS now
sounce retention --days 90        # override for this run
sounce purge --yes                # erase everything, irreversibly
```

The owner can also erase from the chat itself — "sab kuch delete kar do" — which
goes through the same confirmation gate as any other consequential action.

## Backup and restore

The entire state is the `data/` directory:

```
data/
  sounce.db                    # everything the assistant knows
  neonize.db                   # WhatsApp session — treat as a credential
  media/                       # voice notes and documents
  google_calendar_token.json   # refresh token, mode 0600
```

```bash
# Back up (stop the container first for a consistent snapshot)
docker compose stop
tar czf sounce-backup-$(date +%F).tar.gz data/
docker compose start
```

Restore by stopping, replacing `data/`, and starting. Migrations bring an older
database forward automatically. Encrypt the archive — it contains everything.

For a database-only snapshot without stopping:

```bash
sqlite3 data/sounce.db ".backup data/sounce-backup.db"
```

## What to do when things go wrong

### The backlog is growing
Check `/ready` → `queue.durable_backlog`. Messages are not lost; they are
queued in `inbound_jobs`. If the backlog is not draining, the message worker is
likely stuck on a provider call — check logs for repeated provider errors.

### Messages were dead-lettered
```sql
SELECT id, whatsapp_message_id, attempts, last_error FROM inbound_jobs WHERE status = 'dead';
```
The owner already received a failure notice for each. To retry one after fixing
the cause:
```sql
UPDATE inbound_jobs SET status='queued', attempts=0, available_at=CURRENT_TIMESTAMP WHERE id = ?;
```

### A reminder did not arrive
```sql
SELECT r.id, r.title, r.status, r.delivery_attempts, r.last_error,
       d.occurrence_key, d.status
FROM reminders r LEFT JOIN reminder_deliveries d ON d.reminder_id = r.id
WHERE r.status IN ('pending','delivering','undeliverable');
```
`undeliverable` means the attempt budget ran out. Fix the transport, then:
```sql
UPDATE reminders SET status='pending', delivery_attempts=0 WHERE id = ?;
```
A reminder stuck in `delivering` is reclaimed automatically once its lease
expires — no manual action needed.

### Calendar is out of sync
```sql
SELECT id, op, entity_type, entity_id, status, attempts, last_error FROM calendar_ops WHERE status != 'synced';
```
Local state is authoritative and correct regardless. To retry failed ops after
fixing credentials:
```sql
UPDATE calendar_ops SET status='pending', attempts=0, available_at=CURRENT_TIMESTAMP WHERE status='failed';
```

### Recovering from a crash
Nothing is required. On startup the message worker reclaims expired leases and
re-queues unfinished inbox jobs, and the reminder worker reclaims expired
delivery leases. See [ARCHITECTURE.md](ARCHITECTURE.md) for the exact
guarantees.

## Configuration

Every setting is an environment variable; see `.env.example` for the full list
with defaults. The ones worth knowing:

| Variable | Default | Purpose |
|---|---|---|
| `OWNER_JID` | *(required)* | The only sender the assistant accepts |
| `GEMINI_API_KEY` | *(required)* | Decision provider |
| `RETENTION_DAYS` | `365` | Raw-material expiry; `0` keeps everything |
| `MESSAGE_QUEUE_SIZE` | `100` | In-memory queue; overflow spills to the durable inbox |
| `MESSAGE_MAX_ATTEMPTS` | `3` | Before an inbox job is dead-lettered |
| `REMINDER_LEASE_SECONDS` | `120` | How long before a stuck delivery is reclaimed |
| `REMINDER_MAX_ATTEMPTS` | `5` | Before a reminder is marked undeliverable |
| `CALENDAR_MAX_ATTEMPTS` | `6` | Before a calendar op is marked failed |
| `HEALTH_BACKLOG_THRESHOLD` | `500` | Backlog size that fails `/ready` |

## Development

```bash
.venv/bin/pip install -e '.[dev]'
pre-commit install

pytest                        # 150 tests
pytest --cov=app              # coverage, floor at 70%
ruff check . && ruff format --check .
mypy
```

Regenerate the dependency lock after changing `pyproject.toml`:

```bash
pip-compile --generate-hashes --strip-extras --output-file=requirements.lock pyproject.toml
```
