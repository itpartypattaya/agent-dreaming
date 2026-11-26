"""0.9.0: the review agent closes a technical question itself, on its own check, and says what it checked.

Anton, 26.11.2026: a question the agent can settle by checking (a version on the server, a path, what a transcript
says, two facts that say the same) it closes without him; only where it finds no clear confirmation does it ask; he
gets a summary of what was closed, merged and left to him. What it closes must not read as his word, must not come back
the next night, and must not erase anything a store cannot give back (reviews of the plan by Codex and Fable).
"""
import io
import json

from dreamem import questions, review, run

FRIDAY = "Магазин закрывается в девять вечера по пятницам"
COFFEE = ("Поставщик кофе Arabica Beans привозит зерно по вторникам утром",
          "Поставщик кофе Arabica Beans привозит зёрна по вторникам")
VERSION = "Плагин holographic на сервере — версии 0.30.2"
CHECKED = "ssh app-box: plugins/holographic/plugin.yaml says 0.31.7"


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def printed(fn, *args, **kw):
    lines = []
    code = fn(*args, out=lines.append, **kw)
    return code, "\n".join(lines)


def quiet(*_):
    return None


def only(world):
    return world.questions()["questions"][0]


def about(doc, fid):
    return [q for q in doc["questions"] if q.get("fact_id") == fid]


def fact_q(section, **extra):
    return {"qid": "Q1-ab12", "section": section, "fact_id": 7, "version_id": 3, "store": "x", "status": "shown",
            **extra}


def refused(q, answer, text=None):
    try:
        questions.decide(q, answer, text, by_agent=CHECKED)
    except ValueError as exc:
        assert "only the person" in str(exc), exc
        assert q["status"] == "shown" and not q.get("answer") and "by" not in q
        return True
    return False


# -- what the agent closes, and how it is recorded --------------------------------------------------------------------


