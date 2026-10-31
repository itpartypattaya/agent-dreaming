# agent-dreaming

A nightly, read-only memory review for **Claude Code**, **Codex CLI** and **Antigravity** (Gemini CLI's sessions are
read too).

Agents that remember things also remember wrong things: a guess stored as a fact, two retellings of one fact, a note
about a project that ended a month ago. agent-dreaming compares what your agents *stored* with what *you actually said*
to them, and once a day asks you a handful of short questions about it. It changes memory only when you answer, and
only through the memory tools your agent already has.

![Architecture: the pass reads transcripts, sessions, notes and the fact store read-only, writes only to ~/.dreaming; the session hooks show one line and the review skill asks you; memory changes only through the memory tools or a checked edit of a note, on your answer](docs/diagrams/architecture.svg)

It has three parts:

- **The pass** (`dreamem run`) is deterministic: no model, no network, a few seconds. It reads your Claude Code
  transcripts, Codex, Antigravity and Gemini CLI sessions, the facts of
  [holographic-memory](https://github.com/itpartypattaya/holographic-memory) or of the reference MCP `memory` server,
  and Claude Code's own memory notes (subagents' too), and writes its questions under `~/.dreaming`. Nothing else.
- **The summary**: the next session starts with one line — "Dreaming 2026-10-30: 3 memory questions wait…" — counts
  only, never the text of a fact.
- **The review** (`/agent-dreaming:dream` in Claude Code, `$dreaming` in Codex, the skill `dreaming` in Antigravity):
  the agent puts the questions to you one at a time and carries out exactly your answers.

## How a night and a review go

![Sequence: a session-start hook runs the pass in the background; the pass reads the store and writes questions; next session a hook shows the count, you start the review, the agent asks, records your answer with dreamem decide, checks the store and makes the one memory call](docs/diagrams/night-and-review.svg)

1. **A session starts.** Two hooks run. The quick one prints the summary line of the last finished night, once a
   day (again the same day only when a newer night is there). The background one runs `dreamem run --if-due`: if no
   pass succeeded today and it is past your `dream_time`, the pass runs while you work. Nothing runs while no agent
   is open; a hook that fails prints nothing and never breaks a session.
2. **The pass** takes a lock, reads the last 30 days of your messages, screens them (below), compares them with the
   facts and notes, and publishes the night in one step: `questions.json`, `report.md` and `summary.json`. A pass
   killed half-way leaves the previous night whole.
3. **The review.** When you say "dream" (or start the skill), the agent runs `dreamem show` and asks the first
   question in your language, quoting the texts whole. Up to 7 questions a session; the rest wait.
4. **Your answer** is recorded with `dreamem decide`. Most answers print one memory call — `memory_end`,
   `memory_change`, `memory_merge`… — and the agent first checks with `memory_stats` that this session talks to the
   same store the night read. Then it makes exactly that call and reports back with `dreamem applied` (or
   `dreamem failed`, and the question comes back with the current text). An answer about a Claude Code note prints
   an edit instead: the agent runs `dreamem verify` (is the file still the one you were shown?) and only then
   changes or deletes the note with its file tools.
5. **Next night**, an unanswered question is asked again; an answered one rests.

## Examples

What you say in a Claude Code session, and what follows:

| You say | What happens |
|---|---|
| `/agent-dreaming:dream`, or just "dream" / "разбери сон" | The agent runs `dreamem show` and asks last night's first question; you answer in your words ("yes, merge them", "she moved to Chiang Mai in May", "not now"), it records the answer and makes the one memory call it prints. |
| "run dreaming now" / "сделай проход сейчас" | No night yet, or you want a fresh one: the agent runs `dreamem run` (seconds), then starts the review. |
| "dream at 23:00" / "проход в 23:00" | The agent sets the time with `dreamem config time 23:00`: the first session after 23:00 runs the pass. |
| `dreamem run --dry-run` (in a terminal) | Prints what tonight's pass would ask, keeps nothing: a safe first look. |

