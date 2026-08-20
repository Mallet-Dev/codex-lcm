# codex-lcm design

## 1. Scope

codex-lcm is a local context sidecar for OpenAI Codex CLI on macOS. It integrates through Codex command hooks and a stdio MCP server. Its responsibilities are:

1. capture conversation events without blocking the active Codex turn;
2. retain redacted source messages and structured tool payloads in SQLite;
3. compact older messages into a recoverable summary hierarchy;
4. restore bounded context at session start; and
5. provide lexical recall and paged source expansion.

The current release does not provide embeddings, semantic similarity, automatic retention/deletion, cross-device synchronization, or Windows/Linux support.

## 2. System flow

```text
Codex CLI
  │
  ├─ SessionStart ────────────────┐
  ├─ UserPromptSubmit ────────────┤
  ├─ PostToolUse ─────────────────┤
  ├─ PreCompact / PostCompact ────┤
  └─ Stop ────────────────────────┤
                                  ▼
                         codex-lcm hooks
                                  │
                 ┌────────────────┼────────────────┐
                 ▼                ▼                ▼
            SQLite WAL      compaction worker   restore assembler
                 │                │                │
                 └────────────────┴────────────────┘
                                  │
                                  ▼
                         stdio MCP recall server
```

All persistent state is local. The default database is `~/.codex-lcm/codex-lcm.db`.

## 3. Storage invariants

### 3.1 Source retention

- Raw `messages` rows are immutable after insertion.
- Structured `message_parts` retain complete redacted tool input and output.
- Searchable message text contains bounded tool previews so FTS growth is controlled.
- Compaction adds summary rows and source links; it does not delete source messages or inactive summaries.
- Secret redaction runs before content is persisted.

“Lossless” refers to recoverability after redaction. It does not mean byte-for-byte retention of unredacted input.

### 3.2 Active context

The assembled context is:

```text
active summaries ordered by source chronology
+ newest unsummarized raw messages
```

When the context budget is tight, the newest raw content has priority. The assembler enforces an estimated token budget as a hard character bound (`tokens × 4`).

### 3.3 Atomicity

A message append reserves the next session sequence and writes the message, message parts, and FTS row inside one `BEGIN IMMEDIATE` transaction. Duplicate `stable_key` inserts roll back without leaving orphaned FTS content.

Provider-call reservations also use `BEGIN IMMEDIATE`, so concurrent workers cannot overspend the configured per-session call allowance.

## 4. SQLite schema

The schema is created with `PRAGMA journal_mode=WAL`, `foreign_keys=ON`, and a busy timeout.

### `sessions`

One row per Codex session:

- `session_id` primary key
- `project_key` derived from working directory and Git remote
- `cwd`, `git_remote`, `git_branch`, `model`
- `created_at`, `updated_at` as UTC ISO 8601 strings

### `messages`

Raw captured events:

- composite primary key: `(session_id, seq)`
- `role`, `content`, estimated `token_count`, `created_at`
- optional `stable_key`, unique within a session

### `message_parts`

Complete redacted structured payloads:

- composite primary key: `(session_id, message_seq, part_idx)`
- `kind` and `content`
- foreign key to `messages`

### `summaries`

Summary DAG nodes:

- `summary_id` primary key
- `session_id`, `depth`, `kind`, `content`, estimated `token_count`
- source time range: `earliest_at`, `latest_at`
- `descendant_count`, `active`, `created_at`

### `summary_messages`

Links a leaf summary to its source message sequence numbers.

### `summary_parents`

Links a condensed summary to its immediate parent summaries. Parents become inactive when the condensed node is created, but remain stored and expandable.

### `summary_spend`

Records reserved provider attempts by session and timestamp. Old reservations are removed when a new reservation transaction evaluates the configured window.

### FTS5 tables

- `messages_fts` indexes bounded message text.
- `summaries_fts` indexes summary content.

FTS writes are explicit and transactional. Retrieval uses FTS5/BM25 where possible and parameterized literal substring fallback where needed.

## 5. Capture pipeline

`Capturer` handles user, assistant, and tool events.

1. Normalize the hook payload to text and structured parts.
2. Redact known credential patterns.
3. Bound the searchable tool preview.
4. Preserve complete redacted tool payloads in `message_parts`.
5. Atomically append the event with a stable de-duplication key when available.

