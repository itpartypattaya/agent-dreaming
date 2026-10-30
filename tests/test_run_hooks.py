"""Phase 2: running the pass and telling a session about it.

Requirements: the pass reads the CLIs' homes and changes nothing there; the lazy night runs once a day; the
session-start line holds numbers only (it goes into every session's context) and is printed once a day; a hook never
breaks a session; a source read only in part asks no "still relevant?" question (silence there is not evidence).
"""
import io
import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from dreamem import config, hooks, run


def test_nothing_outside_our_home_changes(world):
    world.fact("Магазин закрывается в девять вечера по пятницам", origin="inferred", days_old=2)
    world.note("deploy", "Деплой сайта магазина через rsync", "rsync -av dist/ shop:/var/www")
    world.say({2: ["поменяй ценники"], 1: ["касса сошлась"]})
    world.rollout([(datetime.now().timestamp() - 3600, "user", "посмотри таблицу")], zst=True)
    before = world.tree()
    assert run.run(world.homes, out=io.StringIO()) == 0
    assert world.tree() == before


def test_lazy_night_runs_once_a_day(world):
    out = io.StringIO()
    assert run.run(world.homes, if_due=True, out=out) == 0
    first = json.loads(world.homes.summary.read_text(encoding="utf-8"))["last_success"]
    assert run.run(world.homes, if_due=True, out=out) == 0
    assert json.loads(world.homes.summary.read_text(encoding="utf-8"))["last_success"] == first
    summary = json.loads(world.homes.summary.read_text(encoding="utf-8"))
    summary["last_success"] = (datetime.now().astimezone() - timedelta(days=1)).isoformat(timespec="seconds")
    world.homes.summary.write_text(json.dumps(summary), encoding="utf-8")
    assert run.run(world.homes, if_due=True, out=out) == 0
    assert json.loads(world.homes.summary.read_text(encoding="utf-8"))["last_success"] != summary["last_success"]


def test_a_failed_core_does_not_count_as_done(world, monkeypatch):
    def broken(**kw):
        raise RuntimeError("something broke")

    monkeypatch.setattr(run, "run_night", broken)
    out = io.StringIO()
    assert run.run(world.homes, out=out) == 1
    assert "something broke" in out.getvalue()
    assert "last_success" not in json.loads(world.homes.summary.read_text(encoding="utf-8"))
    assert run.due(world.homes)


def test_session_start_line_has_numbers_not_texts_and_shows_once(world):
    secret_text = "Магазин закрывается в девять вечера по пятницам"
    world.fact(secret_text, origin="inferred", days_old=2)
    run.run(world.homes, out=io.StringIO())
    out = io.StringIO()
    assert hooks.session_start(world.homes, "claude-code", out=out) == 0
    payload = json.loads(out.getvalue())
    line = payload["hookSpecificOutput"]["additionalContext"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "1 memory question" in line and "/agent-dreaming:dream" in line and "$dreaming" in line
    assert secret_text not in out.getvalue() and "девять" not in out.getvalue()
    again = io.StringIO()
    hooks.session_start(world.homes, "codex", out=again)
    assert json.loads(again.getvalue()) == {}


def test_a_newer_night_is_shown_the_same_day(world):
    world.fact("Магазин закрывается в девять вечера по пятницам", origin="inferred", days_old=2)
    run.run(world.homes, out=io.StringIO())
    hooks.session_start(world.homes, "claude-code", out=io.StringIO())
    run.run(world.homes, out=io.StringIO())  # a new night, e.g. the lazy one in the background
    out = io.StringIO()
    hooks.session_start(world.homes, "claude-code", out=out)
    assert "hookSpecificOutput" in json.loads(out.getvalue())


def test_hook_never_breaks_a_session(world, monkeypatch):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.summary.write_text("{ not json", encoding="utf-8")
    out = io.StringIO()
    assert hooks.session_start(world.homes, "claude-code", out=out) == 0
    # A broken summary reads as a fresh install: no night to show (the time question may come, as after install).
    assert "memory question" not in json.dumps(json.loads(out.getvalue()))

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(run, "run", boom)
    out = io.StringIO()
    assert hooks.session_start(world.homes, "codex", background=True, out=out) == 0
    assert json.loads(out.getvalue()) == {}


def test_partial_coverage_asks_no_relevance_question(world):
    world.note("deploy", "Деплой сайта магазина через rsync", "rsync -av dist/ shop:/var/www")
    world.say({1: ["касса сошлась"]})
    run.run(world.homes, out=io.StringIO())
    assert [q["section"] for q in world.questions()["questions"]] == ["note_decay"]
    p = world.rollout([(datetime.now().timestamp() - 3600, "user", "x")], zst=True, plain=False)[0]
    p.write_bytes(b"(\xb5/\xfd broken")
    run.run(world.homes, out=io.StringIO())
    doc = world.questions()
    assert doc["questions"] == []
    assert doc["skipped"] == {"note_decay:coverage": 1}
    assert "codex partial" in (world.homes.profile("global") / "report.md").read_text(encoding="utf-8")


def test_no_network_in_this_process(world, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    world.fact("Магазин закрывается в девять вечера по пятницам", origin="inferred", days_old=2)
    world.say({1: ["касса сошлась"]})
    assert run.run(world.homes, out=io.StringIO()) == 0


@pytest.mark.parametrize("hours, name", [(7, "Etc/GMT-7"), (-5, "Etc/GMT+5"), (0, "UTC")])
def test_timezone_follows_the_machine_offset(monkeypatch, hours, name):
    class Fixed(datetime):
        """A machine whose local zone is UTC+hours: now() is naive local time, astimezone() attaches that zone."""

        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 30, 12)

        def astimezone(self, tz=None):
            return self.replace(tzinfo=timezone(timedelta(hours=hours)))

    monkeypatch.setattr(config, "datetime", Fixed)
    assert config.timezone_name("auto") == name
    assert config.timezone_name("Asia/Bangkok") == "Asia/Bangkok"


def test_the_engine_reads_the_store_holomem_opens(world, monkeypatch):
    from dreamem import engineapi

    world.fact("Магазин закрывается в девять вечера по пятницам", origin="inferred", days_old=2)
    configs = []
    real = engineapi.run_night

    def spy(**kw):
        configs.append(kw["config"])
        return real(**kw)

    monkeypatch.setattr(run, "run_night", spy)
    run.run(world.homes, out=io.StringIO())
    assert configs[0]["store"] == str(world.db) and configs[0]["fact_source"] == "holographic"
    state = sorted(p.name for p in (world.homes.profile("global") / "state").iterdir())
    assert "seen.json" in state and not (world.homes.profile("global") / "home").exists()


def test_cleanup_touches_only_our_own_leftovers(world):
    """Security review: a pass removes the `.partial` files it may have left — never other files, even when the
    home is pointed at a folder holding someone else's."""
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    stranger = world.homes.dreaming / "photos" / "holiday.partial.jpg"
    stranger.parent.mkdir(parents=True)
    stranger.write_bytes(b"jpeg")
    ours = world.homes.dreaming / "summary.json.partial"
    ours.write_text("{", encoding="utf-8")
    run.run(world.homes, out=io.StringIO())
    assert stranger.exists() and not ours.exists()