## What it asks

Questions come in this order: what may be wrong in memory now, then what may be missing, then the notes.

| Question | When it is asked | Example | Your answers |
|---|---|---|---|
| **Look like one fact — merge?** (`duplicate`) | two facts say the same thing in other words | «Supplier brings beans on Tuesdays» and «Beans arrive Tuesday mornings» | yes · not-duplicates |
| **Conflicts with memory** (`conflict`) | a fact and a memory note or `CLAUDE.md` speak of one thing with other numbers, or one negates the other | a fact says 9 pm, a note says 10 pm | changed · outdated · wrong · keep · when the other side is a Claude Code note: note-changed · note-outdated |
| **An answer that used it went wrong** (`outcome`) | an agent reported in holographic-memory's journal that an answer built on this fact was corrected as stale | the agent was told the office moved | keep · outdated · changed · wrong · forget |
| **Not mentioned for long — still true?** (`fact_decay`) | nobody has said it for the window (30 days) | a project deadline from last month | keep · outdated · changed · wrong · forget |
| **Corroborated, unconfirmed** (`review`) | your conversations agree with it, but nobody said it is so | a fact of unknown origin | yes · no · outdated · wrong · forget · reject |
| **An agent stored this, nobody confirmed** (`new_fact`) | an agent wrote it lately on its own conclusion | «Prefers short answers» | yes · no · outdated · wrong · forget · reject |
| **People keep saying it** (`promotion`) | you said it on different days | you mentioned the same habit three times | yes · no · outdated · forget |
| **Said again** (`confirm`) | a fact repeated on different days, worth marking confirmed | — | yes · no |
| **Rejected, still recalled** (`trust_feedback`) | you rejected a fact earlier and it keeps coming up | — | yes (lower its trust) · no |
| **A Claude Code note repeats what is kept** (`note_duplicate`) | a note says what another note or a fact already says | two notes about the same deploy | drop-note · keep-both |
| **A Claude Code note nobody mentioned lately** (`note_decay`) | an auto-memory note untouched by your conversations | `memory/deploy.md` of an old project | keep · outdated (deletes the note) · changed |
| **A possible new fact** (`new_candidate`) | you said something lasting that memory does not hold | «we live in Tbilisi with my husband Oleg» | save · no · never |

Any question also takes **later**. What each answer does in memory:

