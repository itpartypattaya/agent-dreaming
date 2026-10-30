"""``dreamem install claude-code | codex | schedule [--dry-run] [--uninstall]``.

Claude Code: the plugin is the one way in (``/agent-dreaming:dream`` and its two hooks come with it); this command
prints — or runs, with ``--run`` — the two ``claude plugin`` commands. Nothing is written into settings by hand, so the
hooks cannot be registered twice.

Codex CLI: one marked block at the end of ``~/.codex/config.toml`` with the two SessionStart hooks, and the skill
``dreaming`` in ``~/.agents/skills/dreaming/`` (``$dreaming``). A file is backed up next to itself before it changes,
the new TOML must parse, ``--dry-run`` shows the diff and writes nothing, ``--uninstall`` removes exactly what was
added. Codex runs a hook only once it is trusted: open ``/hooks`` in Codex and trust the two.

Both ask for the time of the pass when none is chosen: ``install codex`` in a terminal, the plugin through the agent
in the first session (the SessionStart hook asks it to).

Schedule (optional): a daily run at the chosen time (04:00 if none) that catches up after sleep — Task Scheduler on
Windows (``StartWhenAvailable``), launchd on macOS, a systemd user timer with ``Persistent=true`` on Linux. ``--dry-run`` prints it.
"""
from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from importlib import resources
from pathlib import Path

from . import config
from .paths import Homes

MARK_BEGIN = "# >>> agent-dreaming (dreamem install codex) >>>"
MARK_END = "# <<< agent-dreaming <<<"
MARKETPLACE = "itpartypattaya/agent-dreaming"
PLUGIN = "agent-dreaming@agent-dreaming"
TASK = "agent-dreaming"
SKILL_MARK = "dreamem decide"  # in our skill's text, in no other skill's: what install may replace or remove


def python_path() -> str:
    return Path(sys.executable).as_posix()


def command(*args: str) -> str:
    """For a POSIX-style shell (Git Bash on Windows): the interpreter quoted, forward slashes, ``-I``."""
    return " ".join([f'"{python_path()}"', "-I", "-m", "dreamem", *args])


def windows_command(*args: str) -> str:
    """For PowerShell (Codex's ``command_windows``): a quoted path followed by arguments is a parse error there, so the
    path goes bare, or after the call operator when it holds a space."""
    python = str(Path(sys.executable))
    head = f'& "{python}"' if " " in python else python
    return " ".join([head, "-I", "-m", "dreamem", *args])


def skill_text(name: str) -> str:
    text = resources.files("dreamem").joinpath("skill/SKILL.md").read_text(encoding="utf-8")
    return text.replace("name: dream\n", f"name: {name}\n", 1)


def _toml(value: str) -> str:
    return json.dumps(value)


def codex_block() -> str:
    lines = [MARK_BEGIN]
    for extra, label, timeout, background in (([], "dreaming: last night", 10, False),
                                              (["--async"], "dreaming: tonight's pass", 600, True)):
        args = ["hook", "session-start", "--client", "codex", *extra]
        lines += ["[[hooks.SessionStart]]", "[[hooks.SessionStart.hooks]]", 'type = "command"',
                  f"command = {_toml(command(*args))}", f"command_windows = {_toml(windows_command(*args))}",
                  f"timeout = {timeout}", f"statusMessage = {_toml(label)}"]
        if background:
            lines.append("async = true")
        lines.append("")
    lines.append(MARK_END)
    return "\n".join(lines) + "\n"


def codex_text(current: str, *, remove: bool = False) -> str:
    text = current
    start = text.find(MARK_BEGIN)
    if start != -1:
        end = text.find(MARK_END, start)
        end = len(text) if end == -1 else end + len(MARK_END)
        head, tail = text[:start].rstrip("\n"), text[end:].lstrip("\n")
        text = (head + "\n" + tail) if head.strip() else tail
        if head.strip() and not tail:
            text = head + "\n"
    if not remove:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + codex_block()
    return text


def _diff(path: Path, old: str, new: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), str(path), str(path)))


def _write(path: Path, old: str, new: str, dry_run: bool, out) -> bool:
    diff = _diff(path, old, new)
    if not diff:
        print(f"{path}: no change", file=out)
        return False
    print(diff, file=out)
    if dry_run:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        print(f"backup: {backup}", file=out)
    path.write_text(new, encoding="utf-8", newline="\n")
    return True


TIME_QUESTION = ("What time of day suits the nightly pass? A time when this computer is usually on "
                 "(HH:MM, 24-hour; Enter: the first session of each day): ")


def _interactive(question: str) -> str:
    if not (sys.stdin and sys.stdin.isatty()):
        raise EOFError  # not a terminal: the agent asks in the first session instead
    return input(question)


def ask_time(homes: Homes, prompt, out) -> None:
    """Ask for ``dream_time`` when none is chosen; an empty answer is "no preference" (``any``)."""
    if config.load(homes)[0].get("dream_time"):
        return
    for _ in range(3):
        try:
            answer = prompt(TIME_QUESTION).strip()
        except EOFError:
            return
        answer = answer or config.ANY_TIME
        try:
            print(f"dream time: {config.set_time(homes, answer)}", file=out)
            return
        except ValueError as exc:
            print(exc, file=out)


