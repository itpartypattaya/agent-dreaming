"""The CLIs' own memory as a memory system: Claude Code notes (auto memory and subagents' memory) answered into by the
agent of the review — an edit of the note, after `dreamem verify` shows it is the file the person was shown — and
Codex memories, read only."""
import io
import json
from pathlib import Path

from dreamem import questions, review, run

OPENS = "Кофейня на Садовой открывается в 9 утра по будням без перерыва"
NOTE_OPENS = "Кофейня на Садовой открывается в 10 утра по будням без перерыва, бариста приходит заранее"
ROOT = Path(__file__).resolve().parent.parent


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions()


def lines_of(fn, *a, **k):
    out = []
    code = fn(*a, out=out.append, **k)
    return code, out


def ask(world, section):
    return [q for q in night(world)["questions"] if q["section"] == section]


def test_a_project_setting_that_moves_notes_is_named_not_read(world):
    moved = world.root / "elsewhere"
    moved.mkdir()
    (moved / "deploy.md").write_text("---\nname: deploy\ndescription: Деплой через rsync\n---\n\nrsync -av dist/ shop:\n",
                                     encoding="utf-8")
    (world.project / ".claude").mkdir()
    (world.project / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(moved)}),
                                                            encoding="utf-8")
    world.say({1: ["касса сошлась"]})
    doc = night(world)
    assert not [q for q in doc["questions"] if q["section"] == "note_decay"]
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert str(moved) in out.getvalue() and "not read" in out.getvalue()


def test_subagent_memory_is_read(world):
    folder = world.homes.claude / "agent-memory" / "reviewer"
    folder.mkdir(parents=True)
    (folder / "style.md").write_text("---\nname: style\ndescription: Ревьюер просит короткие коммиты\n---\n\n"
                                     "Коммиты в одну строку, без эмодзи.\n", encoding="utf-8")
    local = world.project / ".claude" / "agent-memory-local" / "tester"
    local.mkdir(parents=True)
    (local / "fixtures.md").write_text("---\nname: fixtures\ndescription: Фикстуры кассы лежат в tests/data\n---\n\n"
                                       "Файлы чеков в формате csv.\n", encoding="utf-8")
    world.say({1: ["касса сошлась"]})
    notes = {q["note"]["name"]: q["note"]["project"] for q in ask(world, "note_decay")}
    assert notes == {"style": "agent:reviewer", "fixtures": "agent:tester"}


def test_codex_memory_of_an_unknown_shape_makes_coverage_partial(world):
    folder = world.homes.codex / "memories_v3"
    folder.mkdir(parents=True)
    (folder / "topics.md").write_text("Магазин открывается в 10 утра", encoding="utf-8")
    world.note("deploy", "Деплой сайта магазина через rsync", "rsync -av dist/ shop:/var/www")
    world.say({1: ["касса сошлась"]})
    doc = night(world)
    assert doc["skipped"].get("note_decay:coverage") == 1
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "memories_v3" in out.getvalue()


def test_two_notes_of_one_meaning_are_one_question(world):
    world.note("roastery", "Обжарка зёрен по средам у Петровича", "Обжарщик Петрович берёт заказы на обжарку до обеда.")
    world.note("roasting", "Обжарка зёрен у Петровича по средам", "Петрович берёт заказы на обжарку до обеда, обжарщик.")
    world.say({1: ["касса сошлась"]})
    dups = ask(world, "note_duplicate")
    assert len(dups) == 1
    q = dups[0]
    assert {q["note"]["name"], q["other"]["name"]} == {"roastery", "roasting"}
    assert questions.answers(q).keys() == {"drop-note", "keep-both"}
    code, out = lines_of(review.decide, world.homes, q["qid"], "drop-note")
    edit = json.loads(out[-1])["edit"]
    assert edit["action"] == "delete" and edit["path"] == q["note"]["path"] and len(edit["sha256"]) == 64
    review.mark(world.homes, q["qid"], "undo", out=lambda *_: None)
    review.decide(world.homes, q["qid"], "keep-both", out=lambda *_: None)
    assert not ask(world, "note_duplicate")  # answered: not asked again while both notes say what they said


