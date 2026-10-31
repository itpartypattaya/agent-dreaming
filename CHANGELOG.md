# Changelog

## 0.4.2 — 2026-10-31

- The review needs holographic-memory 0.5.0 or newer for its memory calls. 0.4.1 fell back to checking the version
  by hand "if the tool refuses `expect`" — but an older holographic-memory does not refuse it: it ignores the
  argument and writes unchecked, so the fallback never ran. Now the agent makes a call only through a tool that
  lists an `expect` parameter; without it the answer is recorded, the call is not made, and the person is asked to
  update holographic-memory. The manual version check is gone from the skill and `check.version` from `decide`.

## 0.4.1 — 2026-10-31

- Every memory call the review prints carries `expect` with the version the person was shown, not only a merge:
  holographic-memory now takes it on change, correct, end, forget and feedback too (its issue #2) and refuses a fact
  changed since, in the write's own transaction. With a store older than that the tool refuses the argument, and the
  skill falls back to checking the version right before the call, as before.
- The version is the fact's head, as holographic-memory checks `expect` against it (the newest version that is not
  a correction). A fact ended since (its head is `ended`) is no longer asked about: before, the night could ask
  "is it right?" about it with no version at all, and the answer's write went out unchecked.

## 0.4.0 — 2026-10-31

- A new kind of question from holographic-memory's outcome journal (0.4+, `experience = true`): a fact an agent
  reported an answer went wrong with — named by its correction, or offered in two answers that went wrong — for a
  stale version or a reason it did not narrow down. Asked once per report, only while the fact is still the version
  the answer saw; reports about the search or the answer itself raise nothing, a revised report raises nothing. The
  report is shown as the agent's, not the person's; a correction line holding a secret is not shown. Answers as for
  "still true?": keep / outdated / changed / wrong / forget.
- The pass reads Claude Code transcripts several times faster after the first night: `~/.dreaming/claude-lines.json`
  keeps where the lines that may be a person's start and end (positions and counts, never words), and a file is read
  again only where it grew. A file rewritten, shortened or changed before where the last pass stopped is read whole.
  Measured on 534 transcripts (4 GB): 14.4 s → 2.3 s.
- The reminder to trust the hooks in `/hooks` compares our hooks as Codex parses them, with their place: a header
  written with spaces or a comment no longer brings it (Codex review of 0.3.3), and hooks moved to another place by
  the reinstall now do.

## 0.3.3 — 2026-10-31

- `dreamem install codex` asks for the time only with a person at a console: on Windows the NUL device
  (`< /dev/null`) says `isatty()` and the question was printed with nobody to answer it.
- The reminder to trust the hooks in `/hooks` comes only when our hooks are new or rewritten, not when another
  change to the file (a Codex setting moved out of our block) left them as they were.

## 0.3.2 — 2026-10-31

- `dreamem install codex` keeps what Codex itself appended inside our block: Codex adds a new table at the end of
  its `config.toml`, before our end mark (`[tui] screen_reader_detection_done = true`, seen live), and a reinstall
  or `--uninstall` removed it with the block. Now only our two hook groups are replaced, byte for byte (their trust
  in `/hooks` holds); anything else between the marks is moved after the block and kept.
  Our hook tables are recognised with a trailing comment or spaces in their header too (Codex review of PR #4).

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
