"""0.8.0: the plugin ships no hooks (the Anthropic directory refuses hooks of any type from organizations that are not
Verified, since 7 November 2026), and the program takes over what the hooks did for Claude Code: a daily task runs the
pass, `dreamem status` says what starts it, the skill tells the person when nothing does."""
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from dreamem import install, run

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude-plugin"
ALLOWED = {".md", ".json", ".png"}  # what the directory takes from a publisher that is not Verified


def test_the_plugin_ships_no_hooks_and_only_files_the_directory_takes():
    files = [p for p in PLUGIN.rglob("*") if p.is_file()]
    assert files
    assert not [p for p in files if "hooks" in p.relative_to(PLUGIN).parts or p.name == "hooks.json"]
    assert {p.suffix for p in files} <= ALLOWED, sorted(str(p) for p in files if p.suffix not in ALLOWED)
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert "hooks" not in manifest


class Calls:
    """Stands in for the operating system's scheduler and the `claude` CLI: records what would run."""

    def __init__(self):
        self.seen = []

    def __call__(self, cmd, *args, **kwargs):
        self.seen.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="SUCCESS", stderr="")


@pytest.fixture
def calls(monkeypatch):
    c = Calls()
    monkeypatch.setattr(install.subprocess, "run", c)
    monkeypatch.setattr(install.shutil, "which", lambda name: None)  # no `claude` CLI here
    return c


def test_install_claude_code_previews_the_daily_pass_and_keeps_nothing(world, calls):
    out = io.StringIO()
    assert install.install_claude(world.homes, remove=False, dry_run=True, run_it=False, out=out) == 0
    text = out.getvalue()
    assert "claude plugin install agent-dreaming@agent-dreaming" in text
    assert install.schedule_spec(at="04:00")[1] in text  # the daily task it would set up
    assert calls.seen == []
    assert not list(world.root.rglob("*.xml"))  # the task's file is not written either


def test_install_claude_code_sets_up_the_daily_pass(world, calls):
    out = io.StringIO()
    assert install.install_claude(world.homes, remove=False, dry_run=False, run_it=False, out=out) == 0
    if sys.platform == "win32":
        assert any(c[:3] == ["schtasks", "/Create", "/TN"] and install.TASK in c for c in calls.seen)
    else:  # macOS and Linux: the files are printed for the person to put in place, as `install schedule` does
        assert install.schedule_spec(at="04:00")[1] in out.getvalue()


def test_uninstall_claude_code_removes_the_daily_task(world, calls):
    out = io.StringIO()
    assert install.install_claude(world.homes, remove=True, dry_run=False, run_it=False, out=out) == 0
    assert "claude plugin uninstall agent-dreaming@agent-dreaming" in out.getvalue()
    if sys.platform == "win32":
        assert ["schtasks", "/Delete", "/TN", install.TASK, "/F"] in calls.seen


def test_status_says_when_nothing_starts_the_pass(world, monkeypatch):
    monkeypatch.setattr(install, "scheduled", lambda homes: False)
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "nightly run: nothing starts it on its own — run `dreamem install schedule`" in out.getvalue()


def test_status_names_each_thing_that_starts_the_pass(world, monkeypatch):
    monkeypatch.setattr(install, "scheduled", lambda homes: True)
    monkeypatch.setattr(install.Path, "home", classmethod(lambda cls: world.root / "home"))
    assert install.install_codex(world.homes, remove=False, dry_run=False, out=io.StringIO(),
                                 prompt=lambda q: "") == 0
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "nightly run: daily task, Codex hooks" in out.getvalue()


@pytest.mark.parametrize("copy", ["dreamem/skill/SKILL.md", "claude-plugin/skills/dream/SKILL.md"])
def test_the_skill_says_how_the_pass_starts_without_hooks(copy):
    text = (ROOT / copy).read_text(encoding="utf-8")
    missing = text.split("## When `dreamem` is missing", 1)[1].split("\n## ", 1)[0]
    assert "dreamem install claude-code" in missing
    section = text.split("## When no pass runs on its own", 1)[1].split("\n## ", 1)[0]
    assert "`nightly run:`" in section and "nothing starts it" in section
    assert "dreamem install schedule" in section
    assert "only if" in section.lower() or "on their yes" in section.lower()


def test_install_claude_code_brings_an_installed_plugin_up_to_date(world, monkeypatch):
    # `claude plugin install` on a plugin that is already there does nothing ("already installed"), so after updating
    # the program a person running `dreamem install claude-code --run` kept the old plugin — with its hooks.
    c = Calls()
    monkeypatch.setattr(install.subprocess, "run", c)
    monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/claude" if name == "claude" else None)
    assert install.install_claude(world.homes, remove=False, dry_run=False, run_it=True, out=io.StringIO()) == 0
    plugin_cmds = [cmd[1:] for cmd in c.seen if cmd[0] == "/usr/bin/claude"]
    assert plugin_cmds == [["plugin", "marketplace", "add", "itpartypattaya/agent-dreaming"],
                           ["plugin", "marketplace", "update", "agent-dreaming"],
                           ["plugin", "install", "agent-dreaming@agent-dreaming"],
                           ["plugin", "update", "agent-dreaming@agent-dreaming"]]
