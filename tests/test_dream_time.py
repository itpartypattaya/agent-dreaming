"""The time of the night's pass is the person's: their computer may be off at 04:00, or the agent used only by day.
Until they choose, the agent asks them once (and not every day for ever)."""
import io
import json
from datetime import datetime, timedelta

import pytest
from world import environ

from dreamem import cli, config, hooks, install, run
from dreamem.fsutil import write_json

SETTINGS = 'max_questions = 3\nclients = ["codex"]\n\n[windows]\nthemes_days = 10\n'


def stamp(dt: datetime) -> str:
    return dt.astimezone().isoformat(timespec="seconds")


def last_success(world, dt: datetime) -> None:
    write_json(world.homes.summary, {"last_success": stamp(dt)})


def hook(world) -> dict:
    out = io.StringIO()
    assert hooks.session_start(world.homes, "claude-code", out=out) == 0
    return json.loads(out.getvalue())


def asks_time(payload: dict) -> bool:
    return "dreamem config time" in ((payload.get("hookSpecificOutput") or {}).get("additionalContext") or "")


# 1. the chosen time decides when the background pass runs
def test_the_chosen_time_decides_when_the_pass_is_due(world):
    config.set_time(world.homes, "23:00")
    day = datetime(2026, 10, 30)
    last_success(world, day - timedelta(hours=1))                                # 29.10 23:00 passed, done at 23:00
    assert not run.due(world.homes, now=(day + timedelta(hours=22)).astimezone())  # 30.10 22:00: not yet
    assert run.due(world.homes, now=(day + timedelta(hours=23, minutes=10)).astimezone())  # 30.10 23:10
    last_success(world, day + timedelta(hours=23, minutes=10))
    assert not run.due(world.homes, now=(day + timedelta(days=1, minutes=30)).astimezone())  # 31.10 00:30: done


def test_without_a_time_the_pass_is_due_once_a_calendar_day(world):
    day = datetime(2026, 10, 30)
    last_success(world, day - timedelta(minutes=30))  # 29.10 23:30
    now = (day + timedelta(minutes=30)).astimezone()
    assert run.due(world.homes, now=now)
    last_success(world, day + timedelta(minutes=20))
    assert not run.due(world.homes, now=now)


# 2. until a time is chosen, the agent is asked to ask — even before the first night, once a day, three days at most
def test_the_agent_asks_for_the_time_until_it_is_chosen(world, monkeypatch):
    days = iter(["2026-10-30", "2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"])
    monkeypatch.setattr(run, "today", lambda: current[0])
    current = [next(days)]
    first = hook(world)  # right after install: no night yet
    assert asks_time(first)
    assert "memory question" not in first["hookSpecificOutput"]["additionalContext"]
    current[0] = next(days)
    assert not asks_time(hook(world))  # the same day: not again
    for _ in range(2):
        current[0] = next(days)
        assert asks_time(hook(world))
    current[0] = next(days)
    assert not asks_time(hook(world))  # three days without an answer: the default stays, no more asking


def test_a_chosen_time_or_no_preference_ends_the_asking(world, monkeypatch):
    monkeypatch.setattr(run, "today", lambda: "2026-10-30")
    config.set_time(world.homes, "any")
    assert not asks_time(hook(world))
    world.homes.config.unlink()
    config.set_time(world.homes, "21:30")
    assert not asks_time(hook(world))


def test_the_question_and_the_night_come_together(world, monkeypatch):
    world.fact("Магазин закрывается в девять вечера по пятницам", origin="inferred", days_old=2)
    run.run(world.homes, out=io.StringIO())
    payload = hook(world)
    text = payload["hookSpecificOutput"]["additionalContext"]
    assert asks_time(payload) and "1 memory question" in text


# 3. setting the time touches that one line of our settings
def test_setting_the_time_keeps_the_rest_of_the_settings(world):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text(SETTINGS, encoding="utf-8")
    config.set_time(world.homes, "9:05")
    config.set_time(world.homes, "23:40")
    settings, warnings = config.load(world.homes)
    assert warnings == []
    assert settings["dream_time"] == "23:40" and settings["max_questions"] == 3 and settings["clients"] == ["codex"]
    assert settings["windows"]["themes_days"] == 10 and "dream_time" not in settings["windows"]
    assert world.homes.config.read_text(encoding="utf-8").count("dream_time") == 1


@pytest.mark.parametrize("bad", ["25:00", "7", "23:60", "evening", ""])
def test_a_wrong_time_is_refused_and_nothing_changes(world, bad):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text(SETTINGS, encoding="utf-8")
    with pytest.raises(ValueError):
        config.set_time(world.homes, bad)
    assert world.homes.config.read_text(encoding="utf-8") == SETTINGS


def test_unreadable_settings_are_not_overwritten(world):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_bytes(b"max_questions = 3\n# \xff\n")
    with pytest.raises(ValueError):
        config.set_time(world.homes, "23:00")
    assert world.homes.config.read_bytes() == b"max_questions = 3\n# \xff\n"


def test_the_command_line_sets_and_shows_the_time(world, capsys):
    with environ(world):
        assert cli.main(["config", "time", "07:30"]) == 0
        assert cli.main(["config", "time"]) == 0
        assert cli.main(["config", "time", "half past seven"]) == 2
    assert "07:30" in capsys.readouterr().out
    assert config.load(world.homes)[0]["dream_time"] == "07:30"


# 4. the optional schedule runs at the chosen time
@pytest.mark.parametrize("platform, expected", [("win32", "T23:15:00"), ("linux", "*-*-* 23:15:00"),
                                                ("darwin", "<key>Hour</key><integer>23</integer><key>Minute</key>"
                                                           "<integer>15</integer>")])
def test_the_schedule_uses_the_chosen_time(platform, expected):
    assert expected in install.schedule_spec(platform, at="23:15")[1]


def test_install_schedule_reads_the_chosen_time(world):
    config.set_time(world.homes, "23:15")
    out = io.StringIO()
    assert install.install_schedule(world.homes, remove=False, dry_run=True, out=out) == 0
    assert "23:15" in out.getvalue() and "04:00" not in out.getvalue()


# 5. installing into Codex in a terminal asks for the time when none is chosen
def test_install_codex_asks_for_the_time(world, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    asked = []
    out = io.StringIO()
    assert install.install_codex(world.homes, remove=False, dry_run=False, out=out,
                                 prompt=lambda q: asked.append(q) or "22:30") == 0
    assert asked and config.load(world.homes)[0]["dream_time"] == "22:30"
    asked.clear()
    assert install.install_codex(world.homes, remove=False, dry_run=False, out=out,
                                 prompt=lambda q: asked.append(q) or "06:00") == 0
    assert not asked  # chosen already: not asked again


def test_install_codex_takes_the_time_without_asking(world, tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with environ(world):
        assert cli.main(["install", "codex", "--time", "21:15"]) == 0
        assert cli.main(["install", "codex", "--time", "late"]) == 2
    assert config.load(world.homes)[0]["dream_time"] == "21:15"
