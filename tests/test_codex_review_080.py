"""Codex review of 0.8.0–0.8.1 (07.11.2026): the daily task that now runs the pass for Claude Code must be created,
removed and reported truthfully."""
import io
import subprocess
import sys
import xml.dom.minidom
from pathlib import Path

import pytest

from dreamem import cli, install, run

ROOT = Path(__file__).resolve().parent.parent


class Calls:
    """Stands in for the operating system's scheduler and the `claude` CLI: records what would run."""

    def __init__(self, stdout=b"SUCCESS"):
        self.seen, self.stdout = [], stdout

    def __call__(self, cmd, *args, **kwargs):
        self.seen.append(list(cmd))
        out = self.stdout if not kwargs.get("text") else self.stdout.decode()
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="" if kwargs.get("text") else b"")


@pytest.fixture
def calls(monkeypatch):
    c = Calls()
    monkeypatch.setattr(install.subprocess, "run", c)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    return c


def test_the_readme_says_what_uninstall_removes_without_run():
    # Finding 1: without --run the plugin's removal is only printed (as installing it is), the task is removed.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    install_part = readme.split("dreamem install claude-code\n```", 1)[1].split("**Codex CLI**", 1)[0]
    assert "removes the plugin and the task" not in install_part
    assert "--uninstall" in install_part and "--run" in install_part


@pytest.mark.parametrize("platform, stop, start", [("linux", "systemctl --user disable --now agent-dreaming.timer",
                                                    "enable --now"),
                                                   ("darwin", "launchctl unload", "launchctl load ")])
def test_uninstall_on_macos_and_linux_says_how_to_stop_the_task(world, monkeypatch, platform, stop, start):
    # Finding 2: removing printed the files to install and "enable --now".
    monkeypatch.setattr(install.sys, "platform", platform)
    out = io.StringIO()
    assert install.install_schedule(world.homes, remove=True, dry_run=False, out=out) == 0
    assert stop in out.getvalue()
    assert start not in out.getvalue()
    assert "[Timer]" not in out.getvalue() and "<plist" not in out.getvalue()


def test_the_task_is_told_it_does_not_carry_folders_set_in_this_terminal(world, monkeypatch, calls):
    # Finding 3: a task started by the system does not see variables set only in the installing terminal.
    monkeypatch.setenv("DREAMEM_HOME", str(world.homes.dreaming))
    out = io.StringIO()
    install.install_schedule(world.homes, remove=False, dry_run=True, out=out)
    assert "DREAMEM_HOME" in out.getvalue()
    assert "does not carry" in out.getvalue()


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_a_python_path_with_an_ampersand_makes_valid_xml(monkeypatch, platform):
    # Finding 4: `&` in the interpreter's path broke the task's XML, so no task was created.
    odd = r"C:\Users\A&B\uv\tools\agent-dreaming\Scripts\python.exe" if platform == "win32" else "/Users/a&b/py/python"
    monkeypatch.setattr(install.sys, "executable", odd)
    monkeypatch.setattr(install, "python_path", lambda: odd)
    content = install.schedule_spec(platform, at="00:30")[1]
    doc = xml.dom.minidom.parseString(content.replace('encoding="UTF-16"', 'encoding="UTF-8"').encode("utf-8"))
    texts = [n.data for n in doc.getElementsByTagName("*") for n in n.childNodes if n.nodeType == n.TEXT_NODE]
    assert odd in texts


def test_status_does_not_count_a_codex_summary_hook_without_the_pass(world, monkeypatch):
    # Finding 5a: only the hook that runs the pass in the background starts it; the summary hook does not.
    monkeypatch.setattr(install, "scheduled", lambda homes: False)
    config = world.homes.codex / "config.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    block = install.codex_block().split("[[hooks.SessionStart]]")
    config.write_text(block[0] + "[[hooks.SessionStart]]" + block[1] + install.MARK_END + "\n", encoding="utf-8")
    assert "--async" not in config.read_text(encoding="utf-8")
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "nightly run: nothing starts it on its own" in out.getvalue()


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler")
def test_a_disabled_task_does_not_count(world, monkeypatch):
    # Finding 5b: a task switched off in Task Scheduler is still there, but runs nothing. Its XML as schtasks prints it.
    disabled = (b'<?xml version="1.0" encoding="UTF-16"?>\r\r\n<Task><Settings>\r\r\n'
                b'<StartWhenAvailable>true</StartWhenAvailable>\r\r\n<Enabled>false</Enabled>\r\r\n</Settings></Task>')
    monkeypatch.setattr(install.subprocess, "run", Calls(stdout=disabled))
    assert install.scheduled(world.homes) is False
    enabled = disabled.replace(b"<Enabled>false</Enabled>\r\r\n", b"")
    monkeypatch.setattr(install.subprocess, "run", Calls(stdout=enabled))
    assert install.scheduled(world.homes) is True


def test_a_preview_of_install_claude_code_shows_the_time_given(world, monkeypatch, calls, capsys):
    # Finding 6: `--dry-run --time 23:15` said "would set dream time: 23:15" and showed the task at the old time.
    for name, value in world.env.items():
        monkeypatch.setenv(name, value)
    assert cli.main(["install", "claude-code", "--dry-run", "--time", "23:15"]) == 0
    shown = capsys.readouterr().out
    assert install.schedule_spec(at="23:15")[1] in shown
    assert calls.seen == []
