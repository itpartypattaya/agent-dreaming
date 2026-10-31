"""The plugin's session-start hooks, run as Claude Code runs them: a shell form through bash (sh on macOS and Linux,
Git Bash on Windows) or PowerShell when Git Bash is missing.

Requirement: the directory installs the plugin without the dreamem program, and a hook that fails puts a "hook error"
notice in front of the person at every session start. So without dreamem both hooks succeed and the quick one tells
the agent how to install it; with dreamem they call it with the same arguments as before and pass its answer on.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = "uv tool install git+https://github.com/itpartypattaya/agent-dreaming"


def hooks():
    data = json.loads((ROOT / "claude-plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    (entry,) = data["hooks"]["SessionStart"]
    return entry["hooks"]


def bash():
    if sys.platform == "win32":  # Claude Code's choice there; a bare `bash` may be WSL's
        git = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        return str(git) if git.is_file() else None
    return shutil.which("sh")


SHELLS = {"bash": lambda cmd: [bash(), "-c", cmd],
          "powershell": lambda cmd: [shutil.which("pwsh"), "-NoProfile", "-NonInteractive", "-Command", cmd]}


def run_hook(shell, command, path_dir, tmp_path):
    if shell == "bash" and not bash() or shell == "powershell" and not shutil.which("pwsh"):
        pytest.skip(f"{shell} is not installed here")
    env = {k: v for k, v in os.environ.items() if k.upper() != "PATH"}
    env["PATH"] = str(path_dir)
    env["DREAMEM_ARGS_FILE"] = str(tmp_path / "args.txt")
    return subprocess.run(SHELLS[shell](command), env=env, capture_output=True, text=True, timeout=60,
                          cwd=tmp_path, stdin=subprocess.DEVNULL)


def fake_dreamem(folder: Path) -> None:
    folder.mkdir()
    script = folder / "dreamem"
    script.write_text('#!/bin/sh\nprintf "%s " "$@" > "$DREAMEM_ARGS_FILE"\necho \'{"from": "dreamem"}\'\n',
                      encoding="utf-8", newline="\n")
    script.chmod(0o755)
    (folder / "dreamem.cmd").write_text('@echo %* > "%DREAMEM_ARGS_FILE%"\r\n@echo {"from": "dreamem"}\r\n',
                                        encoding="utf-8", newline="")


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_without_dreamem_the_hooks_succeed_and_say_how_to_install(shell, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    quick, background = hooks()
    done = run_hook(shell, quick["command"], empty, tmp_path)
    assert done.returncode == 0, done.stderr
    assert INSTALL in done.stdout
    done = run_hook(shell, background["command"], empty, tmp_path)
    assert done.returncode == 0, done.stderr
    assert INSTALL not in done.stdout  # one hint a session is enough


@pytest.mark.parametrize("shell", ["bash", "powershell"])
def test_with_dreamem_the_hooks_call_it_as_before(shell, tmp_path):
    folder = tmp_path / "bin"
    fake_dreamem(folder)
    quick, background = hooks()
    for hook, args in ((quick, "hook session-start --client claude-code"),
                       (background, "hook session-start --client claude-code --async")):
        done = run_hook(shell, hook["command"], folder, tmp_path)
        assert done.returncode == 0, done.stderr
        assert (tmp_path / "args.txt").read_text(encoding="utf-8").split() == args.split()
        assert '{"from": "dreamem"}' in done.stdout
        assert INSTALL not in done.stdout
    assert background.get("async") is True and not quick.get("async")
