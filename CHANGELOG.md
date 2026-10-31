# Changelog

## 0.6.0 — 2026-10-31

- Memory systems behind one seam (`dreamem/providers`): a system reads its store for the pass and says how an answer
  becomes calls of its own tools. holographic-memory stays the reference, its 33 calls byte for byte as before
  (`tests/fixtures/holomem-0.7.0-calls.json`, a test that fails on any change of an argument).
- Claude Code memory notes are answered into. A note nobody mentioned: keep / **outdated** (deletes it: a note has
  no history) / **changed**. New question `note_duplicate`: a note says what another note or a fact already says —
  drop-note / keep-both. A `conflict` with a Claude Code note also takes note-changed / note-outdated. The agent edits
  the note itself, after the new `dreamem verify Q` shows the file is byte for byte the one the person was shown;
  the commands only record. Codex memories are never edited.
- Subagents' memory is read (`~/.claude/agent-memory/*/`, a project's `.claude/agent-memory[-local]/*/`). A project's
  `autoMemoryDirectory` is named in `dreamem status`, not read (Claude Code honours it only in a trusted folder). A
  Codex memories folder in a shape we do not know makes the coverage partial instead of silently empty.
- The reference MCP `memory` server (`@modelcontextprotocol/server-memory` 2.0.1) as a memory system: its JSONL file,
  found by `MEMORY_FILE_PATH` in Claude Code's or Codex's MCP config or `[providers.mcp-memory] path`, read only.
  Each observation is a fact with a surrogate id (`ids.json`, digests); answers are `delete_observations` by the exact
  text and, for a change, `add_observations` then `delete_observations`; a new fact goes to the entity the agent
  names (`decide --entity`) or `entity` in the settings. "Still true?" waits for 60 days of its own first-sighting
  journal. The fixture and the tool schemas were taken from the server itself, run once on synthetic data.
- Antigravity and Gemini CLI sessions are read (only what the person typed: Antigravity's `<USER_REQUEST>`, Gemini
  CLI's text parts, not tool results); `~/.gemini/GEMINI.md` and a project's `GEMINI.md` count as already written.
  `dreamem install antigravity` puts the review skill in `~/.gemini/config/skills/dreaming` and two named hooks in
  `~/.gemini/config/hooks.json` (the line in Antigravity's shape; tonight's pass started detached), other hooks kept.
- A forgotten or deleted fact's text no longer stays in a question's `locator` (found by the MCP `memory` tests).
- Codex review of 0.6.0 before release (12 findings, all fixed; a test each, red without its fix): a fact holding a
  secret or an instruction is never paired with a note; `forget-sweep` finds a fact inside a `note_duplicate`; an
  observation deleted from the MCP file leaves the engine's own copies too (reject list, cooldowns, diary); a note's
  digest is of the very bytes shown; "changed" to the same text makes no call (it would delete the fact); a saved
  MCP observation is applied without a fact id and keeps no words; a deleted note's text leaves its question;
  Antigravity and Gemini CLI messages raise no candidate the review could not read again; `install antigravity`
  never touches a hook of our name that is not ours; a changed note gets its description too. The skill: a delete
  empties the note by exact replacement first; an MCP call is made only through a server using the file the night
  read.
- SECURITY.md said only `memory_merge` carried `expect`; every holographic-memory write has carried it since 0.4.1.
- `dreamem run --dry-run` keeps nothing, as it says: it reads the transcript position cache (`claude-lines.json`) but
  no longer rewrites it. Positions only, never words, but a preview should leave `~/.dreaming` as it found it.

## 0.5.0 — 2026-10-31

- Possible new facts. The pass picks up to 10 clauses (`max_candidates`, newest first) where the person says something about
  themselves or their people that memory does not hold — commands, questions, code and quotes left out — and the
  review agent judges each before showing anything (lasting, theirs, not in memory, worth asking), then asks about
  one at most: **save** (the fact the agent worded, `memory_add`) / no (a month) / never / not-a-fact /
  already-stored. Measured before release with the same filter and judging rules: 86 % of the judged facts real on a
  blind synthetic persona (38 of 82 facts found), 7 of 7 on a real home (about 1.6 a week).
- Pasted documents are not offered: a message with a file attached (`@"C:\…"`), one of 8 or more lines, a clause
  pasted word for word into 3 messages. On a real home this removed the briefs and specs that led the list; on two
  synthetic persons no real fact was lost.
- No word of the person is kept for it: a candidate is the session file and HMAC-SHA256 digests (a salt made per
  install, `~/.dreaming/salt`) of the message and the clause; `dreamem show` reads the clause again from the session
  file, prints it without saving it, and drops a candidate whose message changed or went. After a save only the new
  fact's id stays; a dry run makes no salt.
- The review no longer bars a memory call through a tool without an `expect` parameter (0.4.2): the call carries
  `expect`, holographic-memory 0.5.0+ checks it, an older one lets it through unchecked (README says to update). The
  gate stood in the way of `memory_add` too, which has no version to pin.
- Candidates take no place of the other questions (`max_questions` is theirs) and the session line counts them as
  one. `dreamem applied Q --fact-id N` records the new fact's id for a saved candidate.
- Codex review of 0.5.0, before release: a failed save's reason and a `--text` given with another answer no longer
  stay in the question file; a candidate gone after a failed save keeps no words and takes no answer; the person's
  `exclude_patterns` apply to candidates; an old Claude Code line (before `turnOrigin`) and a message dated a little
  after its file's mtime are found again; a broken `candidates.json` or reference no longer stops the pass or the
  review.

## 0.4.3 — 2026-10-31

- The pass no longer counts recurring themes of the person's messages: nothing asked about them and they were not
  kept, yet counting them took close to half of a pass. On a real home (2 500 messages, 36 facts) a pass with the
  transcript cache warm went from 16.5 s to 9.0 s; the questions are the same. The engine keeps the count behind a
  `themes` setting, on by default.

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
