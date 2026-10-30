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

    assert not (engineapi.ENGINE_DIR / "dream_judge.py").exists()  # the network judge is not part of this plugin