def test_conflict_with_a_claude_note_offers_answers_on_the_note_side(world):
    fid = world.fact(OPENS, origin="said", days_old=5)
    world.note("hours", "Часы работы магазина", NOTE_OPENS)
    world.say({1: ["касса сошлась"]})
    q = next(q for q in ask(world, "conflict") if q["fact_id"] == fid)
    assert {"changed", "outdated", "wrong", "keep", "note-changed", "note-outdated"} <= set(questions.answers(q))
    code, out = lines_of(review.decide, world.homes, q["qid"], "note-changed", text="Кофейня открывается в 9 утра")
    todo = json.loads(out[-1])
    assert todo["edit"]["action"] == "replace-body" and todo["edit"]["text"] == "Кофейня открывается в 9 утра"
    assert todo["edit"]["path"].endswith("hours.md") and "call" not in todo
    assert todo["check"] == {"verify": f"dreamem verify {q['qid']}"}


def test_codex_memory_in_a_conflict_is_never_edited(world):
    fid = world.fact(OPENS, origin="said", days_old=5)
    folder = world.homes.codex / "memories"
    folder.mkdir(parents=True)
    (folder / "MEMORY.md").write_text(f"# Memory\n\n- {NOTE_OPENS}\n", encoding="utf-8")
    world.say({1: ["касса сошлась"]})
    q = next(q for q in ask(world, "conflict") if q["fact_id"] == fid)
    assert not [a for a in questions.answers(q) if a.startswith("note-")]


def test_note_decay_outdated_deletes_and_changed_replaces(world):
    path = world.note("deploy", "Деплой сайта магазина через rsync", "rsync -av dist/ shop:/var/www")
    world.say({1: ["касса сошлась"]})
    q = ask(world, "note_decay")[0]
    assert set(questions.answers(q)) == {"keep", "outdated", "changed"}
    code, out = lines_of(review.decide, world.homes, q["qid"], "outdated")
    edit = json.loads(out[-1])["edit"]
    assert edit == {"action": "delete", "path": str(path), "sha256": q["note"]["sha256"],
                    "index": str(path.parent / "MEMORY.md"), "index_entry": "deploy.md"}
    review.mark(world.homes, q["qid"], "undo", out=lambda *_: None)
    code, out = lines_of(review.decide, world.homes, q["qid"], "changed", text="Деплой через GitHub Actions")
    assert json.loads(out[-1])["edit"]["action"] == "replace-body"


def test_verify_refuses_a_note_changed_since_it_was_shown(world):
    path = world.note("deploy", "Деплой сайта магазина через rsync", "rsync -av dist/ shop:/var/www")
    world.say({1: ["касса сошлась"]})
    q = ask(world, "note_decay")[0]
    review.decide(world.homes, q["qid"], "outdated", out=lambda *_: None)
    assert lines_of(review.verify, world.homes, q["qid"]) == (0, [f"{q['qid']}: ok — the note is as it was shown"])
    path.write_text(path.read_text(encoding="utf-8") + "\nДобавлено Claude сегодня.\n", encoding="utf-8")
    code, out = lines_of(review.verify, world.homes, q["qid"])
    assert code == 1 and "changed" in out[0]
    assert world.questions()["questions"][0]["status"] == questions.FAILED


def test_the_review_commands_never_write_claudes_files(world):
    world.note("deploy", "Деплой сайта магазина через rsync", "rsync -av dist/ shop:/var/www")
    world.say({1: ["касса сошлась"]})
    q = ask(world, "note_decay")[0]
    before = world.tree()
    review.show(world.homes, out=lambda *_: None)
    review.decide(world.homes, q["qid"], "outdated", out=lambda *_: None)
    review.verify(world.homes, q["qid"], out=lambda *_: None)
    review.mark(world.homes, q["qid"], "applied", out=lambda *_: None)
    assert world.tree() == before  # the agent edits the note; the commands only record


def test_the_skill_tells_how_to_edit_a_note():
    for path in (ROOT / "dreamem" / "skill" / "SKILL.md", ROOT / "claude-plugin" / "skills" / "dream" / "SKILL.md"):
        text = path.read_text(encoding="utf-8")
        assert "dreamem verify" in text and "Never edit Claude Code memory notes" not in text
