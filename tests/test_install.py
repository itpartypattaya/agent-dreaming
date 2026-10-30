"""Installing into Codex: one marked block, idempotent, removable exactly; the TOML stays valid; the user's own
settings around it are kept. The skill ships one text for both CLIs, named per CLI."""
import io
import tomllib
from pathlib import Path

from dreamem import install

USER = 'model = "gpt-6.1-sol"\n\n[features]\nmemories = true\n'


def test_block_is_added_once_and_removed_exactly():
    once = install.codex_text(USER)
    twice = install.codex_text(once)
    assert once == twice
    parsed = tomllib.loads(once)
    hooks = parsed["hooks"]["SessionStart"]
    assert len(hooks) == 2
    assert [h["hooks"][0].get("async", False) for h in hooks] == [False, True]
    assert all("-I -m dreamem hook session-start --client codex" in h["hooks"][0]["command"] for h in hooks)
    assert parsed["model"] == "gpt-6.1-sol" and parsed["features"]["memories"] is True
    assert install.codex_text(once, remove=True) == USER


def test_install_codex_dry_run_writes_nothing(tmp_path, monkeypatch):
    from dreamem.paths import homes

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    h = homes({"CODEX_HOME": str(tmp_path / ".codex"), "DREAMEM_HOME": str(tmp_path / ".dreaming")})
    out = io.StringIO()
    assert install.install_codex(h, remove=False, dry_run=True, out=out) == 0
    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert not (tmp_path / ".agents").exists()
    assert install.install_codex(h, remove=False, dry_run=False, out=out) == 0
    skill = (tmp_path / ".agents" / "skills" / "dreaming" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: dreaming\n")
    assert install.install_codex(h, remove=True, dry_run=False, out=out) == 0
    assert install.MARK_BEGIN not in (tmp_path / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert not (tmp_path / ".agents" / "skills" / "dreaming").exists()


def test_windows_command_survives_powershell():
    cmd = install.windows_command("hook", "session-start")
    assert not cmd.startswith('"')  # a quoted path followed by arguments is a ParserError in PowerShell


def test_plugin_skill_is_the_package_skill():
    root = Path(__file__).resolve().parent.parent
    plugin = (root / "claude-plugin" / "skills" / "dream" / "SKILL.md").read_text(encoding="utf-8")
    package = (root / "dreamem" / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert plugin == package
    assert plugin.startswith("---\nname: dream\n")


def test_schedule_specs_run_if_due():
    for platform in ("win32", "darwin", "linux"):
        _name, content = install.schedule_spec(platform)
        assert "-I -m dreamem run --if-due" in content or "<string>--if-due</string>" in content
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in install.schedule_spec("win32")[1]
    assert "Persistent=true" in install.schedule_spec("linux")[1]


def test_engine_is_self_contained():
    """The engine ships with the package and loads nothing from another project: no import of a sibling repository,
    no tool that syncs it, its licence and notice in place."""
    root = Path(__file__).resolve().parent.parent
    engine = root / "dreamem" / "engine"
    assert (engine / "LICENSE").exists() and (engine / "NOTICE.md").exists()
    assert not (root / "tools" / "sync_dream.py").exists()
    for path in engine.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "hermes-dreaming/" not in text.replace("hermes-dreaming/issues", "")
    from dreamem import engineapi

    for gone in ("dream_judge.py", "dream_yaml.py", "dream-reject.py"):  # the network judge, the Hermes readers
        assert not (engineapi.ENGINE_DIR / gone).exists()
    source = (engineapi.ENGINE_DIR / "dream.py").read_text(encoding="utf-8")
    for word in ("HERMES_HOME", "os.environ", "config.yaml", "state.db\"", "subprocess"):
        assert word not in source, word


def test_another_skill_named_dreaming_is_never_overwritten_or_removed(tmp_path, monkeypatch):
    """Security review: hermes-dreaming ships a skill named `dreaming` too; install and uninstall must leave a skill
    that is not ours as it is."""
    from dreamem.paths import homes

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    h = homes({"CODEX_HOME": str(tmp_path / ".codex"), "DREAMEM_HOME": str(tmp_path / ".dreaming")})
    theirs = tmp_path / ".agents" / "skills" / "dreaming" / "SKILL.md"
    theirs.parent.mkdir(parents=True)
    theirs.write_text("---\nname: dreaming\n---\nsomeone else's skill\n", encoding="utf-8")
    out = io.StringIO()
    install.install_codex(h, remove=False, dry_run=False, out=out)
    assert theirs.read_text(encoding="utf-8").endswith("someone else's skill\n")
    assert "not ours" in out.getvalue()
    install.install_codex(h, remove=True, dry_run=False, out=io.StringIO())
    assert theirs.exists()


def _codex_appended(text: str, setting: str) -> str:
    """What Codex does with a new table: it appends it at the end of the file — inside our block, before its end mark
    (seen live 2026-10-31: `[tui] screen_reader_detection_done = true`)."""
    return text.replace(install.MARK_END, setting + "\n" + install.MARK_END)


def test_a_setting_codex_appended_inside_the_block_is_kept():
    tui = "[tui]\nscreen_reader_detection_done = true\n"
    live = _codex_appended(install.codex_text(USER), tui)
    again = install.codex_text(live)  # reinstall / upgrade
    parsed = tomllib.loads(again)
    assert parsed["tui"]["screen_reader_detection_done"] is True
    assert len(parsed["hooks"]["SessionStart"]) == 2
    assert install.codex_block() in again  # our hooks byte for byte: their trust (a hash) holds
    assert again.index(install.MARK_END) < again.index("[tui]")  # set outside our block, never removed with it
    assert install.codex_text(again) == again
    removed = install.codex_text(live, remove=True)
    assert tomllib.loads(removed)["tui"]["screen_reader_detection_done"] is True
    assert install.MARK_BEGIN not in removed and "dreamem" not in removed
    assert tomllib.loads(removed)["model"] == "gpt-6.1-sol"


def test_an_old_block_with_another_python_is_replaced_not_kept():
    old = install.codex_text(USER).replace(install.python_path(), "C:/old/python.exe")
    old = old.replace(str(Path(install.sys.executable)).replace("\\", "\\\\"), "C:\\old\\python.exe")
    assert "C:/old/python.exe" in old
    new = install.codex_text(_codex_appended(old, "[tui]\nalt_screen = false\n"))
    assert "C:/old/python.exe" not in new and "old\\python" not in new
    assert len(tomllib.loads(new)["hooks"]["SessionStart"]) == 2 and tomllib.loads(new)["tui"]["alt_screen"] is False