def install_codex(homes: Homes, *, remove: bool, dry_run: bool, out=sys.stdout, prompt=_interactive) -> int:
    path = homes.codex / "config.toml"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    new = codex_text(old, remove=remove)
    try:
        tomllib.loads(new)
    except tomllib.TOMLDecodeError as exc:
        print(f"{path}: the result would not parse ({exc}); nothing was changed", file=out)
        return 1
    written = _write(path, old, new, dry_run, out)
    skill = Path.home() / ".agents" / "skills" / "dreaming" / "SKILL.md"
    old_skill = skill.read_text(encoding="utf-8", errors="replace") if skill.exists() else ""
    if old_skill and SKILL_MARK not in old_skill:
        # Another skill of that name (hermes-dreaming has one): never overwritten, never removed.
        print(f"{skill}: a skill named 'dreaming' that is not ours is there — left as it is; "
              f"{'nothing removed' if remove else 'install this one by hand under another name'}", file=out)
    elif remove:
        if skill.exists() and not dry_run:
            skill.unlink()
            for backup in skill.parent.glob("SKILL.md.bak-*"):
                backup.unlink()
            if not any(skill.parent.iterdir()):
                skill.parent.rmdir()
        print(f"{'would remove' if dry_run else 'removed'}: {skill}", file=out)
    else:
        _write(skill, old_skill, skill_text("dreaming"), dry_run, out)
    if not remove and not dry_run:
        ask_time(homes, prompt, out)
    if written and not remove:
        print("Codex runs a hook only once it is trusted: open /hooks in Codex and trust the two dreaming hooks.",
              file=out)
    return 0


def install_claude(*, remove: bool, dry_run: bool, run_it: bool, out=sys.stdout) -> int:
    cmds = ([["claude", "plugin", "uninstall", PLUGIN]] if remove else
            [["claude", "plugin", "marketplace", "add", MARKETPLACE], ["claude", "plugin", "install", PLUGIN]])
    cli = shutil.which("claude")
    for cmd in cmds:
        shown = " ".join(cmd)
        if dry_run or not run_it or not cli:
            print(("run: " if cli else "run (claude not on PATH here): ") + shown, file=out)
            continue
        result = subprocess.run([cli, *cmd[1:]], capture_output=True, text=True, timeout=120)
        print(f"{'ran' if result.returncode == 0 else 'failed'}: {shown}", file=out)
        if result.returncode != 0:
            print((result.stderr or result.stdout).strip()[-500:], file=out)
            return 1
    if not remove:
        print("The plugin calls `dreamem` from PATH: install it with `uv tool install "
              "git+https://github.com/itpartypattaya/agent-dreaming`.", file=out)
    return 0


def schedule_spec(platform: str | None = None, at: str = "04:00") -> tuple[str, str]:
    """(file name, content) of the daily job for this platform, at *at* (HH:MM, local time)."""
    platform = platform or sys.platform
    hour, minute = config.dream_time(at) or (4, 0)
    if platform == "win32":
        python = str(Path(sys.executable))
        return (f"{TASK}.xml", f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>agent-dreaming: the nightly memory review (read-only)</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>2026-01-01T{hour:02d}:{minute:02d}:00</StartBoundary><Enabled>true</Enabled>
    <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay></CalendarTrigger></Triggers>
  <Settings><StartWhenAvailable>true</StartWhenAvailable><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy></Settings>
  <Actions><Exec><Command>{python}</Command><Arguments>-I -m dreamem run --if-due</Arguments></Exec></Actions>
</Task>
""")
    if platform == "darwin":
        return ("com.agent-dreaming.plist", f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.agent-dreaming</string>
  <key>ProgramArguments</key><array><string>{python_path()}</string><string>-I</string><string>-m</string>
    <string>dreamem</string><string>run</string><string>--if-due</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>{hour}</integer><key>Minute</key><integer>{minute}</integer></dict>
</dict></plist>
""")
    return ("agent-dreaming.service + agent-dreaming.timer", f"""# ~/.config/systemd/user/agent-dreaming.service
[Unit]
Description=agent-dreaming nightly memory review (read-only)
[Service]
Type=oneshot
ExecStart={python_path()} -I -m dreamem run --if-due

# ~/.config/systemd/user/agent-dreaming.timer
[Timer]
OnCalendar=*-*-* {hour:02d}:{minute:02d}:00
Persistent=true
[Install]
WantedBy=timers.target
""")


def install_schedule(homes: Homes, *, remove: bool, dry_run: bool, out=sys.stdout) -> int:
    chosen = config.load(homes)[0].get("dream_time")
    name, content = schedule_spec(at=chosen if config.dream_time(chosen) else "04:00")
    if sys.platform != "win32":
        print(f"{name}:\n{content}", file=out)
        print("Put these files in place and enable them yourself (launchctl load / systemctl --user enable --now "
              "agent-dreaming.timer); this command writes them only on Windows.", file=out)
        return 0
    if remove:
        cmd = ["schtasks", "/Delete", "/TN", TASK, "/F"]
    else:
        xml = homes.dreaming / name
        if not dry_run:
            homes.dreaming.mkdir(parents=True, exist_ok=True)
            xml.write_text(content, encoding="utf-16")
        cmd = ["schtasks", "/Create", "/TN", TASK, "/XML", str(xml), "/F"]
    print(("would run: " if dry_run else "run: ") + " ".join(cmd), file=out)
    if dry_run:
        if not remove:
            print(content, file=out)
        return 0
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                            encoding=os.device_encoding(1) or "utf-8", errors="replace")
    print((result.stdout or result.stderr).strip(), file=out)
    return 0 if result.returncode == 0 else 1
