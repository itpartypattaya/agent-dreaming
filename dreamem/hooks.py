"""SessionStart hooks, two of them, for both CLIs.

- ``dreamem hook session-start --client X`` (synchronous, well under a second): one line about the last finished night
  — counts only, never a fact's text — once a day, or again the same day when a newer night is there. Marked as
  shown only after it is printed. While no ``dream_time`` is chosen it also asks the agent to ask the person for one
  (once a day, on three days at most), from the very first session after install, with or without a night.
- ``dreamem hook session-start --client X --async`` (``async: true`` in the hook definition): ``dreamem run --if-due``
  in the background, printing an empty JSON object. Codex cancels a background hook when its session ends and Claude
  Code drops its output on ``/clear``: neither matters, the next session shows the new night.

The output is the JSON both CLIs take: ``{"hookSpecificOutput": {"hookEventName": "SessionStart",
"additionalContext": ...}}``. Any failure prints ``{}`` and exits 0: a hook must never break a session.
"""
from __future__ import annotations

import io
import json
import sys

from . import config, report, run
from .fsutil import read_json, write_json
from .lock import Lock
from .paths import Homes

ASK_DAYS = 3  # days on which the agent asks for a time before the default simply stays
ASK_TIME = (
    "agent-dreaming does not know yet when the person wants its nightly memory pass. In your first reply, ask them once, "
    "in one short line in their language, what time of day suits it: a time when this computer is usually on and they "
    "use an agent soon after (the pass reads recent sessions for about half a minute in the background, at the first "
    "session after that time; it changes no memory by itself). When they name a time, run `dreamem config time HH:MM` "
    "(24-hour, local time); if they have no preference, run `dreamem config time any` (the first session of each day). "
    "If they do not answer, do not press: it will be asked again on another day, three days at most.")


def newest_night(summary: dict) -> float:
    """When the newest night was published (two nights may share a `generated_at` second)."""
    return max((float(e.get("published") or 0) for e in (summary.get("profiles") or {}).values()
                if isinstance(e, dict)), default=0.0)


def _ask_time(homes: Homes, summary: dict) -> bool:
    """No time chosen yet, not asked today, asked on fewer than ASK_DAYS days."""
    if config.load(homes)[0].get("dream_time"):
        return False
    asked = summary.get("time_asked") or {}
    return asked.get("on") != run.today() and int(asked.get("days") or 0) < ASK_DAYS


def session_start(homes: Homes, client: str, *, background: bool = False, out=sys.stdout) -> int:
    try:
        if background:
            run.run(homes, if_due=True, out=io.StringIO())
            out.write("{}\n")
            return 0
        summary = read_json(homes.summary, {}) or {}
        night = newest_night(summary)
        shown = summary.get("shown") or {}
        line = None
        if not (shown.get("on") == run.today() and float(shown.get("night") or 0) >= night):
            line = report.summary_line(summary.get("profiles") or {})
        ask = _ask_time(homes, summary)
        if not line and not ask:
            out.write("{}\n")
            return 0
    except Exception:  # noqa: BLE001 - a hook must never break a session
        out.write("{}\n")
        return 0
    text = "\n\n".join(t for t in (line, ASK_TIME if ask else None) if t)
    out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}},
                         ensure_ascii=False) + "\n")
    out.flush()
    try:  # bookkeeping after the one answer: whatever fails here prints nothing more
        lock = Lock(homes.lock, wait=0.5)
        if lock.acquire():  # a pass running now writes its own summary; the next session shows that night
            try:
                summary = read_json(homes.summary, {}) or {}
                if line:
                    summary["shown"] = {"on": run.today(), "night": night}
                if ask:
                    days = int((summary.get("time_asked") or {}).get("days") or 0)
                    summary["time_asked"] = {"on": run.today(), "days": days + 1}
                write_json(homes.summary, summary)
            finally:
                lock.release()
    except Exception:  # noqa: BLE001
        pass
    return 0
