"""Phase 3: the review. An answer is recorded only as one of the answers its section takes; it says which memory call
to make and never makes one; reject and not-duplicates go to the engine's own lists; undo touches no memory."""
import io
import json
import re

import pytest

from dreamem import questions, review, run

FRIDAY = "Магазин закрывается в девять вечера по пятницам"


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def quiet(*_):
    return None


def test_show_marks_questions_shown_and_prints_instructions(world):
    world.fact(FRIDAY, origin="inferred", days_old=2)
    night(world)
    lines = []
    review.show(world.homes, out=lines.append)
    text = "\n".join(lines)
    assert FRIDAY in text and "memory_stats" in text and "answers: yes, no" in text
    assert {q["status"] for q in world.questions()["questions"]} == {questions.SHOWN}


def test_an_answer_the_section_does_not_take_is_refused(world):
    world.fact(FRIDAY, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    lines = []
    assert review.decide(world.homes, q["qid"], "maybe", out=lines.append) == 2
    assert "takes:" in lines[0]
    assert world.questions()["questions"][0]["status"] == questions.GENERATED


def test_changed_needs_the_new_text(world):
    fid = world.fact(FRIDAY, origin="said", days_old=90, rc=0)
    world.say({1: ["касса сошлась"]})
    q = {"qid": "Q1", "section": "fact_decay", "fact_id": fid, "version_id": 1, "store": "x", "status": "shown"}
    with pytest.raises(ValueError, match="--text"):
        questions.decide(q, "changed")
    todo = questions.decide(q, "changed", "Магазин закрывается в десять вечера по пятницам")
    assert todo["call"] == {"tool": "memory_change", "args": {"fact_id": fid, "content":
                                                               "Магазин закрывается в десять вечера по пятницам",
                                                               "expect": {str(fid): 1}}}


def test_later_keeps_it_open_and_undo_reopens(world):
    world.fact(FRIDAY, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "later", out=quiet)
    assert world.questions()["questions"][0]["status"] == questions.SHOWN
    review.decide(world.homes, q["qid"], "yes", out=quiet)
    assert world.questions()["questions"][0]["status"] == questions.DECIDED
    lines = []
    assert review.decide(world.homes, q["qid"], "no", out=lines.append) == 2  # already decided
    review.mark(world.homes, q["qid"], "undo", out=quiet)
    after = world.questions()["questions"][0]
    assert after["status"] == questions.SHOWN and after["answer"] is None


def test_applied_only_after_a_decided_call(world):
    world.fact(FRIDAY, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    lines = []
    assert review.mark(world.homes, q["qid"], "applied", out=lines.append) == 2
    review.decide(world.homes, q["qid"], "yes", out=quiet)
    assert review.mark(world.homes, q["qid"], "applied", detail="ok", out=quiet) == 0
    assert world.questions()["questions"][0]["status"] == questions.APPLIED


def test_reject_puts_the_fact_on_the_core_reject_list(world):
    fid = world.fact(FRIDAY, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    lines = []
    assert review.decide(world.homes, q["qid"], "reject", out=lines.append) == 0
    # "never ask again" also stops recall offering it: the answer hands the agent that one call
    assert json.loads(lines[0])["call"] == {"tool": "memory_feedback", "args": {
        "fact_id": fid, "action": "unhelpful", "expect": {str(fid): world.version(fid)}}}
    rejected = json.loads((world.homes.profile("global") / "state" / "rejected.json")
                          .read_text(encoding="utf-8"))
    assert any(str(fid) in json.dumps(v) for v in rejected.values())
    # The call was not made yet: the engine asks only for it, never for the fact itself again.
    assert {x["section"] for x in night(world)["questions"] if x.get("fact_id") == fid} == {"trust_feedback"}
    world.sql("update facts set trust_score = 0.2 where fact_id = ?", (fid,))  # the call made, trust under the floor
    assert not [x for x in night(world)["questions"] if x.get("fact_id") == fid]


def test_decide_writes_no_memory(world):
    fid = world.fact(FRIDAY, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    before = world.tree(world.homes.holomem)
    for answer in ("yes",):
        review.decide(world.homes, q["qid"], answer, out=quiet)
    review.mark(world.homes, q["qid"], "undo", out=quiet)
    review.decide(world.homes, q["qid"], "forget", out=quiet)
    assert world.tree(world.homes.holomem) == before
    assert world.sql("select count(*) from facts where fact_id=?", (fid,))[0][0] == 1


def test_list_and_status(world, capsys):
    world.fact(FRIDAY, origin="inferred", days_old=2)
    night(world)
    lines = []
    review.listing(world.homes, out=lines.append)
    assert len(lines) == 1 and re.fullmatch(r"global Q1-[0-9a-f]{4} new_fact generated", lines[0])
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "profile global" in out.getvalue() and "open 1" in out.getvalue()


def test_one_question_about_a_fact_a_night(world):
    """A fact people keep saying is offered by the engine twice (promotion and confirm): the person is asked once."""
    fid = world.fact("Кассир Марина работает по субботам и воскресеньям", origin="said", days_old=50, rc=4, helpful=1)
    world.say({6: ["Кассир Марина сегодня опять работает в субботу"],
               4: ["Марина работает по субботам и воскресеньям, ставь её в график"],
               2: ["Марина в воскресенье тоже работает, кассир на месте"]})
    doc = night(world)
    about = [q for q in doc["questions"] if q.get("fact_id") == fid]
    assert len(about) >= 2  # the engine did offer it twice
    assert [q["status"] for q in about].count(questions.GENERATED) == 1
    assert doc["skipped"]["same_fact"] == len(about) - 1


def test_a_fact_ended_since_is_not_asked_about(world):
    """holographic-memory keeps an ended fact (its head version is `ended`): a night must not ask whether it still
    holds, and no answer may go out with a version the store does not hold as its head (a note from the
    holomem session, 2026-10-31: `expect` is checked against the head, not only `current`)."""
    fid = world.fact(FRIDAY, origin="inferred", days_old=2)
    world.end(fid)
    doc = night(world)
    assert [q for q in doc["questions"] if q.get("fact_id") == fid] == []


def test_the_review_never_tells_the_agent_to_write_without_expect(world):
    """An older holographic-memory ignores `expect` and writes unchecked, so it never "refuses" it (0.4.1 waited for a
    refusal that cannot come): the instructions of the review and of the skill make no call through a tool without
    an `expect` parameter, and keep no manual check to fall back on."""
    from importlib import resources

    world.fact(FRIDAY, origin="inferred", days_old=2)
    night(world)
    lines = []
    review.show(world.homes, out=lines.append)
    skill = resources.files("dreamem").joinpath("skill/SKILL.md").read_text(encoding="utf-8")
    for text in ("\n".join(lines), skill):
        assert "0.5.0" in text and "`expect` parameter" in text
        assert "memory_history {fact_id}" not in text and "refuses the `expect` argument" not in text
