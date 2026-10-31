"""Possible new facts: what the person said about their life that memory does not hold.

Requirements: the pass offers the review a clause where the person says something lasting about themselves or their
people (commands, questions, code and what memory already holds are not offered) — as a reference, never as words: no
word of it in ~/.dreaming after the pass, after the review shows it, after any answer. The review reads the clause
again from the session file; a message changed since is never shown. "save" adds the fact the agent worded (and only
then is it recorded); "no" rests a month; "never", "not-a-fact", "already-stored" end it; undo brings it back. The
candidates do not take places of the other questions, and the session line counts them as one.
"""
import io
import json
from datetime import datetime, timedelta

import pytest

from dreamem import questions, report, review, run

LIFE = "Мы с мужем Олегом живём в Тбилиси уже пятый год"
DAY = "Сделай мне отчёт по продажам за неделю"
ASKED = "Как настроить cron на сервере"
PIECES = (LIFE, "Олегом", "Тбилиси", "пятый год", LIFE.lower(), LIFE[:20])


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def candidates_of(doc):
    return [q for q in doc["questions"] if q["section"] == "new_candidate"]


def no_words(world):
    raw = world.our_bytes()
    for piece in PIECES:
        assert piece.encode() not in raw, piece
        assert json.dumps(piece).encode() not in raw, piece  # as JSON writes it (\\u escapes)


def shown(world):
    lines = []
    review.show(world.homes, out=lines.append)
    return "\n".join(lines)


