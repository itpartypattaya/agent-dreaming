# Changelog

## 0.2.0 — 2026-10-30

The engine works on direct inputs. `dream.night()` takes the person's messages, the memory notes, the fact store's
path, the answers to the previous night and a state folder; the "shadow home" of 0.1.0 (a temporary agent home with a
message database and config files) is gone, and so are the parts of the engine that served its first host (config
reader, message database, scheduled jobs, cloud judge, per-chat stores, approval queue): 7 572 → about 5 300 lines.
The engine runs in process — a pass and the test suite are several times faster.

- Privacy: the person's messages are read in memory only — no temporary database, no evidence snippets or theme samples
  in the saved output; memory notes are not copied.
- One question about a fact a night (a fact offered twice is asked once; the other waits).
- PR #1 review: a fact changed by another session while the pass ran is not asked about with the new version under the
  old text; `forget-sweep` cleans the night's output by tombstone even when the fact never became a question; a
  question whose fact was forgotten no longer breaks `show`, the report or the sweep.
- Security review: `dreamem install codex` never overwrites or removes another skill named `dreaming`; the cleanup of a
  killed pass removes only its own `<name>.partial` files; transcripts are read line by line (a 250 MB transcript no
  longer sits in memory).
- The session-start line orders nights by when they were published (two passes in one second are two nights).

## 0.1.0 — 2026-10-30

First version: the read-only pass over Claude Code transcripts, Codex rollouts (plain and zstd), auto-memory notes
and holographic-memory; the question protocol (show / decide / applied / failed / undo) with per-question cooldowns;
session-start hooks for both CLIs; the review skill (`/agent-dreaming:dream`, `$dreaming`); `dreamem install codex`,
`install schedule`; the Claude Code plugin and marketplace.
