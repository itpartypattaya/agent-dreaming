# agent-dreaming

A nightly, read-only memory review for **Claude Code** and **Codex CLI**.

Agents that remember things also remember wrong things: a guess stored as a fact, two retellings of one fact, a note
about a project that ended a month ago. agent-dreaming compares what your agents *stored* with what *you actually said*
to them, and once a day asks you a handful of short questions about it. It changes memory only when you answer "yes",
and only through the memory tools your agent already has.

- **The pass** (`dreamem run`) is deterministic: no model, no network, a few seconds. It reads your Claude Code
  transcripts, Codex sessions, the facts of [holographic-memory](https://github.com/itpartypattaya/holographic-memory)
  and Claude Code's own memory notes, and writes a report under `~/.dreaming`. Nothing else.
- **The summary**: the next session starts with one line — "Dreaming 2026-10-30: 3 memory questions wait…" — counts
  only, never the text of a fact.
- **The review** (`/agent-dreaming:dream` in Claude Code, `$dreaming` in Codex): the agent puts the questions to you one
  at a time and carries out exactly your answers.

## What it asks

| Question | Example | Your answers |
|---|---|---|
| Look like one fact — merge? | «Supplier brings beans on Tuesdays» and «Beans arrive Tuesday mornings» | yes · not-duplicates |
| Conflicts with what memory says | a fact says 9 pm, a note says 10 pm | changed · outdated · wrong · keep |
| Not mentioned for long — still true? | a fact nobody has said in a month | keep · outdated · changed · wrong · forget |
| An answer that used it went wrong — still true? | holographic-memory's journal: an agent reported it was corrected (stale fact) | keep · outdated · changed · wrong · forget |
| An agent stored this, nobody confirmed | an agent's own conclusion | yes · no · outdated · wrong · forget · reject |
| People keep saying it | you said it on three different days | yes · no |
| A Claude Code note nobody mentioned lately | `memory/deploy.md` of an old project | keep · outdated (you edit the note yourself) |

Any question also takes **later**. An unanswered question comes back the next night; an answered one rests.

Only **your** words count as evidence. Background tasks, scheduled jobs, subagents, `codex exec` scripts, slash
commands, reminders and quotes of the agent are recognised and left out. A message holding a secret or an instruction
to an agent is not read at all.

The journal question needs holographic-memory 0.4+ with `experience = true`. A report there is the agent's,
not yours: it is asked only for a stale fact (not for a bad search or a wrong answer), once per report, and only
while the fact is still the version the answer saw.

## Install

Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/itpartypattaya/agent-dreaming
dreamem run --dry-run        # see what it would ask, keep nothing
```

**Claude Code** — the plugin brings the review skill and the two session-start hooks:

```
/plugin install agent-dreaming --marketplace itpartypattaya/agent-dreaming
```

**Codex CLI** — one marked block in `~/.codex/config.toml` and the skill in `~/.agents/skills/dreaming`
(`--dry-run` shows the diff, `--uninstall` removes it; then trust the two hooks in `/hooks`):

```bash
dreamem install codex
```

**When the pass runs** is yours to choose — your computer may be off at night, or you may use the agent by day only:

```bash
dreamem config time 23:00    # the first session after 23:00 runs it (local time)
dreamem config time any      # the first session of each day
```

Until you choose, your agent asks you once in a session (on three days at most), and `dreamem install codex` asks in
the terminal. The pass itself runs in the background at a session start; nothing runs while no agent is open.

**Optional schedule** — a daily run at that time (04:00 if none) that catches up after sleep (Task Scheduler / launchd /
systemd timer); run it again after changing the time:

```bash
dreamem install schedule --dry-run
```

## What it reads, writes, and never does

| | |
|---|---|
| Reads | `~/.claude/projects/*/*.jsonl` (transcripts), `~/.claude/projects/*/memory/*.md` (auto-memory), `~/.codex/sessions/**` (`.jsonl` and `.jsonl.zst`), `~/.codex/memories*/`, `CLAUDE.md` / `AGENTS.md`, holographic-memory's database (read-only). Locations follow `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `HOLOMEM_HOME`. |
| Writes | only `~/.dreaming` (or `DREAMEM_HOME`): its state, the questions, `report.md`, and `claude-lines.json` — where in each transcript the lines that may be yours start and end, so the next pass does not read gigabytes of tool output again. Your messages are read in memory for the pass and stored nowhere — not even as snippets. |
| Never | writes to memory, transcripts, Claude or Codex settings; sends anything over the network; runs a model. Memory changes are made by your agent, in a session, on your answer. (SQLite itself may create the `-wal`/`-shm` files next to the holographic-memory database when it reads a database in WAL mode whose writer is not running, as any reader does; the database is never written.) |

Details: [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md).

## Commands

```
dreamem run [--if-due] [--dry-run]      the pass
dreamem show                            open questions (for the review)
dreamem decide Q3 yes|no|… [--text T]   record an answer; prints the memory call to make
dreamem applied Q3 | failed Q3 | undo Q3
dreamem list | status | forget-sweep
dreamem config time [HH:MM|any]         when the pass runs
dreamem install claude-code|codex|schedule [--dry-run] [--uninstall]
```

## Settings

`~/.dreaming/config.toml` (all optional):

```toml
clients = ["claude-code", "codex"]
timezone = "auto"            # the machine's offset; or an IANA name
dream_time = "23:00"         # when the pass runs: HH:MM, or "any" (first session of the day)
max_questions = 7            # per night; the rest wait
max_message_chars = 2000     # longer turns are pasted documents, not your words
exclude_patterns = []        # regexes of text that must never count as yours
fact_source = "auto"         # auto | holographic | none

[windows]
themes_days = 14
corroboration_days = 30      # Claude Code keeps transcripts 30 days by default
md_decay_days = 30
```

Without holographic-memory the review works on Claude Code's memory notes alone. With holographic-memory in
`scope = "project"` each project's store is reviewed with that project's conversations only.

## Limits

- Both CLIs' session formats are internal and change between versions. Unknown shapes are left out and reported as
  partial coverage; a night with partial coverage asks no "still relevant?" question.
- A short quote you pasted and a sentence you typed look the same in a transcript.
- Claude Code's memory notes and Codex memories are never edited by this tool (the review tells you which note to fix).
- holographic-memory checks versions on merge (`expect`); for other changes the review checks the version right
  before the call.

## Licence

MIT. The dreaming engine in `dreamem/engine` is maintained in this repository; see its `NOTICE.md`.
