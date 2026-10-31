"""Antigravity: the review skill and two named SessionStart hooks in ~/.gemini/config (its global customization root),
other hooks kept; the hook's answer in the shape Antigravity takes; tonight's pass started detached."""
import io
import json


from dreamem import hooks, install
from dreamem.fsutil import write_json


def never(*_):
    raise AssertionError("asked a question with nobody at the console")


def test_install_adds_ours_keeps_theirs_and_uninstall_removes_only_ours(world):
    path = world.homes.gemini / "config" / "hooks.json"
    path.parent.mkdir(parents=True)
    theirs = {"lint-checker": {"PostToolUse": [{"matcher": "run_command", "hooks": [{"command": "./lint.sh"}]}]}}
    path.write_text(json.dumps(theirs), encoding="utf-8")
    out = io.StringIO()
    assert install.install_antigravity(world.homes, remove=False, dry_run=True, out=out, prompt=never) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == theirs  # a preview writes nothing
    assert install.install_antigravity(world.homes, remove=False, dry_run=False, out=out, prompt=lambda q: "") == 0
    now = json.loads(path.read_text(encoding="utf-8"))
    assert now["lint-checker"] == theirs["lint-checker"]
    sync, background = (now[n]["SessionStart"][0]["command"] for n in install.AG_HOOKS)
    assert sync.endswith("-I -m dreamem hook session-start --client antigravity")
    assert background.endswith("--client antigravity --async")
    skill = world.homes.gemini / "config" / "skills" / "dreaming" / "SKILL.md"
    assert skill.read_text(encoding="utf-8").startswith("---\nname: dreaming\n")
    assert install.install_antigravity(world.homes, remove=True, dry_run=False, out=out, prompt=never) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == theirs
    assert not skill.exists()


def test_a_broken_hooks_file_is_left_as_it_is(world):
    path = world.homes.gemini / "config" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    assert install.install_antigravity(world.homes, remove=False, dry_run=False, out=io.StringIO(), prompt=never) == 1
    assert path.read_text(encoding="utf-8") == "{ not json"


def test_the_hook_answers_in_antigravitys_shape(world):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text('dream_time = "any"\n', encoding="utf-8")
    write_json(world.homes.summary, {"profiles": {"global": {"generated_at": "2026-10-31T04:00:00+07:00",
                                                             "published": 1.0, "open": 2,
                                                             "sections": {"conflict": 2}}}})
    out = io.StringIO()
    assert hooks.session_start(world.homes, "antigravity", out=out) == 0
    said = json.loads(out.getvalue())
    text = said["injectSteps"][0]["systemMessage"]["systemMessage"]
    assert "2 memory questions" in text and "hookSpecificOutput" not in said


def test_the_background_hook_starts_the_pass_detached_and_returns_at_once(world, monkeypatch):
    started = []
    monkeypatch.setattr(hooks, "detach", started.append)
    monkeypatch.setattr(hooks.run, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran in the hook")))
    out = io.StringIO()
    assert hooks.session_start(world.homes, "antigravity", background=True, out=out) == 0
    assert out.getvalue() == "{}\n" and started == [["-m", "dreamem", "run", "--if-due"]]
