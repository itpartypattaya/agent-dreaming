"""Antigravity and Gemini CLI sessions under ~/.gemini: only the person's own words count (the shapes seen on agy 1.4.3
and Gemini CLI 0.64, synthetic content)."""
import io
import json
import uuid

from world import days_ago, iso

from dreamem import run
from dreamem.sources import gemini

ASKED = "Кофейня на Садовой теперь открывается в восемь утра"


def antigravity(world, steps, conversation=None):
    conv = conversation or str(uuid.uuid4())
    path = world.homes.gemini / "antigravity-cli" / "brain" / conv / ".system_generated" / "logs" / "transcript.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(s, ensure_ascii=False).encode("utf-8") + b"\n" for s in steps))
    return path


def step(i, source, kind, content, *, ts=None, **extra):
    return {"step_index": i, "source": source, "type": kind, "status": "DONE",
            "created_at": iso(ts or days_ago(1)).replace(".000", ""), "content": content, **extra}


def user_input(i, words, ts=None):
    return step(i, "USER_EXPLICIT", "USER_INPUT",
                f"<USER_REQUEST>\n{words}\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\nThe current local time is 10:00. "
                "Active document: C:/work/shop/notes.md\n</ADDITIONAL_METADATA>", ts=ts)


def test_antigravity_counts_only_the_users_request(world):
    antigravity(world, [
        user_input(0, ASKED),
        step(1, "MODEL", "PLANNER_RESPONSE", "Хорошо, кофейня открывается в восемь утра.", tool_calls=[]),
        step(2, "SYSTEM", "SYSTEM_MESSAGE", "<USER_SETTINGS_CHANGE>mode changed</USER_SETTINGS_CHANGE>"),
        step(3, "USER_EXPLICIT", "USER_INPUT", "<USER_SETTINGS_CHANGE>model switched</USER_SETTINGS_CHANGE>"),
    ])
    msgs, cov = gemini.read_antigravity(world.homes.gemini, days_ago(30))
    assert [m.text for m in msgs] == [ASKED]
    assert cov.status == "complete" and msgs[0].client == "antigravity"


def test_antigravity_step_of_an_unknown_source_is_never_the_person(world):
    antigravity(world, [step(i, "USER_IMPLICIT", "USER_INPUT", f"<USER_REQUEST>{ASKED} {i}</USER_REQUEST>")
                        for i in range(5)])
    msgs, cov = gemini.read_antigravity(world.homes.gemini, days_ago(30))
    assert msgs == [] and cov.status == "partial"  # too much of it unknown: the night must not read silence


def gemini_chat(world, lines, project="shop"):
    path = world.homes.gemini / "tmp" / project / "chats" / f"session-2026-10-30T10-00-{uuid.uuid4().hex[:8]}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(json.dumps(x, ensure_ascii=False).encode("utf-8") + b"\n" for x in lines))
    return path


def test_gemini_cli_reads_user_text_once_and_skips_tool_results(world):
    sid = str(uuid.uuid4())
    said = {"id": str(uuid.uuid4()), "timestamp": iso(days_ago(1)), "type": "user", "content": [{"text": ASKED}]}
    tool = {"id": uuid.uuid4().hex, "timestamp": iso(days_ago(1)), "type": "user",
            "content": [{"text": "Касса закрыта до понедельника"},
                        {"functionResponse": {"name": "read_file", "response": {"output": "Касса закрыта"}}}]}
    answer = {"id": str(uuid.uuid4()), "timestamp": iso(days_ago(1)), "type": "gemini", "content": "Записал.",
              "model": "gemini-3-pro"}
    gemini_chat(world, [{"sessionId": sid, "projectHash": "a" * 64, "startTime": iso(days_ago(1)),
                         "lastUpdated": iso(days_ago(1)), "kind": "main"},
                        said, answer, {"$set": {"messages": [said, tool], "lastUpdated": iso(days_ago(1))}}])
    msgs, cov = gemini.read_gemini(world.homes.gemini, days_ago(30))
    assert [(m.text, m.session_id) for m in msgs] == [(ASKED, sid)]
    assert cov.status == "complete"


def test_the_night_reads_both_and_gemini_md_is_a_dedupe_target(world):
    antigravity(world, [user_input(0, ASKED)])
    world.homes.gemini.mkdir(parents=True, exist_ok=True)
    (world.homes.gemini / "GEMINI.md").write_text("## Gemini Added Memories\n- Кассир Марина работает по субботам\n",
                                                  encoding="utf-8")
    assert run.run(world.homes, out=io.StringIO()) == 0
    doc = world.questions()
    assert doc["coverage"]["antigravity"]["messages"] == 1
    assert doc["coverage"]["gemini-cli"]["reasons"] == {"not_used": 1}
    assert ASKED.encode() not in world.our_bytes()  # read for the night, kept nowhere
