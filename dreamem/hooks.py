"""SessionStart hooks, two of them, for both CLIs.

- ``dreamem hook session-start --client X`` (synchronous, well under a second): one line about the last finished night
  — counts only, never a fact's text — once a day, or again the same day when a newer night is there. Marked as
  shown only after it is printed.
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

from . import report, run
from .fsutil import read_json, write_json
from .lock import Lock
from .paths import Homes


def newest_night(summary: dict) -> float:
    """When the newest night was published (two nights may share a `generated_at` second)."""
    return max((float(e.get("published") or 0) for e in (summary.get("profiles") or {}).values()
                if isinstance(e, dict)), default=0.0)


def session_start(homes: Homes, client: str, *, background: bool = False, out=sys.stdout) -> int:
    try:
        if background:
            run.run(homes, if_due=True, out=io.StringIO())
            out.write("{}\n")
            return 0
        summary = read_json(homes.summary, {}) or {}
        night = newest_night(summary)
        shown = summary.get("shown") or {}
        if shown.get("on") == run.today() and float(shown.get("night") or 0) >= night:
            out.write("{}\n")
            return 0
        line = report.summary_line(summary.get("profiles") or {})
        if not line:
            out.write("{}\n")
            return 0
    except Exception:  # noqa: BLE001 - a hook must never break a session
        out.write("{}\n")
        return 0
    out.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": line}},
                         ensure_ascii=False) + "\n")
    out.flush()
    try:  # bookkeeping after the one answer: whatever fails here prints nothing more
        lock = Lock(homes.lock, wait=0.5)
        if lock.acquire():  # a pass running now writes its own summary; the next session shows that night
            try:
                summary = read_json(homes.summary, {}) or {}
                summary["shown"] = {"on": run.today(), "night": night}
                write_json(homes.summary, summary)
            finally:
                lock.release()
    except Exception:  # noqa: BLE001
        pass
    return 0
