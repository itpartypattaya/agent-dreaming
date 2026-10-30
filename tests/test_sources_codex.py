"""Codex rollouts: a person's session (cli, vscode) is read, a script's (exec) or another agent's is not; a rollout
is read once whether it is plain, compressed or both; what could not be read makes the coverage partial."""
import time

from world import days_ago

from dreamem.paths import homes
from dreamem.sources import COMPLETE, PARTIAL, Coverage, codex


def read(world):
    return codex.read(world.homes.codex_session_dirs(), time.time() - 30 * 86400)


def texts(msgs):
    return sorted(m.text for m in msgs)


def test_person_sessions_only(world):
    w = world
    w.rollout([(days_ago(1), "user", "из терминала")], source="cli")
    w.rollout([(days_ago(1), "user", "из редактора")], source="vscode")
    w.rollout([(days_ago(1), "user", "скрипт codex exec")], source="exec")
    w.rollout([(days_ago(1), "user", "бриф субагента")], source={"subagent": {"thread_spawn": {}}})
    w.rollout([(days_ago(1), "user", "неизвестный источник")], source="something-new")
    msgs, cov = read(w)
    assert texts(msgs) == ["из редактора", "из терминала"]
    assert cov.kinds == {"human": 2, "machine": 2, "unknown": 1}


def test_developer_and_inserted_blocks_are_not_speech(world):
    w = world
    w.rollout([(days_ago(1), "developer", "инструкции харнесса"),
               (days_ago(1), "user", "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"),
               (days_ago(1), "user", "# AGENTS.md instructions for /x\n\nправила проекта"),
               (days_ago(1), "assistant", "ответ агента"),
               (days_ago(1), "user", "настоящий вопрос человека")])
    msgs, _ = read(w)
    assert texts(msgs) == ["настоящий вопрос человека"]


def test_plain_and_zst_of_one_session_are_read_once(world):
    w = world
    w.rollout([(days_ago(1), "user", "одна сессия в двух видах")], zst=True, plain=True)
    w.rollout([(days_ago(9), "user", "только сжатая")], zst=True, plain=False)
    msgs, cov = read(w)
    assert texts(msgs) == ["одна сессия в двух видах", "только сжатая"]
    assert cov.status == COMPLETE and cov.files == 2


def test_compressed_between_listing_and_reading(world):
    plain, _packed = world.rollout([(days_ago(1), "user", "сжали на лету")], zst=True)
    plain.unlink()
    cov = Coverage("codex")
    with codex._open(plain, None, cov) as stream:  # listed as plain, gone when opened: read as .zst
        data = stream.read()
    assert "сжали на лету".encode() in data and cov.status == COMPLETE


def test_without_zstandard_a_compressed_session_makes_coverage_partial(world, monkeypatch):
    world.rollout([(days_ago(1), "user", "сжатая")], zst=True, plain=False)
    monkeypatch.setattr(codex, "_zstd", None)
    msgs, cov = read(world)
    assert msgs == [] and cov.status == PARTIAL and "zstd_missing" in cov.reasons


def test_broken_zst_makes_coverage_partial(world):
    p = world.rollout([(days_ago(1), "user", "x")], zst=True, plain=False)[0]
    p.write_bytes(b"(\xb5/\xfd not really zstd")
    _msgs, cov = read(world)
    assert cov.status == PARTIAL and "bad_zstd" in cov.reasons


def test_no_codex_at_all_is_not_a_gap(tmp_path):
    h = homes({"CODEX_HOME": str(tmp_path / "none")})
    msgs, cov = codex.read(h.codex_session_dirs(), 0)
    assert msgs == [] and cov.status == COMPLETE
