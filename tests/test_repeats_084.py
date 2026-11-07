"""0.8.4: what the person already answered is not asked again, and a fact is compared with a passage of a long
document, not with the whole document (Anton, 07.11.2026: confirmed facts came back every few days, and conflicts were
found with whole files of rules).

Data that showed it (holographic `fact_confirmations`): #35, #36, #38 confirmed 02.11 and again 07.11; #41, #43 on 02.11
and 06.11; #64 on 05.11 and 07.11 — each time as "people keep saying this — mark as confirmed?"."""
import io
from datetime import datetime, timedelta, timezone

from dreamem import run

FACT = "Кассир Марина работает по субботам и воскресеньям"
SAID = {6: ["Кассир Марина сегодня опять работает в субботу"],
        4: ["Марина работает по субботам и воскресеньям, ставь её в график"],
        2: ["Марина в воскресенье тоже работает, кассир на месте"]}


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def stamp(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")


def asked(doc, fid, sections=("promotion", "review", "confirm")):
    return [q["section"] for q in doc["questions"] if q.get("fact_id") == fid and q["section"] in sections]


def corroborated(world, origin="said"):
    fid = world.fact(FACT, origin=origin, days_old=50, rc=4, helpful=1)
    world.say(SAID)
    return fid


def test_the_store_offers_the_fact_when_nobody_confirmed_it(world):
    # The ground the other tests stand on: this fact is asked about when no confirmation is there.
    fid = corroborated(world)
    assert asked(night(world), fid)


def test_a_fact_the_person_confirmed_is_not_asked_to_be_confirmed_again(world):
    fid = corroborated(world)
    world.sql("update fact_versions set recorded_at=? where fact_id=?", (stamp(50), fid))
    world.sql("insert into fact_confirmations (fact_id, confirmed_at, via) values (?, ?, 'feedback')", (fid, stamp(5)))
    assert asked(night(world), fid) == []


def test_a_confirmation_counts_when_the_version_has_no_recorded_time(world):
    # An import or an older store leaves recorded_at empty: the confirmation still stands for the text there.
    fid = corroborated(world)
    world.sql("insert into fact_confirmations (fact_id, confirmed_at, via) values (?, ?, 'feedback')", (fid, stamp(5)))
    assert asked(night(world), fid) == []


def test_a_reviewed_fact_without_origin_is_not_offered_again_once_confirmed(world):
    # `review` "yes" confirms a fact of unknown origin; it then reads as `confirmed` and came back as a promotion.
    fid = corroborated(world, origin=None)
    world.sql("insert into fact_confirmations (fact_id, confirmed_at, via) values (?, ?, 'feedback')", (fid, stamp(5)))
    assert asked(night(world), fid) == []


def test_a_fact_changed_after_its_confirmation_may_be_asked_about_again(world):
    # Regression guard (passes on 0.8.3 too): a new text is not what the person confirmed.
    fid = corroborated(world)
    world.sql("update fact_versions set recorded_at=? where fact_id=?", (stamp(50), fid))
    world.sql("insert into fact_confirmations (fact_id, confirmed_at, via) values (?, ?, 'feedback')", (fid, stamp(20)))
    world.sql("update fact_versions set status='superseded' where fact_id=?", (fid,))
    world.sql("insert into fact_versions (fact_id, seq, content, status, recorded_at, src_origin) "
              "values (?, 2, ?, 'current', ?, 'said')", (fid, FACT + " с утра", stamp(3)))
    world.sql("update facts set content=? where fact_id=?", (FACT + " с утра", fid))
    assert asked(night(world), fid)


def test_a_fact_answered_last_night_is_not_asked_about_in_another_section(world):
    # #57 (06.11): confirmed as a promotion in the morning, asked as a conflict that night; #67: a promotion, then a
    # conflict the same day. A conflict's "keep" writes nothing, so no confirmation protects the fact either.
    from dreamem import review

    fid = world.fact(FACT + " с 9 до 18", origin="said", days_old=50, rc=4, helpful=1)
    world.say({6: ["Кассир Марина сегодня опять работает в субботу с 9 до 18"],
               4: ["Марина работает по субботам и воскресеньям с 9 до 18, ставь её в график"],
               2: ["Марина в воскресенье тоже работает с 9 до 18, кассир на месте"]})
    world.note("cashier", "кассир Марина", "Марина на кассе по субботам и воскресеньям с 10 до 19, график такой")
    first = night(world)
    shown = [q for q in first["questions"] if q.get("fact_id") == fid and q["status"] == "generated"]
    assert [q["section"] for q in shown] == ["conflict"]
    review.decide(world.homes, shown[0]["qid"], "keep", out=lambda *_: None)
    assert [q["section"] for q in night(world)["questions"] if q.get("fact_id") == fid] == []


def test_a_windows_file_of_rules_loses_only_its_secret_paragraph():
    # ~/.claude/CLAUDE.md is saved with CRLF: split on "\n\n" it was one paragraph, which held a secret word — and the
    # whole file of rules left the comparison (found on Anton's machine, 07.11.2026: 238 CRLF, no bare LF).
    from dreamem import screen
    from dreamem.notes import Note

    secret = "sk-proj-Zq81vXn4Lm2Tt7Rr9Yy3Uu5Ii1Oo6Pp0Aa"
    text = f"# Rules\r\n\r\nAnswer in Russian, briefly.\r\n\r\nThe deploy key is {secret}\r\n\r\nNever push to main.\r\n"
    kept, dropped = screen.dedupe_targets([Note("i1", "instructions", "", "CLAUDE.md", "CLAUDE.md", text)])
    assert dropped == 1
    assert len(kept) == 1
    assert "Answer in Russian, briefly." in kept[0].text and "Never push to main." in kept[0].text
    assert secret not in kept[0].text


RULES = "\n\n".join([
    "# Rules for the agent",
    "Сервер для тяжёлых задач трогать только с оглядкой на нагрузку, версия клиента 2.1.314.",
    "Диск ноутбука 476 GB, свободно около 90 GB на начало сентября.",
    "Память агентов ведётся в holographic, база заведена в 2026 году.",
    "Папка загрузок заполнена черновиками, их 12 штук — чистить раз в 7 дней.",
    "hr-ovh — общий бокс с Мией, дашборд на порту 9119.",
])
SERVER = "Сервер hr-ovh: диск заполнен на 83 процента, памяти 8 GB"


def memory_text(*docs):
    from dreamem.engine import dream

    return dream.load_durable_memory_text(dream.Memory(dedupe=docs))


def test_a_long_document_is_no_conflict_by_words_scattered_over_it():
    # Q2/Q3 of 07.11.2026: a fact "conflicted" with a whole file of rules or of Codex memory, whose hundreds of numbers
    # (versions, dates, ports) differ from the fact's; its words were spread over unrelated paragraphs.
    from dreamem.engine import dream

    assert dream.find_conflicts(SERVER, memory_text(RULES)) == []


def test_a_paragraph_about_the_same_thing_with_other_numbers_is_still_a_conflict():
    from dreamem.engine import dream

    doc = RULES + "\n\nСервер hr-ovh: диск заполнен на 70 процентов, памяти 8 GB, следить еженедельно."
    hits = dream.find_conflicts(SERVER, memory_text(doc))
    assert len(hits) == 1 and "70" in hits[0]["memory_numbers"]


def test_a_fact_is_not_already_in_memory_by_words_scattered_over_a_document():
    # The same whole-document comparison hid 12 of Anton's facts as "already in memory" (e.g. "Anton's cat is Mia"
    # against a whole AGENTS.md): they were never offered.
    from dreamem.engine import dream

    assert not dream.already_in_memory(SERVER, memory_text(RULES))


def test_a_fact_a_paragraph_already_says_is_still_in_memory():
    from dreamem.engine import dream

    assert dream.already_in_memory(SERVER, memory_text(RULES + "\n\n" + SERVER + "."))


def test_a_list_item_is_its_own_unit():
    from dreamem.engine import dream

    doc = "Сайты:\n- obmen.itparty.club — курс и калькулятор\n- " + SERVER + "\n- mafia.itparty.club — прод"
    assert dream.already_in_memory(SERVER, memory_text(RULES + "\n\n" + doc))
    assert dream.find_conflicts(SERVER.replace("83", "70"), memory_text(doc))


def conflicting(world, *, confirmed_days_ago=None):
    fid = world.fact(SERVER, origin="said", days_old=40)
    if confirmed_days_ago is not None:
        world.sql("insert into fact_confirmations (fact_id, confirmed_at, via) values (?, ?, 'feedback')",
                  (fid, stamp(confirmed_days_ago)))
    return fid


def test_a_fact_confirmed_lately_is_not_a_conflict_with_a_file_of_rules(world):
    # The person said a day ago that the fact holds; a file of rules (which the review never edits) disagreeing
    # with it is not a new question for them.
    fid = conflicting(world, confirmed_days_ago=1)
    (world.homes.claude / "CLAUDE.md").write_text(
        RULES + "\n\nСервер hr-ovh: диск заполнен на 70 процентов, памяти 8 GB, следить еженедельно.\n",
        encoding="utf-8")
    assert asked(night(world), fid, ("conflict",)) == []


def test_without_a_confirmation_the_same_file_of_rules_is_a_conflict(world):
    fid = conflicting(world)
    (world.homes.claude / "CLAUDE.md").write_text(
        RULES + "\n\nСервер hr-ovh: диск заполнен на 70 процентов, памяти 8 GB, следить еженедельно.\n",
        encoding="utf-8")
    assert asked(night(world), fid, ("conflict",)) == ["conflict"]


def test_a_claude_note_disagreeing_with_a_confirmed_fact_is_still_asked_about(world):
    # The note may be the stale side, and the review can fix a note (note-changed / note-outdated).
    fid = conflicting(world, confirmed_days_ago=1)
    world.note("server", "сервер hr-ovh", "Сервер hr-ovh: диск заполнен на 70 процентов, памяти 8 GB.")
    assert asked(night(world), fid, ("conflict",)) == ["conflict"]


def test_keep_on_a_conflict_records_that_the_fact_holds():
    # "keep" wrote nothing, so nothing kept the same conflict from coming back after its cooldown; it now confirms
    # the fact, as "keep" of a relevance question always did.
    from dreamem import questions

    q = {"qid": "Q1-abcd", "section": "conflict", "status": questions.SHOWN, "fact_id": 7, "version_id": 12,
         "content": "x", "store": "C:/holo/memory_store.db"}
    todo = questions.decide(q, "keep", None)
    assert todo["call"]["tool"] == "memory_feedback"
    assert todo["call"]["args"]["action"] == "confirmed" and todo["call"]["args"]["expect"] == {"7": 12}


WAREHOUSE = "Склад на улице Ленина принимает товар по вторникам"


def test_a_fact_nobody_mentioned_for_long_is_asked_whether_it_still_holds(world):
    # The ground for the next test.
    fid = world.fact(WAREHOUSE, origin="said", days_old=120, trust=0.5)
    assert asked(night(world), fid, ("fact_decay",)) == ["fact_decay"]


def test_a_fact_confirmed_lately_is_not_asked_whether_it_still_holds(world):
    fid = world.fact(WAREHOUSE, origin="said", days_old=120, trust=0.5)
    world.sql("insert into fact_confirmations (fact_id, confirmed_at, via) values (?, ?, 'feedback')", (fid, stamp(2)))
    assert asked(night(world), fid, ("fact_decay",)) == []
