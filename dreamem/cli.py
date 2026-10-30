"""``dreamem`` — the command line of agent-dreaming."""
from __future__ import annotations

import argparse
import sys

from . import __version__, config, hooks, install, review, run
from .paths import homes


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def set_time(h, value: str | None) -> int:
    if value is None:
        chosen = config.load(h)[0].get("dream_time")
        print(f"dream time: {chosen or 'not chosen'}"
              f"{' (the first session of each day)' if chosen in ('', config.ANY_TIME) else ''}")
        return 0
    try:
        kept = config.set_time(h, value)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"dream time: {kept}" + (" — the first session of each day" if kept == config.ANY_TIME else
                                   " — the first session after it runs the pass"))
    print("If you installed the schedule, run `dreamem install schedule` again to move it to this time.")
    return 0


def build() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="dreamem", description="A nightly, read-only memory review for Claude Code and "
                                 "Codex CLI. Nothing outside ~/.dreaming is ever written by it.")
    ap.add_argument("--version", action="version", version=f"dreamem {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="the night's pass (seconds, no model, no network)")
    p.add_argument("--if-due", action="store_true", help="only if no pass succeeded today; silent when busy")
    p.add_argument("--dry-run", action="store_true", help="print the report; keep nothing")
    p.add_argument("--profile", action="append", help="only this profile (repeatable)")

    p = sub.add_parser("show", help="print the open questions for a review (marks them shown)")
    p.add_argument("--profile")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="answered ones too")

    p = sub.add_parser("decide", help="record the person's answer to a question")
    p.add_argument("qid")
    p.add_argument("answer")
    p.add_argument("--text", help="the new text, for changed / wrong")
    p.add_argument("--profile")
    for name, help_ in (("applied", "the memory call of a decided question succeeded"),
                        ("failed", "the memory call was refused; the question comes back"),
                        ("undo", "cancel a recorded answer (memory is not touched)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("qid")
        p.add_argument("--profile")
        if name == "applied":
            p.add_argument("--result", help="what the tool answered (kept for the record)")
        if name == "failed":
            p.add_argument("--reason", default="failed")
    p = sub.add_parser("list", help="every question of the last night with its status")
    p.add_argument("--profile")

    p = sub.add_parser("config", help="show or set a setting of ~/.dreaming/config.toml")
    p.add_argument("key", choices=["time"], help="time: when the night's pass runs")
    p.add_argument("value", nargs="?", help="HH:MM (24-hour, local) or 'any' (the first session of each day)")

    sub.add_parser("status", help="where things are, the last night, what is due")
    sub.add_parser("forget-sweep", help="remove facts forgotten since the night from every copy")

    p = sub.add_parser("hook", help="for the CLIs' hooks")
    p.add_argument("event", choices=["session-start"])
    p.add_argument("--client", choices=["claude-code", "codex"], required=True)
    p.add_argument("--async", dest="background", action="store_true", help="run --if-due, print {}")

    p = sub.add_parser("install", help="hook it into Claude Code or Codex, or schedule it")
    p.add_argument("target", choices=["claude-code", "codex", "schedule"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--run", dest="run_it", action="store_true", help="claude-code: run the plugin commands")
    p.add_argument("--time", help="when the pass runs (HH:MM or 'any'); without it, a terminal asks")
    return ap


def main(argv=None) -> int:
    _utf8()
    args = build().parse_args(argv)
    h = homes()
    if args.cmd == "run":
        return run.run(h, if_due=args.if_due, dry_run=args.dry_run, only=args.profile)
    if args.cmd == "show":
        return review.show(h, profile=args.profile, as_json=args.json, include_all=args.all)
    if args.cmd == "decide":
        return review.decide(h, args.qid, args.answer, text=args.text, profile=args.profile)
    if args.cmd in ("applied", "failed", "undo"):
        detail = getattr(args, "result", None) or getattr(args, "reason", None)
        return review.mark(h, args.qid, args.cmd, detail=detail, profile=args.profile)
    if args.cmd == "list":
        return review.listing(h, profile=args.profile)
    if args.cmd == "config":
        return set_time(h, args.value)
    if args.cmd == "status":
        return run.status(h)
    if args.cmd == "forget-sweep":
        return run.forget_sweep(h)
    if args.cmd == "hook":
        return hooks.session_start(h, args.client, background=args.background)
    if args.cmd == "install":
        if args.time and not args.uninstall and set_time(h, args.time):
            return 2
        if args.target == "codex":
            return install.install_codex(h, remove=args.uninstall, dry_run=args.dry_run)
        if args.target == "claude-code":
            return install.install_claude(remove=args.uninstall, dry_run=args.dry_run, run_it=args.run_it)
        return install.install_schedule(h, remove=args.uninstall, dry_run=args.dry_run)
    return 2


if __name__ == "__main__":
    sys.exit(main())
