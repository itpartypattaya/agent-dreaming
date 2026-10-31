"""Recurring themes of the person's words: the engine can count them, our pass does not ask for them.

Nothing in dreamem turns a theme into a question, and counting them took about half of a night. The requirement:
the pass does not count themes at all, and leaving them out changes nothing else a night finds; the engine run
without the setting still counts them, as it always did.
"""
import io
import json
import re

from world import days_ago

from dreamem import engineapi, run
from dreamem.engine import dream

# Not a word of these is in a fact or a note, and "инвентаризация" / "стеллажи" come up on three days: themes.
# The cashier's lines corroborate a fact: the night has a question to ask, not only themes.
WORDS = {6: ["Инвентаризация склада в пятницу, стеллажи пересчитаем вечером",
             "Кассир Марина сегодня опять работает в субботу"],
         4: ["После инвентаризации стеллажи у входа надо переставить ближе к кассе",
             "Марина снова работает по субботам, кассир на месте"],
         2: ["Инвентаризация опять показала недостачу, стеллажи у окна пустые",
             "Марина в воскресенье тоже работает, кассир на месте"]}

# Per run, not per input: the moment of the pass.
PER_RUN = {"generated_at"}
STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\"]*")


def handed(world, monkeypatch):
    """The real pass on *world*; what our pass hands the engine (run_night's arguments)."""
    world.fact("Кассир Марина работает по субботам и воскресеньям", origin="said", days_old=50, rc=4, helpful=1)
    world.fact("Поставщик кофе Arabica Beans привозит зерно по вторникам утром", origin="said", days_old=40)
    world.say(WORDS)
    world.rollout([(days_ago(3), "user", "Инвентаризация в субботу, стеллажи с кофе тоже считаем")])
    world.note("shop", "Магазин", "Касса открывается в девять утра")
    calls = []
    real = engineapi.run_night

    def spy(**kw):
        calls.append(kw)
        return real(**kw)

    monkeypatch.setattr(run, "run_night", spy)
    assert run.run(world.homes, out=io.StringIO()) == 0
    assert len(calls) == 1
    return calls[0]


def night_with(kw, state_dir, config):
    """The same inputs, a fresh state folder, another config."""
    result, _ = engineapi.run_night(**dict(kw, state_dir=state_dir, config=config))
    return result


def state(path):
    """A state file with the moments of the run (ISO stamps) masked."""
    return STAMP.sub("<at>", path.read_text(encoding="utf-8"))


def without(result, *keys):
    out = {k: v for k, v in result.items() if k not in PER_RUN and k not in keys}
    out["stats"] = {k: v for k, v in result["stats"].items() if k not in keys}
    return out


def test_without_themes_a_night_differs_only_in_themes(world, monkeypatch, tmp_path):
    kw = handed(world, monkeypatch)
    engine_default = {k: v for k, v in kw["config"].items() if k != "themes"}
    on = night_with(kw, tmp_path / "on", engine_default)
    off = night_with(kw, tmp_path / "off", dict(engine_default, themes=False))
    themes = {t["theme"] for t in on["emerging_themes"]}
    assert any(t.startswith("инвентаризац") for t in themes), themes  # the engine still counts them by default
    assert on["stats"]["themes"] > 0
    assert off["emerging_themes"] == [] and off["stats"]["themes"] == 0
    # Everything else the night found — questions, stats, diary — is the same.
    assert without(off, "emerging_themes", "themes") == without(on, "emerging_themes", "themes")
    # The state a night leaves (cooldowns, snapshot, diary) is the same too, but for the moment of the run.
    names = sorted(p.name for p in (tmp_path / "on").iterdir())
    assert names == sorted(p.name for p in (tmp_path / "off").iterdir())
    assert {"seen.json", "asked.json", "DREAMS.md"} <= set(names)
    for name in names:
        assert state(tmp_path / "off" / name) == state(tmp_path / "on" / name), name


def test_our_pass_does_not_count_themes(world, monkeypatch, tmp_path):
    counted = []
    real = dream.extract_themes

    def spy(messages, window_days):
        counted.append(len(messages))
        return real(messages, window_days)

    monkeypatch.setattr(dream, "extract_themes", spy)
    kw = handed(world, monkeypatch)
    assert counted == []  # our pass: never counted
    assert kw["config"].get("themes") is False
    kept = json.loads((world.homes.profile("global") / "dream.json").read_text(encoding="utf-8"))
    assert kept["stats"]["themes"] == 0
    # The spy does see a count when the engine is left to its default: the check above is not vacuous.
    night_with(kw, tmp_path / "default", {k: v for k, v in kw["config"].items() if k != "themes"})
    assert counted
