# Privacy

agent-dreaming runs entirely on your computer. It has no server, no account, no telemetry, and makes no network
connections: the pass is a local program that reads files and writes a report.

## What it reads

- Claude Code transcripts (`<config dir>/projects/*/*.jsonl`) and auto-memory notes (`projects/*/memory/*.md`).
- Codex CLI sessions (`$CODEX_HOME/sessions`, `archived_sessions`) and memories (`memories*/`).
- `CLAUDE.md` / `AGENTS.md` instruction files (to avoid asking about what they already say).
- The holographic-memory database, opened read-only (SQLite may create its `-wal`/`-shm` side files, as for any
  reader of a database in WAL mode; the database itself is never written), its outcome journal included when it is
  on: which facts an answer used and the agent's report on how it went.

Of the transcripts it keeps only what you typed yourself, within the window (30 days by default).

## What it stores

Everything goes to `~/.dreaming` (or `DREAMEM_HOME`), readable by your user only:

- the questions of the last night and their answers (`questions.json`, `report.md`), the engine's output (`dream.json`);
- cooldowns, the list of rejected facts, the duplicate proposals, a snapshot of note sizes and a short diary of
  nights (`profiles/*/state`), and the journal reports you have answered about (`outcomes.json`, their ids);
- the question about a fact from the journal carries the agent's one-line correction, unless it holds a secret;
- `claude-lines.json`: for each transcript, where the lines that may be yours start and end and how far it was read —
  numbers only, no words — so the next pass reads only what is new.

Your messages are read into memory for the length of one pass and written nowhere — not as a copy, not as an
evidence snippet. Memory notes are not copied either: a question quotes the note it is about. A message or note that
holds a secret (a key, token, password) or an instruction aimed at an agent is left out of the pass entirely.

When a fact is forgotten in holographic-memory, `dreamem forget-sweep` (and every pass) removes it from these files.

## What it shares

Nothing. The one line a session sees at start holds counts only. The questions are shown to the agent of your session
when you ask for the review; that agent is subject to its own provider's terms.

## Removing it

`dreamem install codex --uninstall`, `/plugin uninstall agent-dreaming`, `uv tool uninstall agent-dreaming`, then delete
`~/.dreaming`.
