---
name: data-integrity-review
aliases:
  - data-contract-state-integrity-engine
version: 4.0.0
description: >-
  Use when a change touches database schemas, migrations, serialized formats
  (API payloads, messages, on-disk files), transactions or entity state machines,
  and the question is whether data stays correct across deploys, retries,
  concurrent writers and crashes. Produces a schema evolution matrix, a migration
  safety table, an invariant enforcement map and EARS data-contract requirements.
  Not for general code wiring (use wiring-audit), attacking a
  whole implementation plan (use plan-review), or agent and
  tool orchestration state (use agent-workflow-review).
brief: |
  Mission: prove data stays correct through migrations, rolling deploys, retries, concurrent writers and crashes, on the engine and isolation level actually in use.
  - First pin the engine and version, the configured isolation level, and the driver's transaction mode; lock behaviour and anomalies depend on all three.
  - Migrations: expand/contract for anything old code still reads, batched backfills with short transactions, the lock each DDL statement takes on this engine, a lock timeout, and a rollback that still works after new-shaped data exists.
  - Compatibility: old readers with new writers and the reverse, for rolling deploys and for persisted data; enum additions, renames, removed fields, changed defaults.
  - Concurrency: lost update, write skew and phantoms judged against the configured isolation; idempotency enforced by a unique constraint in the same transaction as the effect; exactly-once means at-least-once plus idempotent effects.
  - Invariants: which live in the database (NOT NULL, CHECK, UNIQUE, FK) and which only in code; state transitions as compare-and-set with terminal states and recovery for stuck states.
  - Values: time zones and precision, money as decimal or integer minor units, encoding, absent versus null; caches coherent with the source of truth.
  Output before findings: data surface inventory, schema evolution matrix, migration safety table, invariant enforcement map, EARS requirements (REQ-DATA-NNN).
  PASS: no path loses or corrupts data. PASS_WITH_FIXES: local fixes (constraint, batch size, guard). BLOCK: a migration or deploy order can lose data, lock a busy table unboundedly, or break deployed readers.
activation_triggers:
  task_modes:
    - DATA_CONTRACT_AUDIT
    - SCHEMA_MIGRATION_VERIFICATION
    - STATE_MACHINE_AUDIT
  keywords:
    - schema migration
    - expand contract
    - backfill
    - isolation level
    - write skew
    - lost update
    - idempotency key
    - exactly-once
    - wire compatibility
    - ddl lock
    - state transition
    - money precision
  do_not_use_when:
    - The concern is whether code is reachable and wired, not data correctness (route to wiring-audit).
    - The concern is agent loops, tool retries or multi-agent handoffs (route to agent-workflow-review).
    - The data change is one step of a broader plan that needs end-to-end attack (route to plan-review first).
input_contract:
  requires_worktree: true
  required_inputs:
    - The schema, migration, serializer or state-machine change (diff, files, or design)
  optional_inputs:
    - Database engine and version, isolation level, deployment model (rolling, blue-green, single binary)
    - Table sizes and write rates for affected tables
    - Consumers of the affected formats outside this repository
output_contract:
  sections:
    - Data surface inventory
    - Schema evolution matrix
    - Migration safety table
    - Invariant enforcement map
    - Data-contract requirements (EARS)
  findings: shared format
  verdict: shared verdict block
---

# Data contract and state integrity

## Mission

The consumer is about to ship a change to stored or exchanged data and needs to
know which sequence of deploys, retries or crashes corrupts data or takes the
system down, judged against the real engine, version and configured isolation. The common failure is
engine-agnostic advice: PostgreSQL lock rules applied to SQLite, or "use a
transaction" where the default isolation does not prevent the anomaly.

## Inputs to establish first

- Engine and version from config, compose files, drivers or lockfiles
  (`SELECT version()`, `sqlite3.sqlite_version`); it decides which DDL is online.
- Isolation level and transaction mode: database default, session overrides, ORM
  settings (Django `ATOMIC_REQUESTS`, Python `sqlite3` implicit transactions).
- Deployment model: can old and new code run at once (rolling deploy, several
  desktop instances, queue workers)? Then every change needs both-direction
  compatibility.
- Table sizes and write rates: rewriting 200 rows is harmless, 50 million is an
  outage. If unknown, state the threshold at which a finding becomes real.
- External consumers of changed formats that this repository cannot see.

## Method

1. **Inventory.** List every data surface touched (tables, indexes, serialized
   types, files, messages, caches) with writers and readers. Done when each has a
   named source of truth.
2. **Diff the contract.** For each surface compare before and after field by
   field: type, nullability, default, constraints, enum members, meaning. Check
   migration history: a migration edited after it was applied (`git log --follow`
   on the file) means environments silently diverge. Done when every delta is in
   the evolution matrix.
3. **Simulate the deploy.** Migration runs while old code serves; new code starts
   beside old instances; a rollback puts old code over new data. Done when each
   delta has a verdict for both directions.
