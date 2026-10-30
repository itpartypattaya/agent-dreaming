"""Claude Code transcripts: only a person's words reach the pass, and what could not be read is said so.

Requirement: a line corroborates memory only when a person typed it. The Desktop app marks a person's turn as
promptSource=sdk + turnOrigin=human; background tasks, scheduled jobs, other sessions, subagents, slash commands,
reminders and quotes of the agent are not the person. A format we do not recognise is left out and reported."""
import json
import os
import time

from world import days_ago

from dreamem.sources import COMPLETE, PARTIAL, claude_code


def read(world, days=30):
    return claude_code.read(world.homes.claude_projects, time.time() - days * 86400)


def texts(msgs):
    return sorted(m.text for m in msgs)


def test_only_human_turns_are_read(world):
    w = world
    w.claude_file([
        w.claude_line("человек пишет про витрину"),
        w.claude_line("<task-notification><task-id>1</task-id></task-notification>", origin="task_notification",
                      source="system"),
        w.claude_line("фоновая задача сообщает итог", origin="task_notification", source="system"),
        w.claude_line("другая сессия прислала отчёт", origin="sdk", source="sdk"),
        w.claude_line("расписание запустило проверку", origin="scheduled", source="system"),
        w.claude_line("субагент получил бриф", isSidechain=True),
        w.claude_line("служебная мета-строка", isMeta=True),
        w.claude_line("This session is being continued from a previous conversation", isCompactSummary=True),
        w.claude_line([{"type": "tool_result", "tool_use_id": "t1", "content": "вывод команды"}]),
    ])
    msgs, cov = read(w)
    assert texts(msgs) == ["человек пишет про витрину"]
    assert cov.status == COMPLETE


def test_inserts_and_quotes_are_cut_from_a_human_turn(world):
    w = world
    w.claude_file([
        w.claude_line("<system-reminder>The task tools haven't been used</system-reminder>\nпоменяй ценники"),
        w.claude_line("<!-- reply 1 -->\n> агент написал: склад закрыт\nда, склад закрыт до понедельника"),
        w.claude_line('<pasted_content id="x">чужой документ про аренду</pasted_content>\nсделай выжимку'),
        w.claude_line("<command-name>/compact</command-name>", origin=None, source=None),
        w.claude_line("[Request interrupted by user]", origin=None, source=None),
    ])
    msgs, _ = read(w)
    assert texts(msgs) == ["да, склад закрыт до понедельника", "поменяй ценники", "сделай выжимку"]


def test_line_without_turn_origin_is_unknown_on_versions_that_write_it(world):
    w = world
    w.claude_file([w.claude_line("размеченная реплика", version="2.1.309"),
                   w.claude_line("неизвестно чья реплика", origin=None, version="2.1.309")])
    msgs, cov = read(w)
    assert texts(msgs) == ["размеченная реплика"]
    assert cov.kinds["unknown"] == 1


def test_versions_older_than_turn_origin_count_their_prompts_as_human(world):
    w = world
    w.claude_file([w.claude_line("старый Desktop без поля", origin=None, version="2.1.210"),
                   w.claude_line("новый Desktop", version="2.1.309")])
    msgs, cov = read(w)
    assert texts(msgs) == ["новый Desktop", "старый Desktop без поля"]
    assert cov.kinds.get("unknown", 0) == 0


def test_resumed_session_repeats_lines_once(world):
    w = world
    line = w.claude_line("одна и та же реплика", uid="same-uuid")
    w.claude_file([line], session="a")
    w.claude_file([line, w.claude_line("новая в продолжении")], session="b")
    msgs, cov = read(w)
    assert texts(msgs) == ["новая в продолжении", "одна и та же реплика"]
    assert cov.reasons["duplicate_line"] == 1


def test_cut_last_line_is_forgiven_a_broken_middle_line_is_not(world):
    w = world
    w.claude_file([w.claude_line("до обрыва")], session="growing", raw_tail=b'{"type":"user","message":{"ro')
    msgs, cov = read(w)
    assert texts(msgs) == ["до обрыва"]
    assert cov.status == COMPLETE
    w.claude_file([w.claude_line("до мусора"), b'{"type":"user", broken json\n', w.claude_line("после мусора")],
                  session="broken")
    msgs, cov = read(w)
    assert "после мусора" in texts(msgs)
    assert cov.status == PARTIAL and "bad_lines" in cov.reasons


def test_empty_file_bom_and_cp1251_line(world):
    w = world
    folder = w.homes.claude / "projects" / "C--work-shop"
    folder.mkdir(parents=True)
    (folder / "empty.jsonl").write_bytes(b"")
    good = json.dumps(w.claude_line("строка с BOM"), ensure_ascii=False).encode("utf-8")
    legacy = json.dumps(w.claude_line("строка в cp1251"), ensure_ascii=False).encode("cp1251")
    w.claude_file([b"\xef\xbb\xbf" + good + b"\n", legacy + b"\n"], session="enc")
    msgs, cov = read(w)
    assert texts(msgs) == ["строка в cp1251", "строка с BOM"]
    assert cov.status == COMPLETE


def test_many_unknown_lines_make_the_coverage_partial(world):
    w = world
    lines = [w.claude_line(f"неразмеченная {i}", origin=None, version="2.1.309") for i in range(3)]
    lines += [w.claude_line(f"человек {i}") for i in range(7)]
    w.claude_file(lines)
    _, cov = read(w)
    assert cov.status == PARTIAL and "unknown_share" in cov.reasons


def test_old_files_and_old_lines_stay_out_of_the_window(world):
    w = world
    path = w.claude_file([w.claude_line("сорок дней назад", ts=days_ago(40)), w.claude_line("вчера")])
    msgs, _ = read(w)
    assert texts(msgs) == ["вчера"]
    os.utime(path, (days_ago(40), days_ago(40)))
    msgs, cov = read(w)
    assert msgs == [] and cov.files == 0
