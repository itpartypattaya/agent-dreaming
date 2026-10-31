"""What people said to the agents: messages read from the CLIs' own session files, never written.

Both formats are internal to their CLIs and change between versions, so every reader is defensive: a line it cannot
parse is skipped, a combination of fields it does not know is "unknown" and never counts as a person's words
(fail-closed), and what it could not read is reported as coverage — a night with half the sessions unread must not
turn into "nobody mentioned this for a month".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUMAN, MACHINE, UNKNOWN = "human", "machine", "unknown"
COMPLETE, PARTIAL, UNAVAILABLE = "complete", "partial", "unavailable"
# Above this share of user-text lines in field combinations we do not know, the reader no longer knows the format.
UNKNOWN_SHARE_PARTIAL = 0.10


@dataclass
class Message:
    client: str          # "claude-code" | "codex"
    session_id: str
    ts: float            # Unix time
    text: str
    cwd: str = ""
    key: str = ""        # identity across files (a resumed or forked session repeats the same line)
    source: str = ""     # which session file: Claude Code "<project>/<session>.jsonl", Codex the rollout's stem
    below: str = ""      # Claude Code: the version a line without turnOrigin was older than (why it counted)


@dataclass
class Coverage:
    client: str
    status: str = COMPLETE
    files: int = 0
    read: int = 0
    messages: int = 0
    reasons: dict = field(default_factory=dict)
    kinds: dict = field(default_factory=dict)

    def note(self, reason: str, n: int = 1) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + n

    def count(self, kind: str) -> None:
        self.kinds[kind] = self.kinds.get(kind, 0) + 1

    def partial(self, reason: str) -> None:
        self.note(reason)
        if self.status == COMPLETE:
            self.status = PARTIAL

    def finish(self) -> Coverage:
        known = sum(self.kinds.values())
        if known and self.kinds.get(UNKNOWN, 0) / known > UNKNOWN_SHARE_PARTIAL:
            self.partial("unknown_share")
        return self

    def as_dict(self) -> dict:
        return {"client": self.client, "status": self.status, "files": self.files, "read": self.read,
                "messages": self.messages, "reasons": dict(sorted(self.reasons.items())),
                "kinds": dict(sorted(self.kinds.items()))}


# Inserts a CLI or an app puts into the user's turn: not the person's words. A block is removed; a text that is
# nothing but such a block (or starts with a notice tag) is no speech at all.
_BLOCK_RE = re.compile(
    r"<(system-reminder|pasted_content|environment_context|user_instructions|turn_aborted|local-command-stdout"
    r"|local-command-stderr|local-command-caveat|command-name|command-message|command-args|ide_selection"
    r"|ide_opened_file)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_NOTICE_START_RE = re.compile(
    r"^\s*(?:<(?:task-notification|ci-monitor-event|scheduled-task|side-sessions-event|artifact-comment|bash-input"
    r"|bash-stdout|bash-stderr|user-prompt-submit-hook)\b|\[Request interrupted|# AGENTS\.md instructions"
    r"|Caveat: The messages below)", re.IGNORECASE)
_ONLY_TAG_RE = re.compile(r"^\s*<([A-Za-z][\w:-]*)\b[^>]*>.*</\1>\s*$", re.DOTALL)
_REPLY_MARK_RE = re.compile(r"^\s*<!--\s*(?:reply(?:\s+\d+)?|attach)\s*-->\s*$", re.IGNORECASE)


def clean_text(text: str) -> str:
    """The person's own words of a user turn: notices, reminders, pasted blocks and quoted lines (a reply quoting the
    agent: ``> …``) removed. Empty when nothing of the person is left."""
    if not text or _NOTICE_START_RE.match(text):
        return ""
    text = _BLOCK_RE.sub("", text)
    if _ONLY_TAG_RE.match(text):
        return ""
    lines = [ln for ln in text.splitlines() if not _REPLY_MARK_RE.match(ln) and not ln.lstrip().startswith(">")]
    return "\n".join(lines).strip()