4. **Analyse migrations.** Per statement: lock on this engine, rewrite or scan,
   inside a transaction or not, what queues behind it. Done when the safety table
   is complete.
5. **Model concurrency and crashes.** Interleave two executions of each write
   path at the configured isolation, and crash between each pair of writes. Done
   when each path is safe or a concrete interleaving breaks an invariant.
6. **Map invariants.** For each rule, find where it is enforced and whether that
   survives concurrency. Done when every code-only invariant has a race analysis.
7. **Specify.** Write EARS requirements for each accepted fix. Done when every
   P0-P2 finding maps to a REQ-DATA requirement.

## Checklist

### Online migrations (PostgreSQL)
- `ADD COLUMN` with a constant default is metadata-only from version 11; a volatile
  default (`gen_random_uuid()`, `clock_timestamp()`) rewrites the table under
  ACCESS EXCLUSIVE.
- `ALTER COLUMN TYPE` rewrites unless binary-coercible (widening `varchar(n)`,
  `varchar` to `text`).
- `SET NOT NULL` scans under ACCESS EXCLUSIVE; from version 12 a validated
  `CHECK (col IS NOT NULL)` lets it skip the scan. Safe path: add the check
  `NOT VALID`, `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE), then set not null.
- Foreign keys and checks: add `NOT VALID`, validate separately.
- `CREATE INDEX CONCURRENTLY` cannot run in a transaction block; Alembic and
  Django wrap migrations in one by default. A failed build leaves an INVALID index
  (`pg_index.indisvalid = false`) that still slows writes.
- Lock queueing: a brief ACCESS EXCLUSIVE request waiting behind a long query
  blocks every later query on the table. Require `SET lock_timeout` with retry.
- Long transactions (big backfills) hold back the xmin horizon; tables bloat.

### Online migrations (SQLite, MySQL)
- SQLite `ALTER TABLE` cannot add a NOT NULL column without a non-null default
  or add a column with a non-constant default; type and constraint changes need
  the documented rebuild (new table, copy, drop, rename, `PRAGMA
  foreign_key_check`). `PRAGMA foreign_keys` is per connection, off by default, and
  ignored inside a transaction, so set it before `BEGIN`.
- SQLite read-then-write in a deferred transaction can get SQLITE_BUSY at the lock
  upgrade without the busy handler running; use `BEGIN IMMEDIATE`. WAL mode
  needs shared memory, so not on network filesystems; a long-lived reader stops
  checkpoints and the WAL file grows without bound.
- MySQL: state `ALGORITHM=INSTANT|INPLACE, LOCK=NONE` so an unsupported change
  fails instead of silently copying the table.

### Expand, backfill, contract
- Renames and type changes are add-new, dual-write, backfill, switch reads, stop
  old writes, drop old, across separate deploys. An ORM autogenerate that sees a
  rename as drop plus add (Alembic does) loses the column's data.
- Backfills: keyset batches by primary key (never OFFSET), a commit per batch,
  throttling, resumable progress, and idempotent per row.
- Rollback: after contract, the down migration is lossy. The honest plan keeps
  the old column populated until the new path is proven, or states forward-fix
  plus backup.

### Serialized format compatibility
- Strict readers reject additions: closed enums (`Literal`, serde enums without a
  catch-all, proto2 enums), `extra="forbid"` on persisted or internal messages.
  Strictness suits untrusted ingress; on internal formats it breaks forward
  compatibility. Judge which one each model is.
- Protobuf: never reuse or renumber fields; `reserved` removed numbers and names;
  renaming is wire-safe but breaks the JSON mapping.
- JSON integers above 2^53 lose precision in JavaScript consumers; send 64-bit IDs
  as strings. Python's `json.dumps` emits `NaN` and `Infinity` by default, which
  strict parsers reject.
- A changed code default silently changes the meaning of records that stored
  nothing. Persisted files need a version field.

### Idempotency and delivery
- Check-then-insert dedupe without a unique constraint races; enforce with
  `UNIQUE` plus `INSERT ... ON CONFLICT DO NOTHING` or equivalent.
- Key scope is (client, key); store a request hash to reject key reuse with a
  different payload; write the key in the same transaction as the effect.
- Database write plus message publish is a dual write; use a transactional outbox.
  Consumers get at-least-once delivery: dedupe by message ID inside the effect's
  transaction.

### Isolation anomalies
- READ COMMITTED (PostgreSQL default): read-modify-write in application code loses
  updates. Fix with `UPDATE ... SET n = n + :d`, `SELECT ... FOR UPDATE`, or a
  version column checked in the `WHERE` clause with rowcount verified.
- PostgreSQL REPEATABLE READ turns lost updates into serialization failures but
  allows write skew. It and SERIALIZABLE need a retry loop on SQLSTATE 40001.
- Multi-row rules such as "no overlapping bookings" need an exclusion constraint
  (`EXCLUDE USING gist`), a lock on a parent row, or SERIALIZABLE.
- MySQL InnoDB REPEATABLE READ: plain reads are snapshots; only locking reads take
  gap locks, so check-then-insert still admits phantoms.

### Invariants and state machines
- Case-insensitive uniqueness needs a functional unique index (`lower(email)`) or
  `citext`; `UNIQUE` admits multiple NULLs unless `NULLS NOT DISTINCT`
  (PostgreSQL 15+). Soft delete breaks plain uniqueness; use a partial index.
- Transitions as `UPDATE ... SET state = 'B' WHERE id = :id AND state = 'A'` with
  the rowcount checked; otherwise two workers both transition.
- Intermediate states (`PROCESSING`) need a lease or heartbeat and a reaper, or a
  crash strands them forever. Every state needs a path to a terminal state.

### Values
- Store instants as UTC with zone-aware types; store future local events as local
  time plus IANA zone, because rules change. `timestamp without time zone` holding
  local time is ambiguous at DST transitions.
- Precision: PostgreSQL microseconds, JavaScript milliseconds, Go nanoseconds;
  equality after a round trip fails.
- Money: decimal with explicit scale and rounding mode, or integer minor units with
  per-currency exponent (JPY 0, BHD 3). Floats are fine for scores and ratios.
- Encoding: MySQL `utf8` is 3-byte and rejects 4-byte characters (use `utf8mb4`);
  collation decides uniqueness; normalise identifiers (NFC) before comparing.
- Absent, null and default are three states; PATCH handlers must distinguish them
  (Pydantic `model_fields_set`). `NOT NULL DEFAULT ''` hides missing data.

### Cache and derived state coherence
- Invalidate after commit (`transaction.on_commit`), never before; a rollback
  otherwise leaves the cache ahead of the database.
- Delete-then-reload races: a reader loads the old value and writes it back after
  the invalidation. Use versioned keys or a short TTL as a backstop.
- Durable file writes fsync the file and its directory before relying on a rename.

## Evidence standard

- Run migrations up, down and up on the same engine and version with
  representative data (a container, or a copy of the SQLite file). Observe locks
  (`pg_locks` joined to `pg_stat_activity`) rather than asserting them.
- Prove a race with a two-session interleaving (two `psql` or `sqlite3` sessions,
  or a test with two connections and a barrier); otherwise mark it medium.
- Compatibility: load old-code output with new code and the reverse, using
  fixtures from `git show <old-commit>:<path>`.

## Severity guide

- **P0**: data loss or corruption on a realistic path (rename as drop plus add, a
  lossy backfill, a race that double-charges); a migration that locks a large hot
  table for unbounded time; a deploy order that crashes deployed readers.
- **P1**: lost update or write skew in a normal concurrent path; idempotency
  without a unique constraint; no rollback for a format change; a stuck
  intermediate state with no recovery; enum addition breaking old consumers.
- **P2**: missing `lock_timeout`; timezone or precision mismatch in edge cases; a
  code-only invariant with a narrow race; cache invalidated before commit.
- **P3**: index or constraint improvements with no current defect.

## Skill-specific output

1. **Data surface inventory**: `| Surface | Kind | Engine/format and version | Writers | Readers | Source of truth |`
2. **Schema evolution matrix**: `| Element | Before | After | Old reader + new data | New reader + old data | Verdict (COMPATIBLE / NEEDS EXPAND-CONTRACT / BREAKING) |`
3. **Migration safety table**: `| Migration:statement | Lock on this engine | Rewrite or scan | In transaction | Blocking risk (size basis) | Rollback |`
4. **Invariant enforcement map**: `| Invariant | Database enforcement | Code enforcement (path:line) | Concurrency-safe at configured isolation |`
5. **Data-contract requirements (EARS)**: `REQ-DATA-001` onward, e.g. "WHEN two
   requests share an idempotency key, the service SHALL return the first result
   and write nothing", each with a verification method.

## Anti-patterns

- **Engine-agnostic rules.** Rule: name the engine and version behind every claim.
- **Reflexive down migrations.** A down step that drops new data is worse than none.
  Rule: require an honest rollback (dual-write window or forward-fix plus backup).
- **Float panic.** Rule: flag floats only for money, counts, or values compared for
  equality.
- **SERIALIZABLE everywhere.** Rule: fix the anomaly with the narrowest tool
  (atomic update, row lock, constraint).
- **Strictness everywhere.** Rule: strict schemas at untrusted ingress, tolerant
  readers for internal and persisted formats.
- **Ignoring size.** Rule: state the row count behind every blocking finding, or
  the threshold at which it applies.
- **Key column equals idempotency.** Rule: idempotency requires a unique constraint
  and same-transaction write.

## Done when

- [ ] Engine, version, isolation level and deploy model are stated with sources.
- [ ] Every changed surface is in the evolution matrix, both directions judged.
- [ ] Every migration statement has a lock, rewrite and rollback entry.
- [ ] Every write path was interleaved and crash-tested on paper or in practice.
- [ ] Every invariant shows where it is enforced and whether that is race-safe.
- [ ] Every P0-P2 finding has a REQ-DATA requirement with a verification method.
