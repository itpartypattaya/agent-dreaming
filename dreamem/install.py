"""``dreamem install claude-code | codex | schedule [--dry-run] [--uninstall]``.

Claude Code: the plugin brings the review skill ``/agent-dreaming:dream`` and nothing else — since 0.8.0 no hooks:
the Anthropic directory refuses hooks of any type from organizations that are not Verified. This command prints — or
runs, with ``--run`` — the ``claude plugin`` commands that install the plugin or bring it up to date, then sets up the daily task below, which now runs the pass
for Claude Code (``--uninstall`` removes both). Nothing is written into Claude Code's settings.

Codex CLI: one marked block at the end of ``~/.codex/config.toml`` with the two SessionStart hooks, and the skill
``dreaming`` in ``~/.agents/skills/dreaming/`` (``$dreaming``). A file is backed up next to itself before it changes,
the new TOML must parse, ``--dry-run`` shows the diff and writes nothing, ``--uninstall`` removes exactly what was
added. Codex runs a hook only once it is trusted: open ``/hooks`` in Codex and trust the two.

Both ask for the time of the pass when none is chosen, in a terminal (``install codex``, ``install claude-code``);
Codex's hook also asks the agent to ask in the first session.

Antigravity: the skill ``dreaming`` in ``~/.gemini/config/skills/dreaming/`` and two named SessionStart hooks in
``~/.gemini/config/hooks.json`` (the global customization root of agy 1.4.3): one tells the agent how many questions
wait, the other starts tonight's pass detached (a hook there has 30 seconds and no background mode). Other hooks in
that file are kept as they are; ``--uninstall`` removes only ours.

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
import re
import tomllib
from importlib import resources
from pathlib import Path
from xml.sax.saxutils import escape

from . import config
from .paths import Homes

MARK_BEGIN = "# >>> agent-dreaming (dreamem install codex) >>>"
MARK_END = "# <<< agent-dreaming <<<"
MARKETPLACE = "itpartypattaya/agent-dreaming"
MARKETPLACE_NAME = "agent-dreaming"  # the name in .claude-plugin/marketplace.json
PLUGIN = f"agent-dreaming@{MARKETPLACE_NAME}"
TASK = "agent-dreaming"
# Where the program finds its folders (paths.homes); a task the system starts sees only the user's own settings.
NOTHING_RUNS = "nightly run: nothing starts it on its own — run `dreamem install schedule`"
FOLDER_VARIABLES = ("DREAMEM_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "HOLOMEM_HOME", "DREAMEM_GEMINI_DIR")
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


def _our_hooks(text: str) -> list | None:
    """Each dreaming hook as Codex reads the file — its place (group, hook) and its definition, what Codex keeps trust
    by — or None when the file does not parse. Read from the parsed TOML, not the text: a header written with spaces
    or a comment is the same hook (Codex review of 0.3.3)."""
    try:
        groups = (tomllib.loads(text).get("hooks") or {}).get("SessionStart") or []
    except (tomllib.TOMLDecodeError, AttributeError):
        return None
    out = []
    for i, group in enumerate(groups if isinstance(groups, list) else []):
        hooks = group.get("hooks") if isinstance(group, dict) else None
        for j, hook in enumerate(hooks if isinstance(hooks, list) else []):
            if isinstance(hook, dict) and "dreamem hook session-start" in str(hook.get("command", "")):
                out.append((i, j, hook))
    return out


def _foreign(inner: str) -> str:
    """What is not ours between the marks. Codex appends a new table at the end of its file — inside our block, before
    its end mark (``[tui] screen_reader_detection_done = true``, seen live): kept, never removed with the block. Ours is
    each ``[[hooks.SessionStart.hooks]]`` table running ``dreamem hook session-start`` and the empty
    ``[[hooks.SessionStart]]`` header before it, whatever Python path an older install wrote."""
    chunks: list[list[str]] = [[]]
    for line in inner.splitlines():
        if line.lstrip().startswith("["):
            chunks.append([])
        chunks[-1].append(line)
    def header(chunk: list[str]) -> str:
        """The table a header names, as TOML reads it: no trailing comment, no spaces (Codex review of PR #4)."""
        return "".join(chunk[0].split("#", 1)[0].split()) if chunk else ""

    ours = [header(c) == "[[hooks.SessionStart.hooks]]" and
            any("dreamem hook session-start" in line for line in c) for c in chunks]
    for i, c in enumerate(chunks):
        if (header(c) == "[[hooks.SessionStart]]" and not any(line.strip() for line in c[1:])
                and i + 1 < len(chunks) and ours[i + 1]):
            ours[i] = True
    kept = "\n".join(line for c, mine in zip(chunks, ours, strict=True) if not mine for line in c)
    return kept.strip("\n")


def codex_text(current: str, *, remove: bool = False) -> str:
    text = current
    start = text.find(MARK_BEGIN)
    foreign = ""
    if start != -1:
        end = text.find(MARK_END, start)
        inner_end = len(text) if end == -1 else end
        foreign = _foreign(text[start + len(MARK_BEGIN):inner_end])
        end = len(text) if end == -1 else end + len(MARK_END)
        head, tail = text[:start].rstrip("\n"), text[end:].lstrip("\n")
        rest = "\n\n".join(p for p in (foreign, tail.rstrip("\n")) if p)
        if remove:
            text = "\n\n".join(p for p in (head, rest) if p.strip())
            return text + "\n" if text.strip() else ""
        text = head + ("\n" if head.strip() else "")
        tail = rest
    else:
        tail = ""
    if not remove:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + codex_block()
        if tail:
            text += "\n" + tail.rstrip("\n") + "\n"
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


def _terminal() -> bool:
    """A person at a console on the other end of stdin. ``isatty()`` alone is not enough on Windows: the NUL device
    (``< /dev/null``, ``stdin=DEVNULL``) is a character device and says True; only a console has a console mode."""
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
        if sys.platform != "win32":
            return True
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        return bool(ctypes.windll.kernel32.GetConsoleMode(ctypes.c_void_p(handle), ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def _interactive(question: str) -> str:
    if not _terminal():
        raise EOFError  # not a terminal: the time stays unchosen (the task runs at 04:00, Codex's hook asks)
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
    _skill(Path.home() / ".agents" / "skills" / "dreaming" / "SKILL.md", remove=remove, dry_run=dry_run, out=out)
    if not remove and not dry_run:
        ask_time(homes, prompt, out)
    before = _our_hooks(old)
    if written and not remove and (before is None or before != _our_hooks(new)):
        # Trust is kept per hook by its definition and its place: only new, rewritten or moved hooks need it again.
        print("Codex runs a hook only once it is trusted: open /hooks in Codex and trust the two dreaming hooks.",
              file=out)
    return 0


def _skill(skill: Path, *, remove: bool, dry_run: bool, out) -> None:
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


AG_HOOKS = ("agent-dreaming", "agent-dreaming-pass")  # our named hooks in Antigravity's hooks.json


def cmd_command(*args: str) -> str:
    """For ``cmd /c`` (Antigravity on Windows) or ``sh -c``: the interpreter quoted, ``-I``."""
    python = str(Path(sys.executable)) if sys.platform == "win32" else python_path()
    return " ".join([f'"{python}"', "-I", "-m", "dreamem", *args])


def ours(entry) -> bool:
    """A named hook we wrote: every handler runs our session-start hook for Antigravity."""
    handlers = entry.get("SessionStart") if isinstance(entry, dict) else None
    return bool(handlers) and set(entry) == {"SessionStart"} and all(
        isinstance(h, dict) and "-m dreamem hook session-start --client antigravity" in str(h.get("command"))
        for h in handlers)


def antigravity_hooks(current: dict, *, remove: bool = False) -> dict:
    """*current* with our two named hooks (re)written or, with *remove*, gone. A hook of one of our names that is not
    ours raises ValueError: it is never overwritten or removed."""
    foreign = [k for k in AG_HOOKS if k in current and not ours(current[k])]
    if foreign:
        raise ValueError(f"a hook named {', '.join(foreign)} that is not ours is there")
    out = {k: v for k, v in current.items() if k not in AG_HOOKS}
    if not remove:
        for name, extra, timeout in ((AG_HOOKS[0], [], 10), (AG_HOOKS[1], ["--async"], 10)):
            out[name] = {"SessionStart": [{"type": "command", "timeout": timeout,
                                           "command": cmd_command("hook", "session-start", "--client", "antigravity",
                                                                  *extra)}]}
    return out


def install_antigravity(homes: Homes, *, remove: bool, dry_run: bool, out=sys.stdout, prompt=_interactive) -> int:
    root = homes.gemini / "config"
    path = root / "hooks.json"
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    try:
        current = json.loads(old) if old.strip() else {}
    except ValueError as exc:
        print(f"{path}: not JSON ({exc}); nothing was changed", file=out)
        return 1
    if not isinstance(current, dict):
        print(f"{path}: not a JSON object; nothing was changed", file=out)
        return 1
    try:
        hooks = antigravity_hooks(current, remove=remove)
    except ValueError as exc:
        print(f"{path}: {exc} — nothing was changed", file=out)
        return 1
    new = json.dumps(hooks, ensure_ascii=False, indent=2) + "\n" if hooks or old else ""
    if remove and not hooks and old:
        new = "{}\n"
    _write(path, old, new, dry_run, out)
    _skill(root / "skills" / "dreaming" / "SKILL.md", remove=remove, dry_run=dry_run, out=out)
    if not remove and not dry_run:
        ask_time(homes, prompt, out)
    return 0


def install_claude(homes: Homes, *, remove: bool, dry_run: bool, run_it: bool, out=sys.stdout,
                   prompt=_interactive, at: str | None = None) -> int:
    # `install` on a plugin already there does nothing, so an installed plugin is brought up to date by `update`; both
    # answer 0 when there is nothing to do (checked on Claude Code 2.1.x).
    cmds = ([["claude", "plugin", "uninstall", PLUGIN]] if remove else
            [["claude", "plugin", "marketplace", "add", MARKETPLACE],
             ["claude", "plugin", "marketplace", "update", MARKETPLACE_NAME],
             ["claude", "plugin", "install", PLUGIN], ["claude", "plugin", "update", PLUGIN]])
    cli = shutil.which("claude")
    for cmd in cmds:
        shown = " ".join(cmd)
        if dry_run or not run_it or not cli:
            print(("run: " if cli else "run (claude not on PATH here): ") + shown, file=out)
            continue
        try:
            result = subprocess.run([cli, *cmd[1:]], capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:  # `marketplace add` clones a repository: a slow network can take long
            print(f"failed: {shown} (it did not answer in 120 s; run it again)", file=out)
            return 1
        print(f"{'ran' if result.returncode == 0 else 'failed'}: {shown}", file=out)
        if result.returncode != 0:
            print((result.stderr or result.stdout).strip()[-500:], file=out)
            return 1
    if not remove:
        print("The plugin has no session hooks (the directory takes none), so a daily task runs the nightly pass:",
              file=out)
        if not dry_run:
            ask_time(homes, prompt, out)
    return install_schedule(homes, remove=remove, dry_run=dry_run, out=out, at=at)


def scheduled(homes: Homes) -> bool:
    """Is the daily task in place? Windows: Task Scheduler knows it; macOS and Linux: its file is where the printed
    instructions put it (this program does not enable it there, so the file is all it can see)."""
    try:
        if sys.platform == "win32":
            found = subprocess.run(["schtasks", "/Query", "/TN", TASK, "/XML"], capture_output=True, timeout=30)
            if found.returncode != 0:
                return False
            # A task switched off in Task Scheduler is still there but runs nothing: its <Settings> say so.
            settings = re.search(rb"<Settings>.*?</Settings>", found.stdout or b"", re.S)
            return not (settings and re.search(rb"<Enabled>\s*false\s*</Enabled>", settings.group(0)))
        # The printed files may be in place but never loaded: ask the service manager, not the folder.
        cmd = (["launchctl", "list", "com.agent-dreaming"] if sys.platform == "darwin" else
               ["systemctl", "--user", "is-active", "--quiet", "agent-dreaming.timer"])
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pass_starters(homes: Homes) -> list[str]:
    """What starts the nightly pass on this computer: the daily task, Codex's hooks, Antigravity's hooks."""
    found = ["daily task"] if scheduled(homes) else []
    codex = homes.codex / "config.toml"
    try:
        ours_there = _our_hooks(codex.read_text(encoding="utf-8")) if codex.is_file() else None
        if any("--async" in str(hook.get("command", "")) for _i, _j, hook in ours_there or []):
            found.append("Codex hooks")  # the background one runs the pass; the other only shows the summary
    except (OSError, UnicodeDecodeError):
        pass
    try:
        hooks = json.loads((homes.gemini / "config" / "hooks.json").read_text(encoding="utf-8"))
        if isinstance(hooks, dict) and ours(hooks.get(AG_HOOKS[1])):  # the one that starts the pass
            found.append("Antigravity hooks")
    except (OSError, ValueError):
        pass
    return found


def account_variable(name: str) -> str | None:
    """A variable as the user account keeps it, which is what Task Scheduler gives the task: Windows' user, then
    system environment. Elsewhere None: launchd and systemd user units do not read a shell's profile."""
    if sys.platform != "win32":
        return None
    import winreg

    for root, key in ((winreg.HKEY_CURRENT_USER, "Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(root, key) as k:
                value, _kind = winreg.QueryValueEx(k, name)
            return os.path.expandvars(str(value))
        except OSError:
            continue
    return None


def _same_path(a: str, b: str | None) -> bool:
    if b is None:
        return False
    norm = lambda p: os.path.normcase(os.path.abspath(os.path.expanduser(p)))  # noqa: E731
    return norm(a) == norm(b)


def schedule_spec(platform: str | None = None, at: str = "04:00") -> tuple[str, str]:
    """(file name, content) of the daily job for this platform, at *at* (HH:MM, local time)."""
    platform = platform or sys.platform
    hour, minute = config.dream_time(at) or (4, 0)
    if platform == "win32":
        python = Path(sys.executable)
        windowless = python.with_name("pythonw.exe")  # no console window at night (Fable review of 0.8)
        python = escape(str(windowless if windowless.is_file() else python))  # `&` broke the XML (Codex review)
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
  <key>ProgramArguments</key><array><string>{escape(python_path())}</string><string>-I</string><string>-m</string>
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


def install_schedule(homes: Homes, *, remove: bool, dry_run: bool, out=sys.stdout, at: str | None = None) -> int:
    """*at*: a time given on the command line (a ``--dry-run`` previews it without keeping it); else the setting."""
    chosen = at or config.load(homes)[0].get("dream_time")
    name, content = schedule_spec(at=chosen if config.dream_time(chosen) else "04:00")
    if sys.platform != "win32" and remove:
        if sys.platform == "darwin":
            print("Stop and remove the daily task yourself: launchctl unload "
                  "~/Library/LaunchAgents/com.agent-dreaming.plist, then delete that file.", file=out)
        else:
            print("Stop and remove the daily task yourself: systemctl --user disable --now agent-dreaming.timer, then "
                  "delete ~/.config/systemd/user/agent-dreaming.service and agent-dreaming.timer.", file=out)
        return 0
    if not remove:
        unseen = [v for v in FOLDER_VARIABLES if os.environ.get(v) and not _same_path(os.environ[v],
                                                                                         account_variable(v))]
        if unseen:
            print(f"warning: {', '.join(unseen)} set in this terminal only: the daily task will not see "
                  f"{'it' if len(unseen) == 1 else 'them'} and reads the usual folders instead. Set "
                  f"{'it' if len(unseen) == 1 else 'them'} for your user account, or leave "
                  f"{'it' if len(unseen) == 1 else 'them'} out.", file=out)
    if sys.platform != "win32":
        print(f"{name}:\n{content}", file=out)
        print("Put these files in place and enable them yourself (launchctl load / systemctl --user enable --now "
              "agent-dreaming.timer); this command writes them only on Windows.", file=out)
        return 0
    if remove:
        if subprocess.run(["schtasks", "/Query", "/TN", TASK], capture_output=True, timeout=30).returncode != 0:
            print("no daily task to remove", file=out)
            return 0
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
    code, text = _scheduler(cmd)
    print(text.strip(), file=out)
    return 0 if code == 0 else 1


def _scheduler(cmd: list[str]) -> tuple[int, str]:
    """Run a Task Scheduler command; its answer is in the console's OEM code page even through a pipe (an agent's
    shell), where the console's own encoding is unknown."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60,
                            encoding="oem" if sys.platform == "win32" else "utf-8", errors="replace")
    return result.returncode, (result.stdout or result.stderr or "")


def scheduler_text(cmd: list[str]) -> str:
    return _scheduler(cmd)[1]
