"""Fable review of 0.8.0–0.8.2 (07.11.2026): the daily task that runs the pass for Claude Code — what it starts, what
the person is told when nothing runs it, and how install and uninstall behave around it."""
import io
import subprocess
import sys

import pytest

from dreamem import cli, install, review

NOTHING = "nightly run: nothing starts it on its own — run `dreamem install schedule`"


class Fake:
    """Stands in for the scheduler and the `claude` CLI. *answers*: first word(s) of a command → return code."""

    def __init__(self, answers=None, stdout=b""):
        self.seen, self.answers, self.stdout = [], answers or {}, stdout

    def __call__(self, cmd, *args, **kwargs):
        self.seen.append(list(cmd))
        code = next((rc for head, rc in self.answers.items() if " ".join(cmd).startswith(head)), 0)
        if isinstance(code, BaseException):
            raise code
        out = self.stdout.decode() if kwargs.get("text") or kwargs.get("encoding") else self.stdout
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")


def test_the_windows_task_runs_without_a_console_window(tmp_path, monkeypatch):
    # P2-1: python.exe opened a console window at night; pythonw.exe beside it runs the same without one.
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "python.exe").write_bytes(b"")
    (scripts / "pythonw.exe").write_bytes(b"")
    monkeypatch.setattr(install.sys, "executable", str(scripts / "python.exe"))
    assert f"<Command>{scripts / 'pythonw.exe'}</Command>" in install.schedule_spec("win32")[1]
    (scripts / "pythonw.exe").unlink()  # an interpreter without it: the console one still works
    assert f"<Command>{scripts / 'python.exe'}</Command>" in install.schedule_spec("win32")[1]


def test_a_review_tells_the_person_when_nothing_runs_the_pass(world, monkeypatch):
    # P2-2: after the plugin's update to 0.8 without `install claude-code`, no night came and nothing said why;
    # the line was only in `dreamem status`, which nobody runs. `show` is what every review starts with.
    said = []
    monkeypatch.setattr(install, "pass_starters", lambda homes: [])
    review.show(world.homes, out=said.append)
    assert NOTHING in said
    said.clear()
    monkeypatch.setattr(install, "pass_starters", lambda homes: ["daily task"])
    review.show(world.homes, out=said.append)
    assert not any("nightly run" in line for line in said)


@pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler")
def test_uninstall_without_a_task_succeeds(world, monkeypatch):
    # P3: removing a task that was never made printed the scheduler's error and exited with 1.
    fake = Fake({"schtasks /Query": 1})
    monkeypatch.setattr(install.subprocess, "run", fake)
    out = io.StringIO()
    assert install.install_schedule(world.homes, remove=True, dry_run=False, out=out) == 0
    assert not [c for c in fake.seen if c[:2] == ["schtasks", "/Delete"]]
    assert "no daily task" in out.getvalue()


@pytest.mark.skipif(sys.platform != "win32", reason="the console's code page")
def test_the_schedulers_answer_reads_right_through_a_pipe():
    # P3: from an agent's shell (a pipe) the scheduler's Russian answer came out as mojibake.
    text = install.scheduler_text(["schtasks", "/Query", "/TN", "agent-dreaming-no-such-task"])
    assert text.strip()
    assert "�" not in text


@pytest.mark.parametrize("platform, query", [("linux", "systemctl --user is-active"),
                                             ("darwin", "launchctl list com.agent-dreaming")])
def test_a_task_file_that_is_not_loaded_does_not_count(world, monkeypatch, platform, query):
    # P3: on macOS and Linux the printed files may be in place but never enabled; the file alone said "daily task".
    monkeypatch.setattr(install.sys, "platform", platform)
    monkeypatch.setattr(install.subprocess, "run", Fake({query: 3}))
    assert install.scheduled(world.homes) is False
    monkeypatch.setattr(install.subprocess, "run", Fake({query: 0}))
    assert install.scheduled(world.homes) is True


def test_install_with_a_time_does_not_send_the_person_to_install_again(world, monkeypatch, capsys):
    # P3: `install claude-code --time 23:15` said "run `dreamem install schedule` again to move it" while it was
    # setting the task up itself.
    for name, value in world.env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(install.subprocess, "run", Fake())
    monkeypatch.setattr(install.shutil, "which", lambda name: None)
    assert cli.main(["install", "claude-code", "--time", "23:15"]) == 0
    assert "install schedule` again" not in capsys.readouterr().out


def test_a_claude_cli_that_does_not_answer_is_reported_not_a_traceback(world, monkeypatch):
    # P3: `claude plugin marketplace add` clones a repository; past the time limit it raised a traceback.
    fake = Fake({"/usr/bin/claude": subprocess.TimeoutExpired(["claude"], 120)})
    monkeypatch.setattr(install.subprocess, "run", fake)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    out = io.StringIO()
    assert install.install_claude(world.homes, remove=False, dry_run=False, run_it=True, out=out) == 1
    assert "failed" in out.getvalue() and "did not answer" in out.getvalue()
    assert not [c for c in fake.seen if c[0] == "schtasks"]  # nothing half-installed after it


def test_no_text_still_speaks_of_the_plugins_hook():
    # P3: texts left from 0.7 said the plugin's SessionStart hook asks the agent for a time.
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    code = (root / "dreamem" / "install.py").read_text(encoding="utf-8")
    assert "the SessionStart hook asks it to" not in code
    assert "the agent asks in the first session instead" not in code
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    entry = changelog.split("## 0.8.0", 1)[1].split("\n## ", 1)[0]
    assert "`--uninstall` removes the plugin and the task" not in entry