def test_the_agent_closes_a_new_fact_on_its_check_and_it_is_recorded_as_its_own(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    assert q["section"] == "new_fact"
    code, out = printed(review.decide, world.homes, q["qid"], "outdated", verified=CHECKED)
    assert code == 0 and '"memory_end"' in out
    after = only(world)
    assert after["status"] == questions.DECIDED and after["by"] == "agent" and after["verified"] == CHECKED
    _, listed = printed(review.listing, world.homes)
    assert "by the agent" in listed and CHECKED in listed
    report = (world.homes.profile("global") / "report.md").read_text(encoding="utf-8")
    assert "Waiting for the memory call" in report and "Closed by the agent" not in report  # not made yet
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    report = (world.homes.profile("global") / "report.md").read_text(encoding="utf-8")
    assert "Closed by the agent on its own check" in report and CHECKED in report and "Waiting" not in report


def test_the_agents_it_holds_writes_nothing_to_memory_and_is_not_the_persons_confirmation(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    before = world.tree(world.homes.holomem)
    code, out = printed(review.decide, world.homes, q["qid"], "yes", verified=CHECKED)
    assert code == 0 and "no memory call needed" in out and "confirmed" not in out
    assert world.tree(world.homes.holomem) == before
    assert only(world)["by"] == "agent"
    assert questions.decide(fact_q("conflict"), "keep", by_agent=CHECKED) == {}
    assert questions.decide(fact_q("conflict"), "keep")["call"]["args"]["action"] == "confirmed"  # the person's


def test_a_new_fact_takes_changed_the_persons_said_the_agents_inferred():
    """Night 25.11: a version an agent stored had moved on; there was no `changed`, so the agent answered `later` and
    called memory_change past dreamem."""
    new = "Плагин holographic на сервере — версии 0.31.7"
    by_person = questions.decide(fact_q("new_fact", fact_id=135, version_id=2), "changed", new)
    assert by_person["call"] == {"tool": "memory_change", "args": {"fact_id": 135, "content": new, "origin": "said",
                                                                    "expect": {"135": 2}}}
    by_agent = questions.decide(fact_q("new_fact", origin="inferred"), "changed", new, by_agent=CHECKED)
    assert by_agent["call"]["tool"] == "memory_change" and by_agent["call"]["args"]["origin"] == "inferred"
    assert questions.decide(fact_q("review"), "changed", new)["call"]["tool"] == "memory_change"


def test_the_agent_merges_a_duplicate_and_the_night_is_acknowledged_by_who_answered(world):
    world.fact(COFFEE[0], origin="said", days_old=40)
    world.fact(COFFEE[1], origin="said", days_old=3)
    world.fact(VERSION, origin="inferred", days_old=2)
    doc = night(world)
    dup = next(q for q in doc["questions"] if q["section"] == "duplicate")
    new = next(q for q in doc["questions"] if q["section"] == "new_fact")
    code, out = printed(review.decide, world.homes, dup["qid"], "yes", verified="the same supplier and day")
    assert code == 2 and "only the person" in out  # a retelling, not the same text: the person's to judge
    data = world.questions()
    for q in data["questions"]:  # the same text saved twice: the agent's to merge
        if q["qid"] == dup["qid"]:
            q["copy"]["content"] = q["keep"]["content"]
    (world.homes.profile("global") / "questions.json").write_text(json.dumps(data, ensure_ascii=False),
                                                                encoding="utf-8")
    code, out = printed(review.decide, world.homes, dup["qid"], "yes", verified="the same text twice")
    assert code == 0 and '"memory_merge"' in out
    review.mark(world.homes, dup["qid"], "applied", out=quiet)
    review.decide(world.homes, new["qid"], "yes", out=quiet)
    review.mark(world.homes, new["qid"], "applied", out=quiet)
    state = world.homes.profile("global") / "state"
    text = next(iter(questions.acks_for(state, world.questions()).values()))[0]
    assert f"Answered by the person: {new['qid']} yes." in text
    assert f"Closed by the agent on its own check: {dup['qid']} yes." in text
    assert dup["proposal"] in text  # the engine still learns which merge was answered
    legacy = world.questions()  # a night answered before 0.9.0 names nobody: read as before
    for q in legacy["questions"]:
        q.pop("by", None)
    assert "Closed by the agent" not in next(iter(questions.acks_for(state, legacy).values()))[0]


def test_undo_clears_the_agents_record(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "outdated", verified=CHECKED, out=quiet)
    review.mark(world.homes, q["qid"], "undo", out=quiet)
    after = only(world)
    assert "by" not in after and "verified" not in after
    code, out = printed(review.decide, world.homes, q["qid"], "yes")
    assert code == 0 and '"confirmed"' in out and only(world)["by"] == "person"


# -- what only the person answers -------------------------------------------------------------------------------------


def test_what_only_the_person_can_answer_is_refused_to_the_agent(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    for answer in ("forget", "reject", "no", "wrong"):
        code, out = printed(review.decide, world.homes, q["qid"], answer, text="x", verified=CHECKED)
        assert code == 2 and "only the person" in out, answer
        assert only(world)["status"] == questions.GENERATED and only(world).get("answer") is None
    for section, answer in (("promotion", "yes"), ("confirm", "yes"), ("trust_feedback", "yes"),
                            ("fact_decay", "forget"), ("outcome", "forget"), ("conflict", "wrong")):
        assert refused(fact_q(section), answer, "x"), (section, answer)
    candidate = {"qid": "Q9-ab12", "section": "new_candidate", "ref": {}, "clause": "c", "store": "x",
                 "status": "shown"}
    assert refused(candidate, "not-a-fact")  # judged as before, without --verified


def test_the_agent_never_ends_or_rewrites_the_persons_own_words():
    """Fable review: an unknown origin may be the person's words stored before origins were kept."""
    for origin in ("said", "confirmed", None):
        for section in ("conflict", "fact_decay", "outcome", "new_fact"):
            for answer in ("changed", "outdated"):
                assert refused(fact_q(section, origin=origin), answer, "новый текст"), (origin, section, answer)
            hold = "yes" if section == "new_fact" else "keep"
            assert questions.decide(fact_q(section, origin=origin), hold, by_agent=CHECKED) == {}


def test_the_agent_never_erases_what_a_store_cannot_give_back():
    """MCP memory and Claude Code notes keep no old text: a delete or a rewrite there is the person's."""
    mcp = dict(provider="mcp-memory", store="m.jsonl", locator={"entity": "Server", "text": "holographic 0.30.2"})
    person = questions.decide(fact_q("new_fact", **mcp), "changed", "holographic 0.31.7")
    assert [c["tool"] for c in person["calls"]] == ["add_observations", "delete_observations"]
    for answer in ("changed", "outdated"):
        assert refused(fact_q("new_fact", **mcp), answer, "holographic 0.31.7")
    note = {"id": "n", "project": "p", "path": "C:/x/note.md", "name": "note", "sha256": "0" * 64}
    for section, answer in (("note_decay", "outdated"), ("note_decay", "changed"), ("conflict", "note-outdated"),
                            ("conflict", "note-changed")):
        assert refused(fact_q(section, note=note), answer, "new body"), (section, answer)


def test_the_agent_drops_or_merges_only_a_copy_that_adds_nothing():
    keep = {"fact_id": 1, "version_id": 1, "content": COFFEE[0]}
    more = {"fact_id": 2, "version_id": 1, "content": COFFEE[1], "covered": False, "extra_words": ["зёрна"]}
    dup = {"qid": "Q1-ab12", "section": "duplicate", "proposal": "D-1", "keep": keep, "copy": more, "store": "x",
           "status": "shown"}
    assert refused(dup, "yes")
    swapped = {**dup, "keep": {**keep, "content": "Alice pays Bruno 50 every month"},
               "copy": {**more, "content": "Bruno pays Alice 50 every month", "covered": True}}
    assert refused(swapped, "yes")  # every word shared, the meaning turned round (Codex review)
    twin = {**dup, "copy": {**more, "content": "  " + COFFEE[0].replace(" ", "  "), "covered": True}}
    assert questions.decide(twin, "yes", by_agent=CHECKED)["call"]["tool"] == "memory_merge"
    loc = {"entity": "Shop", "text": "t"}
    ext = {"qid": "Q2-ab12", "section": "external_duplicate", "provider": "mcp-memory", "store": "m.jsonl",
           "status": "shown", "pair": "p", "keep": {"fact_id": 1, "content": "Shop opens at 9", "locator": loc},
           "copy": {"fact_id": 2, "content": "Shop opens at 9 on weekdays", "locator": loc}}
    assert refused(ext, "drop-copy")
    assert refused({**ext, "copy": {**ext["copy"], "content": "shop opens at 9"}}, "drop-copy")  # case is meaning
    same = {**ext, "status": "shown", "copy": {**ext["copy"], "content": " Shop  opens at 9"}}
    assert questions.decide(same, "drop-copy", by_agent=CHECKED)["call"]["tool"] == "delete_observations"
    note = {"id": "n", "project": "p", "path": "C:/x/n.md", "name": "n", "sha256": "0" * 64}
    pair = {"qid": "Q3-ab12", "section": "note_duplicate", "status": "shown", "pair": "p", "note": note,
            "content": "Shop opens at 9", "fact": {"fact_id": 1, "content": "Shop opens at 9"}}
    assert questions.decide(dict(pair), "drop-note", by_agent=CHECKED)["edit"]["action"] == "delete"
    assert refused({**pair, "fact": {"fact_id": 1, "content": "It is not true that Shop opens at 9"}}, "drop-note")


def test_the_check_must_be_named_and_hold_no_secret(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    code, out = printed(review.decide, world.homes, q["qid"], "yes", verified="   ")
    assert code == 2 and "--verified" in out
    code, out = printed(review.decide, world.homes, q["qid"], "yes", verified="checked with token ghp_" + "a" * 36)
    assert code == 2 and "secret" in out
    assert only(world)["status"] == questions.GENERATED


# -- the next nights --------------------------------------------------------------------------------------------------


def test_a_text_the_agent_wrote_is_not_asked_about_the_next_night(world):
    """Fable P1: memory_change makes a new version — a new text, `inferred`, fresh — which the night would take for a
    new fact an agent stored, and the agent would check its own text again."""
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    new = "Плагин holographic на сервере — версии 0.31.7"
    review.decide(world.homes, q["qid"], "changed", text=new, verified=CHECKED, out=quiet)
    world.correct(fid, new, origin="inferred")  # what the printed call does to the store
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    assert not about(night(world), fid)


def test_a_fact_the_agent_checked_rests(world):
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", verified=CHECKED, out=quiet)
    assert questions.agent_checked(world.homes.profile("global") / "state")
    for _ in range(2):
        assert not about(night(world), fid)


def test_a_decided_call_never_made_comes_back_the_next_night(world):
    """Codex P2: a session that ended (or a store that refused) before the call leaves memory as it was; the question
    must not rest as answered."""
    fid = world.fact(FRIDAY, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", out=quiet)  # prints memory_feedback; never made
    again = about(night(world), fid)
    assert again and again[0]["section"] == "new_fact"
    review.decide(world.homes, again[0]["qid"], "yes", out=quiet)
    review.mark(world.homes, again[0]["qid"], "applied", out=quiet)
    assert not about(night(world), fid)


def test_an_outcome_whose_call_failed_comes_back(world):
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid], named=[fid], correction="теперь в десять")
    q = next(q for q in night(world)["questions"] if q["section"] == "outcome")
    review.decide(world.homes, q["qid"], "changed", text="Магазин закрывается в десять вечера по пятницам", out=quiet)
    review.mark(world.homes, q["qid"], "failed", detail="changed_since", out=quiet)
    assert [x["section"] for x in about(night(world), fid)] == ["outcome"]


def test_forgetting_the_fact_removes_the_agents_account_of_its_check(world):
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "outdated", verified=f"server says: {VERSION} is gone", out=quiet)
    world.forget(fid)
    run.forget_sweep(world.homes, out=io.StringIO())
    assert "verified" not in only(world)
    assert VERSION not in (world.homes.profile("global") / "report.md").read_text(encoding="utf-8")


# -- after the code review (Codex and Fable) --------------------------------------------------------------------------


def test_a_fact_the_person_confirmed_after_the_night_is_not_the_agents_to_rewrite(world):
    """A confirmation keeps the version: the night's snapshot still says `inferred`."""
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    world.sql("insert into fact_confirmations (fact_id, confirmed_at) values (?, '2026-11-26 10:00:00')", (fid,))
    code, out = printed(review.decide, world.homes, q["qid"], "outdated", verified=CHECKED)
    assert code == 2 and "only the person" in out and "confirmed" in out
    assert only(world).get("answer") is None
    world.change(fid, "Плагин holographic на сервере — версии 0.31.7")  # changed since: back as it is now
    code, out = printed(review.decide, world.homes, q["qid"], "outdated", verified=CHECKED)
    assert code == 1 and "changed" in out and only(world)["status"] == questions.FAILED


def test_a_rest_holds_one_text_of_one_fact_against_one_contradiction():
    """A rest keyed by the text alone hid a new contradiction, and an edited text that read the same to the engine."""
    conflicts = [{"entry": "версия 0.30.2 в заметке"}]
    item = {"fact_id": 5, "content": VERSION, "conflicts": conflicts}
    facts = {5: {"version_id": 2, "content": VERSION, "origin": "inferred", "status": "current"}}

    def asked(result, checked):
        qs, skipped = questions.build(result, store="x", facts=facts, notes_index={}, coverage_ok=True,
                                      max_questions=7, profile="global", checked=checked)
        return [q["section"] for q in qs]
    assert asked({"conflicts": [item]}, {questions.rest_key(5, VERSION, conflicts)}) == []
    newer = {**item, "conflicts": [{"entry": "версия 0.32.0 в заметке"}]}
    assert asked({"conflicts": [newer]}, {questions.rest_key(5, VERSION, conflicts)}) == ["conflict"]
    assert asked({"new_facts": [item]}, {questions.rest_key(5, VERSION)}) == []
    assert asked({"new_facts": [item]}, {questions.rest_key(5, VERSION.upper())}) == ["new_fact"]
    assert asked({"new_facts": [item]}, {questions.rest_key(6, VERSION)}) == ["new_fact"]  # another fact


def test_a_rest_ends_after_a_month_and_holds_no_words(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", verified=CHECKED, out=quiet)
    path = world.homes.profile("global") / "state" / questions.AGENT_FILE
    raw = path.read_text(encoding="utf-8")
    assert "holographic" not in raw and "app-box" not in raw and "0.30.2" not in raw
    data = json.loads(raw)
    for rec in data.values():
        rec["at"] = "2026-01-01T00:00:00+07:00"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert questions.agent_checked(path.parent) == set()


def test_outdated_or_a_failed_hold_leaves_no_rest(world):
    world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    state = world.homes.profile("global") / "state"
    review.decide(world.homes, q["qid"], "outdated", verified=CHECKED, out=quiet)
    review.mark(world.homes, q["qid"], "applied", out=quiet)
    assert questions.agent_checked(state) == set()  # the fact ended: nothing to rest; the text may come back as new
    review.mark(world.homes, q["qid"], "undo", out=quiet)
    review.decide(world.homes, q["qid"], "yes", verified=CHECKED, out=quiet)
    assert questions.agent_checked(state)
    review.mark(world.homes, q["qid"], "failed", detail="session ended", out=quiet)
    assert questions.agent_checked(state) == set()


def test_forget_scrubs_an_answered_question_and_drops_the_rest(world):
    fid = world.fact(VERSION, origin="inferred", days_old=2)
    q = night(world)["questions"][0]
    review.decide(world.homes, q["qid"], "yes", verified=f"the server still says {VERSION}", out=quiet)
    world.forget(fid)
    review.show(world.homes, out=quiet)  # where a review starts: an answered question is scrubbed too
    assert "verified" not in only(world)
    assert VERSION not in (world.homes.profile("global") / "report.md").read_text(encoding="utf-8")
    run.forget_sweep(world.homes, out=io.StringIO())
    assert questions.agent_checked(world.homes.profile("global") / "state") == set()


def test_a_note_is_dropped_as_a_copy_only_while_the_kept_one_is_as_shown(world):
    body = "Обжарщик Петрович берёт заказы на обжарку до обеда."
    world.note("roastery", "Обжарка зёрен по средам у Петровича", body)
    world.note("roasting", "Обжарка зёрен у Петровича по средам", body)
    world.say({1: ["касса сошлась"]})
    q = next(q for q in night(world)["questions"] if q["section"] == "note_duplicate")
    kept = q["other"]["path"]
    with open(kept, "a", encoding="utf-8") as f:
        f.write("Только по средам.\n")
    code, out = printed(review.decide, world.homes, q["qid"], "drop-note", verified="the same text")
    assert code == 1 and "changed" in out


def test_an_outcome_decided_under_0_8_whose_call_was_never_made_comes_back(world):
    """0.8 marked an outcome's reports answered at `decide`; a call never made left them answered for good."""
    fid = world.fact(FRIDAY, origin="said", days_old=40)
    world.outcome("corrected", "stale_version", emitted=[fid], named=[fid])
    q = next(q for q in night(world)["questions"] if q["section"] == "outcome")
    review.decide(world.homes, q["qid"], "changed", text="Магазин закрывается в десять вечера по пятницам", out=quiet)
    decided = next(x for x in world.questions()["questions"] if x["qid"] == q["qid"])
    questions.answer_outcomes(world.homes.profile("global") / "state", decided, True)  # what 0.8 recorded
    assert [x["section"] for x in about(night(world), fid)] == ["outcome"]
