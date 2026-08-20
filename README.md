# codex-lcm

Local, lossless context management for OpenAI Codex CLI.

codex-lcm captures Codex conversation events in SQLite, compacts older context into a hierarchy of summaries without deleting the source messages, restores relevant context when a session starts, and exposes lexical recall through a local MCP server.

> **Meaning of “lossless”:** source content is retained after configured secret redaction. Searchable tool previews are bounded, while complete redacted tool input and output remain available through paged expansion.

## Status and platform

codex-lcm is beta software for macOS. It relies on Codex hooks and POSIX `flock`; Windows and Linux are not currently supported or claimed.

## Features

- **Live capture:** user prompts, assistant responses, and tool activity are captured through Codex hooks.
- **Durable local storage:** SQLite in WAL mode stores raw messages, structured message parts, summaries, and FTS5 indexes.
- **Hierarchical compaction:** older messages become leaf summaries; eligible summary runs can be condensed into deeper DAG nodes. Source rows are never deleted automatically.
- **Context restore:** active summaries and the newest unsummarized messages are injected at session start within a strict size bound.
- **Local recall:** six stdio MCP tools provide lexical search, recent-session lookup, metadata inspection, and paged source expansion.
- **Fail-open hooks:** capture or compaction failures do not block a Codex turn.
- **Safety controls:** secret redaction, per-session compaction locks, atomic message/FTS writes, provider-call reservations, progress checks, and private default file permissions.

## Install from a repository checkout

```bash
python3 -m venv ~/.codex-lcm/venv
~/.codex-lcm/venv/bin/python -m pip install .
~/.codex-lcm/venv/bin/codex-lcm install
~/.codex-lcm/venv/bin/codex-lcm doctor
```

Restart Codex after installation. Use `/hooks` in Codex to inspect and trust the installed hooks.

The installer merges codex-lcm entries into `~/.codex/hooks.json` and `~/.codex/config.toml`. It creates timestamped backups before changing existing files. Running the installer again is idempotent.

A source checkout also includes `scripts/install.sh`, which performs the same isolated installation and verification.

### Optional LLM summarization

On Python 3.11+, the core package has no third-party runtime dependencies. Python 3.10 installs the `tomli` compatibility backport for safe TOML configuration handling. Without an LLM client, compaction uses deterministic head/tail truncation.

To enable OpenAI-compatible summarization, install the optional extra and configure credentials:

```bash
~/.codex-lcm/venv/bin/python -m pip install ".[llm]"
export CODEX_LCM_API_KEY="..."
```

`OPENAI_API_KEY` is also recognized. Use `CODEX_LCM_BASE_URL` or `OPENAI_BASE_URL` for an OpenAI-compatible endpoint. The `CODEX_LCM_*` provider settings are treated as a pair and take precedence as a pair, so a global key is never sent to a scoped endpoint. When `CODEX_LCM_BASE_URL` is set, configure `CODEX_LCM_API_KEY` too.

## CLI

```text
codex-lcm install                       Wire hooks and the MCP server into Codex
codex-lcm uninstall                     Remove codex-lcm wiring and keep stored data
codex-lcm status                        Show database statistics
codex-lcm doctor                        Verify package, database, hooks, and MCP wiring
codex-lcm compact --session <id>        Compact one session synchronously
codex-lcm compact --all                 Compact every recorded session synchronously
```

Uninstalling does not delete `~/.codex-lcm/` or its database.

## MCP tools

| Tool | Purpose |
|---|---|
| `lcm_status` | Database and active-session statistics |
| `lcm_grep` | Literal and FTS5 lexical search over messages and summaries |
| `lcm_recall` | Ranked lexical recall with bounded result counts |
| `lcm_expand` | Summary-source inspection or paged raw-message recovery |
| `lcm_recent` | Sessions updated inside a UTC look-back window |
| `lcm_describe` | Session or summary metadata |

Recall is lexical, not semantic: there are no embeddings or vector-similarity claims in this release.

## Configuration

Configuration is read from environment variables.

| Variable | Default | Purpose |
|---|---:|---|
| `CODEX_LCM_DB_PATH` | `~/.codex-lcm/codex-lcm.db` | SQLite database path |
| `CODEX_LCM_FRESH_TAIL` | `25` | Newest raw messages protected from compaction |
| `CODEX_LCM_LEAF_CHUNK_TOKENS` | `20000` | Maximum estimated tokens in a leaf chunk |
| `CODEX_LCM_LEAF_MIN_FANOUT` | `3` | Active leaf summaries required for condensation |
| `CODEX_LCM_CONDENSED_MIN_FANOUT` | `3` | Active deeper summaries required for condensation |
| `CODEX_LCM_MAX_ROUNDS` | `5` | Compaction rounds per worker run |
| `CODEX_LCM_MAX_SUMMARY_CALLS` | `12` | Provider attempts allowed per session and call window |
| `CODEX_LCM_SUMMARY_CALL_WINDOW_MS` | `600000` | Provider-attempt accounting window |
| `CODEX_LCM_SUMMARY_TIMEOUT_MS` | `60000` | Provider request timeout |
| `CODEX_LCM_PROGRESS_THRESHOLD` | `0.10` | Marks a compaction run stalled below this savings ratio |
| `CODEX_LCM_ADDITIONAL_CONTEXT_LIMIT` | `5000` | Restore-context budget in estimated tokens |
| `CODEX_LCM_COMPACTION_BACKLOG` | `5` | Extra unsummarized messages allowed before `Stop` starts a worker |
| `CODEX_LCM_COMPACTION_MODEL` | `gpt-4.1-mini` | Leaf summarization model |
| `CODEX_LCM_CONDENSATION_MODEL` | `gpt-4.1` | Condensation model |
| `CODEX_LCM_API_KEY` / `OPENAI_API_KEY` | — | Optional provider credential |
| `CODEX_LCM_BASE_URL` / `OPENAI_BASE_URL` | — | Optional OpenAI-compatible endpoint |
| `CODEX_LCM_CAPTURE_TOOLS` | `1` | Set to `0` to disable tool capture |
| `CODEX_LCM_TOOL_INPUT_MAX_CHARS` | `2000` | Searchable tool-input preview length |
| `CODEX_LCM_TOOL_RESULT_MAX_CHARS` | `2000` | Searchable tool-output preview length |

The default data directory is mode `0700`; the database, WAL sidecars, lock files, and compaction log are mode `0600` on POSIX systems.

## Data and security boundaries

- Redaction occurs before persistence for known API keys, tokens, authorization headers, password/secret assignments, JWTs, and PEM private keys.
- Credential-bearing Git remote URLs are stored without user information, query parameters, or fragments.
- Redaction is defence in depth, not a guarantee that every possible secret format will be recognized.
- Complete redacted tool payloads can still contain private conversation or project data. Protect the local account and database backups accordingly.
- No data is sent to an LLM provider unless the optional client is installed and provider credentials or a base URL are configured.
- Automatic retention and deletion are intentionally absent. The application does not silently discard source history.

## Development

Use a project-local environment; tests never need the live Codex profile or database.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q
.venv/bin/python tests/smoke_test.py
.venv/bin/python tests/reliability_test.py
.venv/bin/python tests/package_test.py
.venv/bin/ruff check .
.venv/bin/bandit -q -r codex_lcm
```

The package test builds and installs into a throwaway home directory and virtual environment. The smoke and reliability scripts also use temporary databases.

See [DESIGN.md](DESIGN.md) for the storage model, invariants, hook flow, and failure behaviour.

## License

MIT