The redactor covers common API keys, GitHub and Slack tokens, AWS and Google keys, JWTs, bearer headers, password/secret assignments, JSON credential fields, and PEM private-key blocks. This is defence in depth; unknown secret formats can still pass through.

## 6. Compaction

### 6.1 Leaf pass

1. Protect the newest `fresh_tail` messages.
2. Select the oldest eligible unsummarized messages.
3. Split them at the configured estimated-token limit.
4. Reserve a provider attempt.
5. Try normal LLM summarization.
6. If necessary and budget remains, reserve and try the aggressive prompt.
7. Fall back to deterministic head/tail truncation when no usable LLM result exists.
8. Persist the leaf and its source links.

### 6.2 Condensation pass

The engine scans active summaries in source chronology. At the shallowest eligible depth, it selects the first contiguous run that meets the configured fanout, reserves a provider attempt, and creates a depth-plus-one node. If LLM summarization fails, deterministic truncation is used.

### 6.3 Worker lifecycle

- `Stop` and `PostCompact` can spawn a detached worker.
- A per-session non-blocking `flock` collapses concurrent worker starts.
- SQLite WAL supports concurrent readers and serialized writes.
- Each run stops when no work remains, the configured round limit is reached, or the provider-call allowance is exhausted.
- A low savings ratio is recorded as stalled rather than triggering an unbounded loop.

## 7. Hook contracts

### `SessionStart`

Ensures the session exists, assembles active summaries plus the fresh tail, and writes Codex `hookSpecificOutput.additionalContext` when context exists. The default application budget is 5,000 estimated tokens; installed hook metadata allows up to 8,000.

### `UserPromptSubmit`

Captures the current user prompt. Errors are swallowed so prompt submission continues.

### `PostToolUse`

Captures the tool name, redacted bounded preview, and complete redacted structured input/output. Errors do not fail the Codex tool turn.

### `PreCompact`

Consumes the hook payload and exits immediately. It does not delay Codex's native compaction.

### `PostCompact`

Starts a detached codex-lcm compaction worker and exits without waiting for provider work.

### `Stop`

Captures the final assistant message. When the unsummarized backlog exceeds the protected fresh tail plus the configured margin, it starts a detached worker. It writes no stdout, so it does not override Codex's stop decision.

## 8. MCP server

The zero-dependency server speaks newline-delimited JSON-RPC 2.0 on stdio and exposes:

- `lcm_status`
- `lcm_grep`
- `lcm_recall`
- `lcm_expand`
- `lcm_recent`
- `lcm_describe`

The server validates integer and numeric parameters, returns standard parse/invalid-request/method errors, and continues after malformed input. Tool output is bounded: raw-message and summary expansion are paged by character offset with a 2,000-character maximum page; grep, recall, and recent accept at most 10, 5, and 20 results respectively. The dispatch boundary caps every response at 24,000 characters, derived from the largest result set (20) and existing 1,200-character item preview.

Retrieval is lexical. No tool claims embedding or vector semantics.

## 9. Installation and filesystem safety

`codex-lcm install`:

1. initializes the database;
2. validates existing `hooks.json` before changing it;
3. merges only codex-lcm hook groups;
4. appends an exact `[mcp_servers.codex-lcm]` section;
5. creates timestamped backups before existing files change; and
6. writes configuration replacements atomically.

`codex-lcm uninstall` removes only hook commands that execute `codex_lcm.hooks.*` modules and the exact MCP section. It leaves stored context intact.

On POSIX systems, the default data directory and worker subdirectories use mode `0700`. Database files, SQLite sidecars, locks, and logs use mode `0600`.

## 10. Failure behaviour

- Hook handlers fail open so a local memory failure does not block Codex.
- Session-start errors are reported on stderr; other high-frequency capture hooks stay quiet.
- Provider initialization or requests fall back to deterministic compaction.
- Worker lock contention exits successfully without duplicate work.
- Malformed user configuration is reported rather than silently replaced.
- MCP protocol errors produce JSON-RPC errors without terminating the server loop.

## 11. Verification

The repository contains four complementary checks:

- `pytest`: unit and integration contracts;
- `tests/smoke_test.py`: capture, compaction, restore, redaction, hooks, and MCP protocol;
- `tests/reliability_test.py`: hook latency, concurrent capture, large payloads, and lock contention;
- `tests/package_test.py`: source build, isolated install, doctor, permissions, and uninstall in a throwaway home directory.

All checks use temporary storage and do not require mutation of a live Codex profile.