- **yes** / **keep** — marks the fact confirmed (`memory_feedback`).
- **outdated** — the fact was true and no longer is: `memory_end`, history kept.
- **changed** — it changed: `memory_change` with the new text you give.
- **wrong** — it was never true: `memory_correct` with the right text.
- **forget** — `memory_forget`, then `dreamem forget-sweep` removes it from the dream's own copies.
- **reject** — never ask about this text again (kept in the pass's own reject list; memory is not touched).
- **not-duplicates** — the pair is never offered again.
- **no** — for an unconfirmed fact: marked unhelpful; for a possible new fact: asked again in a month at the earliest.
- **never** / **not-a-fact** / **already-stored** — a possible new fact is not offered again (the last two are the agent's own verdicts, never shown to you).

### Possible new facts

![Possible new facts: during the night a wide filter finds clauses about you or your people that memory does not hold and keeps only a reference with keyed digests; in the review the message is read again from the session file, the agent judges it, and only a real fact is put to you](docs/diagrams/new-facts.svg)

The pass looks for clauses where you say something about yourself or your people: a relationship, a place, a pet, a
habit, a health condition, a standing rule for the agent. A wide filter drops code, commands, questions, quotes and
pasted documents (an attached file, eight lines or more, a clause repeated in three messages); a clause whose words
memory already holds is skipped. Up to 10 of the newest go to the review each night, outside the 7-question budget.

The pass keeps **no word** of them. A candidate is a reference: which session file, and two keyed digests
(HMAC-SHA256 with this install's own salt) of the message and of the clause. In the review the message is read again
from the CLI's own session file and both digests must match; a line edited or a file removed since — the candidate is
gone, nothing is shown.

Your agent judges each one before you see anything: is it lasting (true in a month, not today's mood or this week's
plan), yours (not a quote, a hypothetical, an opinion about the world), not in memory (`memory_search` first), worth
interrupting you for. When unsure, it is not. It asks you about one at most per review, worded as one short sentence;
on **save** it is added with `memory_add` as something you said.

Measured before release: on a blind synthetic persona 86 % of what the agent then asked about were real facts; on one
real home, 7 of 7 (about two a week).

### The journal question

Needs holographic-memory 0.4+ with `experience = true`. A report there is the agent's, not yours: the question says
so, shows the correction line if there is one, and asks whether the fact still holds. It is asked only for a stale
fact (not for a bad search or a wrong answer), when the fact is named in the correction or came up in two such
reports, once per report, and only while the fact is still the version the answer saw.

## Memory systems

| System | What the night reads | How an answer is carried out | What it cannot ask |
|---|---|---|---|
| **holographic-memory** (0.5.0+) | its SQLite store, read-only | its `memory_*` tools; every write carries `expect`, the version you were shown | — (the reference: every question) |
| **Claude Code memory notes** (auto memory, subagents' memory) | `~/.claude/projects/*/memory/`, `~/.claude/agent-memory/*/`, a project's `.claude/agent-memory[-local]/*/` | the agent edits or deletes the note after `dreamem verify` shows it is the file you were shown | a note has no history: "outdated" deletes it |
| **MCP `memory` server** (`@modelcontextprotocol/server-memory` 2.0.1) | its JSONL file, found by `MEMORY_FILE_PATH` in Claude Code's or Codex's MCP config, or `[providers.mcp-memory] path` | `delete_observations` by the exact text (the agent first finds it in `open_nodes`); a change adds the new text, then deletes the old | no versions, history, origin or trust: no "people keep saying it", "said again", "rejected, still recalled" or "merge?"; "still true?" only after 60 days of its own first-sighting journal; changed and wrong are one answer |
| **Codex memories** | `~/.codex/memories*/` — what is already written there is not asked about | never edited: Codex calls them generated state | — |

Each system is a profile of its own. Not covered: cloud memories (mem0, Supermemory: a network adapter only on request),
claude-mem (observations of tool runs, not facts about you), Zep, Cognee, Letta.

## Agents

| Agent | Your messages | The review | The session line |
|---|---|---|---|
| Claude Code | transcripts | plugin skill `/agent-dreaming:dream` | plugin hook |
| Codex CLI | rollouts (plain and zstd) | skill `$dreaming` (`dreamem install codex`) | hook in `config.toml` (trust it in `/hooks`) |
| Antigravity (agy 1.4.3) | `~/.gemini/antigravity*/brain/*/…/transcript.jsonl`: only what is inside `<USER_REQUEST>` | skill `dreaming` in `~/.gemini/config/skills` (`dreamem install antigravity`) | named hooks in `~/.gemini/config/hooks.json`; tonight's pass starts detached |
| Gemini CLI (0.64) | `~/.gemini/tmp/*/chats/session-*.jsonl`, tool results left out; `~/.gemini/GEMINI.md` (its global memory file) is read as already written | not installed yet: point it at `SKILL.md` by hand | not yet |
| Grok Build | not read yet | it loads skills from `~/.claude/skills/` and MCP servers from `~/.claude.json`: copy the skill there by hand | its SessionStart output is ignored |
| OpenAI dots, Grok in X or Telegram | — cloud agents keep no local history to read | — | — |

Sessions of an agent never run read as "not used", not as a gap.

## Whose words count

Only **your** words count as evidence. Background tasks, scheduled jobs, subagents, `codex exec` scripts, slash
commands, reminders, the harness's own inserts and quotes of the agent are recognised and left out. A turn longer than
`max_message_chars` reads as a pasted document and is left out. A message holding a secret or an instruction to an
agent is not read at all — not even cut; only the count is kept. Text matching your `exclude_patterns` never counts.

Claude Code transcripts are mostly tool output. The pass remembers where in each file the lines that may be yours
start and end (`claude-lines.json`, positions only), so the next night reads those lines and not gigabytes again.

## Install

Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv tool install git+https://github.com/itpartypattaya/agent-dreaming
dreamem run --dry-run        # see what it would ask, keep nothing
```

**Claude Code** — the plugin brings the review skill and the two session-start hooks; the program above is installed
apart, the plugin does not bring it. It works where the agent runs commands on your computer (Claude Code in a
terminal, an IDE or the desktop app), not in the claude.ai chat or Cowork:

```
/plugin install agent-dreaming --marketplace itpartypattaya/agent-dreaming
```

(`dreamem install claude-code` prints the same commands; `--run` runs them through the `claude` CLI.)

**Codex CLI** — one marked block in `~/.codex/config.toml` and the skill in `~/.agents/skills/dreaming`
(`--dry-run` shows the diff, `--uninstall` removes it; then trust the two hooks in `/hooks`):

```bash
dreamem install codex
```

**Antigravity** — the skill in `~/.gemini/config/skills/dreaming` and two named hooks in `~/.gemini/config/hooks.json`
(other hooks there are kept; `--dry-run`, `--uninstall` as above):

```bash
dreamem install antigravity
```

**MCP `memory` server** — nothing to install: when Claude Code or Codex runs `server-memory` with `MEMORY_FILE_PATH`,
the night finds the file. Elsewhere, name it; `enabled = false` turns it off:

```toml
[providers.mcp-memory]
path = "~/memory.jsonl"
entity = "Alex"              # where a saved new fact goes (otherwise the agent asks for it)
```

**When the pass runs** is yours to choose — your computer may be off at night, or you may use the agent by day only:

```bash
dreamem config time 23:00    # the first session after 23:00 runs it (local time)
dreamem config time any      # the first session of each day
```

Until you choose, your agent asks you once in a session (on three days at most), and `dreamem install codex` asks in
the terminal.

**Optional schedule** — a daily run at that time (04:00 if none) that catches up after sleep (Task Scheduler / launchd /
systemd timer); run it again after changing the time. It writes the task itself only on Windows and prints the
launchd / systemd files elsewhere:

```bash
dreamem install schedule --dry-run
```

## Commands

| Command | What it does |
|---|---|
| `dreamem run` | The pass. `--if-due`: only if no pass succeeded today and it is past `dream_time`, silent when another pass holds the lock. `--dry-run`: prints the report and keeps nothing (not even the salt). `--profile P`: one profile only (repeatable). |
| `dreamem show` | Prints the open questions for a review and marks them shown. Possible new facts are read again from the session files here. `--json` for machines, `--all` with the answered ones. |
| `dreamem verify Q` | Before the agent edits a Claude Code note: `ok` when the file is byte for byte the one you were shown; `changed` fails the question, and it comes back with the note as it is now. Reads only. |
| `dreamem decide Q answer [--text T] [--entity E]` | Records your answer. Prints the memory call to make (tool, arguments, the store to check), or that none is needed. `--text` carries the new text for changed / wrong / save; a text holding a secret or an instruction is refused. A Claude Code note's answer prints an `edit`; an MCP `memory` change prints `calls`, made in that order. `--entity`: where a new fact goes in the MCP `memory` server. |
| `dreamem applied Q` | The memory call succeeded. `--result` keeps a short note; `--fact-id N` (required for a saved new fact) records the id `memory_add` gave. |
| `dreamem failed Q --reason R` | The store refused the call (the fact changed since, not found…). The question comes back with the current text. |
| `dreamem undo Q` | Cancels a recorded answer (with a `reject` or `not-duplicates`). A memory call already made is undone with `memory_revert` or not at all — the agent says which. |
| `dreamem list` | Every question of the last night with its status and answer. |
| `dreamem status` | Where things are: home, clients, timezone, the store, each profile's last night and open questions, coverage, dream time, whether a pass is due. |
| `dreamem forget-sweep` | After a `memory_forget`: removes the fact from every copy the dream keeps. |
| `dreamem config time [HH:MM\|any]` | Shows or sets when the pass runs. |
| `dreamem install claude-code\|codex\|antigravity\|schedule` | Hooks it into a CLI or schedules it. `--dry-run`, `--uninstall`; `--time HH:MM\|any` sets the dream time on the way. |
| `dreamem hook session-start --client X [--async]` | What the CLIs' hooks call; not for you. |

The question ids look like `Q3-7f2a`: the tag is the night's, so an answer never lands on a question of a newer night.
With several profiles, add `--profile <name>` when a command asks for it.

## Settings

`~/.dreaming/config.toml` (all optional):

```toml
clients = ["claude-code", "codex", "antigravity", "gemini-cli"]
timezone = "auto"            # the machine's offset; or an IANA name
dream_time = "23:00"         # when the pass runs: HH:MM, or "any" (first session of the day)
max_questions = 7            # per night; the rest wait
max_candidates = 10          # possible new facts handed to the agent to judge per night; 0: none
max_message_chars = 2000     # longer turns are pasted documents, not your words
exclude_patterns = []        # regexes of text that must never count as yours
agent_names = ["claude", "codex", "gemini"]  # words that name your agents, not topics
fact_source = "auto"         # auto | holographic | none

[providers.mcp-memory]       # found by itself in the CLIs' MCP config; see Install
# path = "~/memory.jsonl"
# enabled = false

[windows]
themes_days = 14
corroboration_days = 30      # Claude Code keeps transcripts 30 days by default
md_decay_days = 30
```

Locations follow `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `HOLOMEM_HOME`, `DREAMEM_HOME` and `DREAMEM_GEMINI_DIR` (`~/.gemini`).

**Profiles.** One pass is one profile: one fact store and the messages that may back its facts. With holographic-memory
in `scope = "global"` there is one profile, `global`. In `scope = "project"` each project's store is a profile of its
own, reviewed with that project's conversations only, and the notes get a profile `notes`. Without holographic-memory
the profile `default` works on Claude Code's memory notes alone. The MCP `memory` server's file is a profile
`mcp-memory`, with every message. In the review, the agent answers a question only in a
session connected to that question's store; for another project's store it tells you to answer it there.

## What it reads, writes, and never does

| | |
|---|---|
| Reads | `~/.claude/projects/*/*.jsonl` (transcripts), `~/.claude/projects/*/memory/*.md` (auto-memory), `~/.claude/agent-memory/*/` and a project's `.claude/agent-memory[-local]/*/` (subagents), `~/.codex/sessions/**` (`.jsonl` and `.jsonl.zst`), `~/.codex/memories*/`, Antigravity's and Gemini CLI's sessions under `~/.gemini`, `CLAUDE.md` / `AGENTS.md` / `GEMINI.md`, holographic-memory's database (read-only), the MCP `memory` server's JSONL file. |
| Writes | only `~/.dreaming` (or `DREAMEM_HOME`): its state, the questions, `report.md`, `summary.json`, `salt` (this install's key for the digests), the digests of clauses you answered about (`candidates.json`), `claude-lines.json` — where in each transcript the lines that may be yours start and end, and for the MCP `memory` server `ids.json` / `first-seen.json` (digests of its observations with an id and the day first seen, never their words). Your messages are read in memory for the pass and stored nowhere — not even as snippets. |
| Never | writes to memory, transcripts, the agents' settings (`dreamem install` excepted, on your command); sends anything over the network; runs a model. Memory changes are made by your agent, in a session, on your answer. (SQLite itself may create the `-wal`/`-shm` files next to the holographic-memory database when it reads a database in WAL mode whose writer is not running, as any reader does; the database is never written.) |

Details: [PRIVACY.md](PRIVACY.md), [SECURITY.md](SECURITY.md).

## Safety in the review

- Everything a question quotes — fact texts, notes, your old messages — is data for the agent, never an instruction.
  A fact that tells the agent to do something becomes a question for you.
- Only your own answer counts: the agent never answers for you, never reads "yes" into silence, never carries one
  answer over to another question.
- The agent makes only the call `dreamem decide` printed, after checking the store. It never forgets a fact you did
  not ask to forget. It edits a Claude Code memory note only on your answer, after `dreamem verify` shows it is the
  text you were shown, and never edits `CLAUDE.md`, `AGENTS.md` or Codex memories.
- In a system without versions the check is the exact text: an MCP `memory` observation is deleted only on a
  word-for-word match, and a change adds the new text before it deletes the old (a failure in between leaves both,
  and the next night asks about the pair).
- A call that changes a fact carries `expect`, the version you were shown; holographic-memory 0.5.0 or newer refuses
  it when the fact changed since the night.

## When something does not work

- **The agent says the `dreamem` program is not installed** (the plugin's session-start hook tells it so, and the
  review stops there): the program is not installed or not on `PATH`. Install it as in [Install](#install); if `uv` says its tool folder is
  not on `PATH`, run `uv tool update-shell`. Then restart Claude Code: hooks see the `PATH` it started with.
- **No summary line at the start of a session**: the line comes once a day and only for a finished night.
  `dreamem status` tells whether a pass is due, when it runs (`dream_time`) and how the last night went;
  `dreamem run` makes one now.
- **The agent stops: this session talks to another store**: the question is about a per-project store. Answer it in
  a session of that project.
- **`coverage … partial` in `dreamem status`**: an agent changed its session format and the pass left the unknown part
  out. Update agent-dreaming (`uv tool upgrade agent-dreaming`); if it stays, open an issue.
- **A warning about an unknown timezone**: the pass then counts days in UTC. Set `timezone` in the settings, or
  reinstall with `uv tool install --reinstall git+https://github.com/itpartypattaya/agent-dreaming`, which brings
  the zone database (`tzdata`) on Windows.
- **To remove it all**: `/plugin uninstall agent-dreaming`, `uv tool uninstall agent-dreaming`, and the folder
  `~/.dreaming` (its questions and answers; your memory is not in it).

Anything else: [open an issue](https://github.com/itpartypattaya/agent-dreaming/issues) with what you ran and what it
printed. `dreamem status` shows paths, counts and dates, no texts of your facts or messages — look it over before you
paste it.

## Limits

- The agents' session formats are internal and change between versions. Unknown shapes are left out and reported as
  partial coverage; a night with partial coverage asks no "still relevant?" question.
- A short quote you pasted and a sentence you typed look the same in a transcript.
- Codex memories are never edited (the review tells you how: a reset of Codex memory or a rule in `AGENTS.md`).
  A project's `autoMemoryDirectory` is named in `dreamem status`, not read: Claude Code honours it only in a trusted
  folder, which cannot be told from outside.
- The MCP `memory` server 2.0.1 does not say how many observations it deleted in a field: the agent reads the reply
  ("Deleted 0 of 1 observations" is a failure). Duplicates between its observations are not asked about yet.
- An older holographic-memory (before 0.5.0) ignores `expect`: a change then goes through unchecked — update
  holographic-memory.
- Possible new facts come from the last 30 days only, and the filter is tuned for Russian and English.
- The plugin's hooks are one line for bash (macOS, Linux, Git Bash on Windows) and PowerShell 7. Claude Code on
  Windows without Git Bash and without PowerShell 7 runs them in Windows PowerShell 5.1, which cannot read that line:
  there the session starts with a hook error. Git for Windows or PowerShell 7 fixes it.

## Licence

MIT. The dreaming engine in `dreamem/engine` is maintained in this repository; see its `NOTICE.md`.