def test_a_life_fact_is_offered_as_a_reference_and_shown_read_again(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({3: [LIFE, DAY, ASKED], 1: [LIFE]})
    doc = night(world)
    cands = candidates_of(doc)
    assert len(cands) == 1 and cands[0]["days"] == 2  # the same clause on two days is one candidate
    no_words(world)
    text = shown(world)
    assert LIFE in text and "possible new fact" in text and "judge it first" in text
    assert DAY not in text.split("possible new fact")[1]  # commands and questions are not offered
    no_words(world)  # showing it saved nothing of it


def test_what_memory_holds_is_not_offered(world):
    world.fact("Анна с мужем Олегом живут в Тбилиси пятый год", origin="said", days_old=40)
    world.say({2: [LIFE]})
    assert candidates_of(night(world)) == []


def test_save_adds_the_worded_fact_and_then_keeps_only_its_id(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    shown(world)
    printed = []
    fact = "Анна с мужем Олегом живут в Тбилиси с 2021 года."
    assert review.decide(world.homes, q["qid"], "save", text=fact, out=printed.append) == 0
    todo = json.loads(printed[0])
    assert todo["call"] == {"tool": "memory_add", "args": {"content": fact, "origin": "said"}}
    assert review.mark(world.homes, q["qid"], "applied", fact_id=77, out=lambda *_: None) == 0
    after = next(x for x in world.questions()["questions"] if x["qid"] == q["qid"])
    assert after["status"] == questions.APPLIED and after["fact_id"] == 77
    assert "Тбилиси".encode() not in world.our_bytes()  # neither the clause nor the worded fact stays
    assert candidates_of(night(world)) == []  # recorded: not offered again


def test_save_without_an_id_is_refused(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    review.decide(world.homes, q["qid"], "save", text="Анна живёт в Тбилиси.", out=lambda *_: None)
    lines = []
    assert review.mark(world.homes, q["qid"], "applied", out=lines.append) == 2 and "--fact-id" in lines[0]


def test_no_rests_a_month(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    review.decide(world.homes, q["qid"], "no", out=lambda *_: None)
    assert candidates_of(night(world)) == []
    path = world.homes.profile("global") / "state" / questions.CANDIDATES_FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    for rec in data.values():
        rec["at"] = (datetime.now().astimezone() - timedelta(days=31)).isoformat(timespec="seconds")
    path.write_text(json.dumps(data), encoding="utf-8")
    assert len(candidates_of(night(world))) == 1


@pytest.mark.parametrize("answer", ["never", "not-a-fact", "already-stored"])
def test_an_ending_answer_ends_it(world, answer):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    assert review.decide(world.homes, q["qid"], answer, out=lambda *_: None) == 0
    assert candidates_of(night(world)) == []


def test_undo_brings_it_back(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    review.decide(world.homes, q["qid"], "never", out=lambda *_: None)
    review.mark(world.homes, q["qid"], "undo", out=lambda *_: None)
    assert len(candidates_of(night(world))) == 1


def test_a_message_changed_since_is_never_shown(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    path = world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    path.write_bytes(path.read_bytes().replace("пятый".encode(), "шестой".encode()))
    text = shown(world)
    assert "Тбилиси" not in text
    after = next(x for x in world.questions()["questions"] if x["qid"] == q["qid"])
    assert after["status"] == questions.GONE


def test_candidates_take_no_place_of_other_questions_and_count_as_one(world):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text("max_questions = 1\n", encoding="utf-8")
    world.fact("Магазин закрывается в девять вечера по пятницам", origin="inferred", days_old=2)
    world.fact("Склад открывается в восемь утра", origin="inferred", days_old=2)
    world.say({3: [LIFE, "Мой брат Гоша живёт в Батуми с женой"], 1: [LIFE]})
    doc = night(world)
    other = [q for q in doc["questions"] if q["section"] != "new_candidate"]
    assert sum(q["status"] == questions.GENERATED for q in other) == 1  # the budget is the other questions' own
    assert all(q["status"] == questions.GENERATED for q in candidates_of(doc)) and len(candidates_of(doc)) == 2
    summary = json.loads(world.homes.summary.read_text(encoding="utf-8"))
    line = report.summary_line(summary["profiles"])
    assert "1 possible new fact" in line and "Тбилиси" not in line


def test_a_dry_run_keeps_no_key(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    assert run.run(world.homes, dry_run=True, out=io.StringIO()) == 0
    assert not (world.homes.dreaming / "salt").exists()


def test_without_a_fact_store_nothing_is_offered(tmp_path):
    from world import World

    w = World(tmp_path, store=False)
    w.say({2: [LIFE]})
    assert run.run(w.homes, out=io.StringIO()) == 0
    for doc in w.homes.profiles.glob("*/questions.json"):
        assert candidates_of(json.loads(doc.read_text(encoding="utf-8"))) == []


def test_no_words_survive_a_failed_save_and_the_next_night(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]}, session="t1")
    world.say({1: ["Мой брат Гоша живёт в Батуми с женой"]}, session="t2")
    q = candidates_of(night(world))[0]
    shown(world)
    review.decide(world.homes, q["qid"], "save", text="Анна живёт в Тбилиси.", out=lambda *_: None)
    review.mark(world.homes, q["qid"], "failed", detail="duplicate", out=lambda *_: None)
    night(world)
    shown(world)
    raw = world.our_bytes()
    for piece in ("Олегом", "Батуми", "Гоша"):
        assert piece.encode() not in raw, piece


def test_the_newest_come_first_not_the_most_repeated(world):
    """Seen on a real home: the clauses said on many days were pasted briefs; a fact said once, lately, must not wait
    behind them for weeks."""
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text("max_candidates = 1\n", encoding="utf-8")
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({9: ["Мой бриф: у нас три бренда и одна палитра"], 8: ["Мой бриф: у нас три бренда и одна палитра"],
               7: ["Мой бриф: у нас три бренда и одна палитра"], 1: [LIFE]})
    shown_text = (night(world), shown(world))[1]
    assert LIFE in shown_text and "бриф" not in shown_text


# -- Codex review of 0.5.0 ---------------------------------------------------------------------------------------


def _saved_after_a_failure(world, reason):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    shown(world)
    quiet = lambda *_: None  # noqa: E731
    review.decide(world.homes, q["qid"], "save", text="Анна живёт в Тбилиси.", out=quiet)
    review.mark(world.homes, q["qid"], "failed", detail=reason, out=quiet)
    review.decide(world.homes, q["qid"], "save", text="Анна живёт в Тбилиси.", out=quiet)
    review.mark(world.homes, q["qid"], "applied", fact_id=77, out=quiet)
    return q


def test_a_failure_holding_words_is_gone_once_saved(world):
    _saved_after_a_failure(world, "Cannot save: Мы с мужем Олегом живём в Тбилиси")
    assert "Олегом".encode() not in world.our_bytes() and "Тбилиси".encode() not in world.our_bytes()


@pytest.mark.parametrize("answer", ["never", "no", "not-a-fact", "already-stored"])
def test_text_with_an_answer_other_than_save_is_not_kept(world, answer):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    review.decide(world.homes, q["qid"], answer, text=LIFE, out=lambda *_: None)
    assert "Олегом".encode() not in world.our_bytes()


def test_a_candidate_gone_after_a_failed_save_keeps_no_words(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    path = world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    quiet = lambda *_: None  # noqa: E731
    review.decide(world.homes, q["qid"], "save", text="Анна с Олегом живут в Тбилиси.", out=quiet)
    review.mark(world.homes, q["qid"], "failed", detail="busy", out=quiet)
    path.unlink()
    shown(world)
    after = next(x for x in world.questions()["questions"] if x["qid"] == q["qid"])
    assert after["status"] == questions.GONE and "Олегом".encode() not in world.our_bytes()


def test_a_gone_candidate_takes_no_answer(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    path = world.say({2: [LIFE]})
    q = candidates_of(night(world))[0]
    path.unlink()
    shown(world)
    for answer in ("save", "later"):
        lines = []
        assert review.decide(world.homes, q["qid"], answer, text="x y z", out=lines.append) == 2, answer
    assert next(x for x in world.questions()["questions"] if x["qid"] == q["qid"])["status"] == questions.GONE


def test_excluded_words_are_not_offered_nor_shown(world):
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text('exclude_patterns = ["Тбилиси"]\n', encoding="utf-8")
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    assert candidates_of(night(world)) == []


def test_broken_state_and_references_break_nothing(world):
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: [LIFE]})
    state = world.homes.profile("global") / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / questions.CANDIDATES_FILE).write_text('["old"]', encoding="utf-8")
    doc = night(world)
    assert len(candidates_of(doc)) == 1
    path = world.homes.profile("global") / "questions.json"
    for q in doc["questions"]:
        if q["section"] == "new_candidate":
            q["ref"]["ts"] = "invalid"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    shown(world)  # no crash; the candidate is gone


def test_pasted_briefs_and_attachments_are_not_offered(world):
    """Asked by Anton: briefs and specs pasted into a session are not the person's facts. A file attached, a long
    many-line message, the same clause pasted into three messages — none is offered; a life fact said once is."""
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    brief = "\n".join(["Бриф клиента:", "У нас три бренда и одна палитра", "Наша аудитория — малый бизнес",
                       "Мы работаем с 2015 года", "Наш офис в Москве", "Наши цвета — синий и белый",
                       "Мы продаём B2B", "Наш тон — дружелюбный"])
    world.say({6: [r'@"C:\Users\a\Desktop\ТЗ.txt" у нас в компании пять менеджеров по продажам', brief],
               5: ["Наша миссия — сделать бухгалтерию простой для всех"],
               4: ["Наша миссия — сделать бухгалтерию простой для всех"],
               3: ["Наша миссия — сделать бухгалтерию простой для всех"], 1: [LIFE]})
    night(world)
    text = shown(world)
    assert LIFE in text
    for piece in ("пять менеджеров", "три бренда", "миссия"):
        assert piece not in text, piece
