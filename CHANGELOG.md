# Changelog

## 0.3.2 — 2026-10-31

- `dreamem install codex` keeps what Codex itself appended inside our block: Codex adds a new table at the end of
  its `config.toml`, before our end mark (`[tui] screen_reader_detection_done = true`, seen live), and a reinstall
  or `--uninstall` removed it with the block. Now only our two hook groups are replaced, byte for byte (their trust
  in `/hooks` holds); anything else between the marks is moved after the block and kept.

## 0.3.1 — 2026-10-31

- A session-start hook whose reader went away (the stdout pipe closed) ends quietly with exit 0 instead of a
  traceback and exit 120; the night's line it could not deliver is shown in the next session.

## 0.3.0 — 2026-10-31

- The time of the pass is the person's: `dream_time = "HH:MM"` (or `dreamem config time 23:00`) — the first session
  after that time runs it, not the first session of the calendar day; `any` keeps the old rule. Until a time is chosen,
  the session-start hook asks the agent to ask the person once (from the first session after install, once a day, on
  three days at most), and `dreamem install codex` asks in the terminal (or takes `--time 23:00`). `dreamem install
  schedule` uses the time. `dreamem status` shows it. `--dry-run` with `--time` previews the time and keeps nothing;
  the asking is recorded in its own `time-asked.json` before it is made, so a pass holding the lock cannot lift the
  once-a-day and three-day limits (Codex review of PR #2).

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
- Codex review (gpt-6.1-sol, 17 findings; 15 fixed with a test each, red before the fix):
  question ids carry their night (`Q3-7f2a`), so an answer never lands on a question of a newer night; a fact's text is
  compared whole (by fingerprint) with the version a question carries; `reject` refuses a fact changed since it was
  shown; the lock is an OS file lock (no stale takeover, no age limit on a live holder); a note's name is screened like
  its text; only decided questions rest — marks of held-back items and of a night that died before it was published
  are dropped; `undo` lifts a rejection or a "not duplicates" too; a quarantined instruction keeps no preview; an
  answer's `--text` with a secret is refused; `forget-sweep` removes the recorded tool answer too; unmarked SDK lines
  stay unknown unless an older Claude Code version is proven; the hook prints exactly one JSON document; Codex rollouts
  are streamed, compressed ones too; a state or settings file in a foreign encoding no longer stops the pass; themes
  (words of messages) are no longer kept. Not fixable here: version pinning of writes other than merge
  (holographic-memory#2), and SQLite creating `-wal`/`-shm` side files when reading a WAL database (documented).

## 0.1.0 — 2026-10-30

First version: the read-only pass over Claude Code transcripts, Codex rollouts (plain and zstd), auto-memory notes
and holographic-memory; the question protocol (show / decide / applied / failed / undo) with per-question cooldowns;
session-start hooks for both CLIs; the review skill (`/agent-dreaming:dream`, `$dreaming`); `dreamem install codex`,
`install schedule`; the Claude Code plugin and marketplace.
