"""Questions from holographic-memory's outcome journal.

Requirements: when an agent reports that an answer using a fact went wrong because of the fact (a stale version, or
another reason it did not narrow down) the person is asked whether the fact still holds — once per report, about the
version the answer saw. Reports about the search or the answer itself raise nothing; a report the agent revised away
raises nothing; a fact changed, ended or forgotten since raises nothing. The report is the agent's: it never counts as
the person's word, changes no memory, and its line is not shown when it holds a secret. The store is only read.
"""
import io
import json

from dreamem import questions, report, review, run

FRIDAY = "Магазин закрывается в девять вечера по пятницам"
DELIVERY = "Доставка по городу бесплатная от трёх тысяч"


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def asked(world):
    return [q for q in night(world)["questions"] if q["section"] == "outcome" and q["status"] in questions.OPEN]


def quiet(*_):
    return None


def test_a_fact_a_correction_names_is_asked_about(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    other = world.fact(DELIVERY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid, other], named=[fid],
                  correction="магазин теперь закрывается в десять")
    before = world.tree()
    qs = asked(world)
    assert world.tree() == before  # the journal and the store are read, never written
    assert [q["fact_id"] for q in qs] == [fid]  # the one the correction named, not everything the recall offered
    q = qs[0]
    assert q["content"] == FRIDAY and q["version_id"] == world.version(fid)
    text = report.question_text(q)
    assert "went wrong" in text and "магазин теперь закрывается в десять" in text and "not the person's" in text
    lines = []
    assert review.decide(world.homes, q["qid"], "changed", text="Магазин закрывается в десять вечера по пятницам",
                         out=lines.append) == 0
    assert '"memory_change"' in lines[0]


def test_reports_about_the_search_or_the_answer_raise_nothing(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    for outcome, reason in (("corrected", "irrelevant"), ("corrected", "wrong_entity"), ("dead_end", "answer_error"),
                            ("dead_end", "missing_evidence"), ("useful", "other"), ("unknown", "other")):
        world.outcome(outcome, reason, emitted=[fid], named=[fid])
    assert asked(world) == []


def test_a_fact_only_offered_is_asked_about_after_the_second_time(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("dead_end", "other", emitted=[fid])
    assert asked(world) == []
    world.outcome("corrected", "stale_version", emitted=[fid])
    qs = asked(world)
    assert [q["fact_id"] for q in qs] == [fid] and qs[0]["outcome"]["attempts"] == 2
    assert "offered in 2 answers that went wrong" in report.question_text(qs[0])


def test_a_report_revised_away_raises_nothing(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    attempt, event = world.outcome("corrected", "stale_version", emitted=[fid], named=[fid])
    world.outcome("useful", "other", attempt=attempt, supersedes=event)
    assert asked(world) == []


def test_a_fact_changed_or_forgotten_since_raises_nothing(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    gone = world.fact(DELIVERY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid, gone], named=[fid, gone])
    world.change(fid, "Магазин закрывается в десять вечера по пятницам")
    world.forget(gone)
    doc = night(world)
    assert [q for q in doc["questions"] if q["section"] == "outcome"] == []
    assert doc["skipped"].get("outcome:changed_since") == 1 and doc["skipped"].get("outcome:gone") == 1


def test_an_answered_report_is_not_asked_again_but_a_new_one_is(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid], named=[fid])
    q = asked(world)[0]
    review.decide(world.homes, q["qid"], "keep", out=quiet)
    assert asked(world) == []
    q = [q for q in world.questions()["questions"] if q["section"] == "outcome"]
    assert q == []  # answered: not even deferred
    world.outcome("dead_end", "stale_version", emitted=[fid], named=[fid])
    qs = asked(world)
    assert len(qs) == 1 and len(qs[0]["outcome"]["events"]) == 2  # the new report, with the old one beside it


def test_undo_asks_again(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid], named=[fid])
    q = asked(world)[0]
    review.decide(world.homes, q["qid"], "keep", out=quiet)
    review.mark(world.homes, q["qid"], "undo", out=quiet)
    assert [x["fact_id"] for x in asked(world)] == [fid]


def test_a_correction_holding_a_secret_is_not_shown(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    key = "sk-ant-api03-" + "A1b2C3d4E5f6G7h8I9j0" * 4
    world.outcome("corrected", "other", emitted=[fid], named=[fid], correction=f"ключ теперь {key}")
    qs = asked(world)
    assert len(qs) == 1 and qs[0]["outcome"]["correction"] is None
    for name in ("questions.json", "report.md"):
        assert key not in (world.homes.profile("global") / name).read_text(encoding="utf-8")


def test_the_session_line_counts_it_as_a_fact(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid], named=[fid])
    night(world)
    line = report.summary_line(json.loads(world.homes.summary.read_text(encoding="utf-8"))["profiles"])
    assert "1 fact" in line and FRIDAY not in line


def test_a_forgotten_fact_leaves_no_correction_behind(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid], named=[fid], correction="закрывается в десять")
    asked(world)
    world.forget(fid)
    run.forget_sweep(world.homes, out=io.StringIO())
    for name in ("questions.json", "report.md"):
        assert "закрывается в десять" not in (world.homes.profile("global") / name).read_text(encoding="utf-8")
