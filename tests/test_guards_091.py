"""0.9.1: two guards around the agent's own answers (Anton, 27.11.2026, after 0.9.0).

1. A preference is the person's: the agent may not close a question about a fact stored as one (holographic-memory's
   category `user_pref`) — not "it holds", not a correction, not a merge — whatever it calls technical.
2. An answer recorded as the person's must have the person behind it. dreamem cannot tell them from the agent when
   the answer is recorded (both run the same command), so the next night it looks: no message of the person's read
   around the time of the answer → a question to them, "was this your answer?".
"""
import io
import time

from dreamem import questions, report, review, run

VERSION = "Плагин holographic на сервере — версии 0.30.2"
SHORT = "Антон предпочитает короткие ответы без вступлений"
CHECKED = "ssh app-box: plugin.yaml says 0.31.7"


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def printed(fn, *args, **kw):
    lines = []
    code = fn(*args, out=lines.append, **kw)
    return code, "\n".join(lines)


def quiet(*_):
    return None


def preference(world, text, **kw):
    fid = world.fact(text, **kw)
    world.sql("update facts set category='user_pref' where fact_id=?", (fid,))
    return fid


def person_says(world, text, *, session="review", ago=0.0):
    """A line the person typed in Claude Code, *ago* seconds back."""
    return world.claude_file([world.claude_line(text, ts=time.time() - ago, session=session)], session=session)


def unbacked(doc):
    return [q for q in doc["questions"] if q["section"] == "unbacked"]


# -- 1. a preference is the person's --------------------------------------------------------------------------------


def test_the_agent_may_not_close_a_question_about_a_preference(world):
    preference(world, SHORT, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    assert q["section"] == "new_fact"
    for answer, text in (("yes", None), ("changed", "Антон предпочитает развёрнутые ответы"), ("outdated", None)):
        code, out = printed(review.decide, world.homes, q["qid"], answer, text=text, verified="it reads technical")
        assert code == 2 and "preference" in out, answer
        assert world.questions()["questions"][0].get("answer") is None
    code, out = printed(review.decide, world.homes, q["qid"], "yes")  # the person's own answer
    assert code == 0 and '"confirmed"' in out


def test_the_agent_may_not_merge_a_preference(world):
    world.fact("Поставщик кофе Arabica Beans привозит зерно по вторникам утром", origin="said", days_old=40)
    preference(world, "Поставщик кофе Arabica Beans привозит зёрна по вторникам", origin="said", days_old=3)
    dup = next(q for q in night(world)["questions"] if q["section"] == "duplicate")  # one side a preference
    code, out = printed(review.decide, world.homes, dup["qid"], "yes", verified="the same words in another order")
    assert code == 2 and "preference" in out


def test_a_technical_fact_stays_the_agents(world):
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    world.sql("update facts set category='tool' where fact_id=?", (fid,))
    q = night(world)["questions"][0]
    code, _ = printed(review.decide, world.homes, q["qid"], "yes", verified=CHECKED)
    assert code == 0


# -- 2. an answer recorded as the person's, checked the next night ---------------------------------------------------


def test_a_persons_answer_with_their_message_beside_it_raises_nothing(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    person_says(world, "да, верно", ago=60)
    review.decide(world.homes, q["qid"], "yes", out=quiet)
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    assert unbacked(night(world)) == []


def test_an_answer_recorded_as_the_persons_with_no_message_of_theirs_is_asked_about(world):
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", out=quiet)  # recorded as the person's; nobody said anything
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    doc = night(world)
    found = unbacked(doc)
    assert len(found) == 1 and found[0]["fact_id"] == fid
    assert doc["questions"][0]["section"] == "unbacked"  # asked first: memory may hold an answer nobody gave
    assert found[0]["about"]["qid"] == q["qid"] and found[0]["about"]["answer"] == "yes"
    assert set(questions.answers(found[0])) == {"mine", "not-mine"}
    text = report.question_text(found[0])
    assert q["qid"] in text and "yes" in text and VERSION not in text  # no fact text is needed to ask it
    code, out = printed(review.decide, world.homes, found[0]["qid"], "mine", verified="I think it was theirs")
    assert code == 2 and "only the person" in out
    summary = report.summary_line({"global": {"open": 1, "sections": {"unbacked": 1}, "generated_at": "2026-11-27"}})
    assert "answer" in summary


def test_not_mine_undoes_a_change_and_says_when_nothing_can_be_undone(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "changed", text="Плагин holographic на сервере — версии 0.31.7", out=quiet)
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    found = unbacked(night(world))[0]
    code, out = printed(review.decide, world.homes, found["qid"], "not-mine")
    assert code == 0 and '"memory_revert"' in out and f'"fact_id": {found["fact_id"]}' in out
    yes_q = {"qid": "Q1-ab12", "section": "unbacked", "fact_id": 7, "store": "x", "status": "shown",
             "about": {"qid": "Q2-cd34", "section": "new_fact", "answer": "yes"}}
    assert questions.decide(yes_q, "not-mine") == {}  # a confirmation has no undo call: the review says so


def test_the_agents_own_answers_are_not_audited(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", verified=CHECKED, out=quiet)
    assert unbacked(night(world)) == []


def test_an_unanswered_check_comes_back_an_answered_one_does_not(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", out=quiet)
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    first = unbacked(night(world))[0]
    again = unbacked(night(world))  # nobody answered it: still there, about the same answer
    assert len(again) == 1 and again[0]["about"] == first["about"]
    person_says(world, "да, это был мой ответ", ago=30)
    review.decide(world.homes, again[0]["qid"], "mine", out=quiet)
    assert unbacked(night(world)) == []


def test_no_check_when_the_sessions_were_read_only_in_part(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", out=quiet)
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    p = world.rollout([(time.time() - 3600, "user", "x")], zst=True, plain=False)[0]
    p.write_bytes(b"(\xb5/\xfd broken")  # a Codex session that cannot be read: its messages may be the person's
    doc = night(world)
    assert doc["coverage"]["codex"]["status"] != "complete"
    assert unbacked(doc) == [] and doc["skipped"].get("unbacked:coverage") == 1
