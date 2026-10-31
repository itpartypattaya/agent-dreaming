"""The cache of positions for Claude Code transcripts.

Requirements: a pass with the cache reads exactly what a pass without it reads — the same messages, the same coverage —
whatever happened to the files between passes (grew, a line half written then finished, rewritten, gone, the cache
broken); a file nobody touched is not read again from the start; the cache holds no word of what a person said.
"""
import json
import time

import pytest

from world import days_ago

from dreamem.sources import claude_code

SECRET = "склад закрыт до понедельника"


def both(world, cache):
    since = time.time() - 30 * 86400
    plain = claude_code.read(world.homes.claude_projects, since)
    cached = claude_code.read(world.homes.claude_projects, since, cache=cache)
    return plain, cached


def same(plain, cached):
    (pm, pc), (cm, cc) = plain, cached
    assert [(m.session_id, m.ts, m.text, m.key) for m in cm] == [(m.session_id, m.ts, m.text, m.key) for m in pm]
    assert cc.as_dict() == pc.as_dict()


def scans(monkeypatch):
    starts = []
    real = claude_code._scan

    def spy(handle, start, cov, lines):
        starts.append((handle.name, start))
        return real(handle, start, cov, lines)

    monkeypatch.setattr(claude_code, "_scan", spy)
    return starts


def test_an_untouched_file_is_read_at_its_positions_only(world, tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    path = world.claude_file([world.claude_line(SECRET), world.claude_line("поменяй ценники")])
    same(*both(world, cache))
    starts = scans(monkeypatch)
    same(*both(world, cache))
    size = path.stat().st_size
    assert (str(path), 0) in starts  # the pass without the cache
    assert (str(path), size) in starts and starts.count((str(path), 0)) == 1  # with it: from the end only


def test_a_grown_file_is_read_from_where_the_last_pass_stopped(world, tmp_path, monkeypatch):
    cache = tmp_path / "cache.json"
    path = world.claude_file([world.claude_line("первая реплика", ts=days_ago(2))])
    both(world, cache)
    old = path.stat().st_size
    with path.open("ab") as f:
        f.write(json.dumps(world.claude_line("вторая реплика", ts=days_ago(1)), ensure_ascii=False).encode() + b"\n")
    starts = scans(monkeypatch)
    plain, cached = both(world, cache)
    same(plain, cached)
    assert sorted(m.text for m in cached[0]) == ["вторая реплика", "первая реплика"]
    assert (str(path), old) in starts


@pytest.mark.parametrize("cut", [40, -30])  # before the line says "user", and after
def test_a_line_being_written_is_read_again_when_finished(world, tmp_path, cut):
    cache = tmp_path / "cache.json"
    whole = json.dumps(world.claude_line("реплика дописана"), ensure_ascii=False).encode() + b"\n"
    path = world.claude_file([world.claude_line("первая реплика")], raw_tail=whole[:cut])
    plain, cached = both(world, cache)
    same(plain, cached)
    with path.open("ab") as f:
        f.write(whole[cut:])
    plain, cached = both(world, cache)
    same(plain, cached)
    assert sorted(m.text for m in cached[0]) == ["первая реплика", "реплика дописана"]


def test_a_rewritten_file_is_read_whole(world, tmp_path):
    cache = tmp_path / "cache.json"
    world.claude_file([world.claude_line("старая реплика номер один"), world.claude_line("старая реплика номер два")])
    both(world, cache)
    # Same file name, other content of the same length and longer: positions must not be trusted.
    world.claude_file([world.claude_line("новая реплика номер один"), world.claude_line("новая реплика номер два"),
                       world.claude_line("и третья")])
    plain, cached = both(world, cache)
    same(plain, cached)
    assert "старая реплика номер один" not in [m.text for m in cached[0]]
    world.claude_file([world.claude_line("короче")])
    same(*both(world, cache))


def test_a_line_put_in_before_the_end_is_not_missed(world, tmp_path):
    """A rewrite that keeps every remembered position a person's line of the same length — only the bytes where the
    last pass stopped tell it apart."""
    cache = tmp_path / "cache.json"
    a, b, c = (world.claude_line(t, uid=f"uid-{i}") for i, t in enumerate(("реплика аз", "реплика бэ", "реплика цэ")))
    world.claude_file([a, b])
    both(world, cache)
    world.claude_file([a, c, b])
    plain, cached = both(world, cache)
    same(plain, cached)
    assert sorted(m.text for m in cached[0]) == ["реплика аз", "реплика бэ", "реплика цэ"]


def test_settled_lines_count_and_date_the_old_ones_from_the_cache(world, tmp_path, monkeypatch):
    """Lines that are nobody's words for good (a task notice, a subagent, a combination we do not know) are remembered
    as counts, not read again — and the newest version they show writing turnOrigin still dates an old line elsewhere:
    a prompt of a version before the field was a person's."""
    cache = tmp_path / "cache.json"
    world.claude_file([world.claude_line("фоновая задача сообщает итог", origin="task_notification", source="system",
                                         version="2.1.309"),
                       world.claude_line("субагент получил бриф", isSidechain=True),
                       world.claude_line("неизвестно чья реплика", origin=None, source="other")], session="bg")
    world.claude_file([world.claude_line("реплика старой версии", origin=None, version="2.0.10")], session="old")
    plain, cached = both(world, cache)
    same(plain, cached)
    assert [m.text for m in cached[0]] == ["реплика старой версии"]
    entry = json.loads(cache.read_text(encoding="utf-8"))["files"]["C--work-shop/bg.jsonl"]
    assert entry["lines"] == [] and entry["machine"] == 2 and entry["unknown"] == 1
    plain, cached = both(world, cache)
    same(plain, cached)
    assert [m.text for m in cached[0]] == ["реплика старой версии"]


def test_a_file_that_left_the_window_leaves_the_cache(world, tmp_path):
    import os

    cache = tmp_path / "cache.json"
    old = world.claude_file([world.claude_line("давняя реплика", ts=days_ago(40))], session="old")
    world.claude_file([world.claude_line("свежая реплика")], session="new")
    both(world, cache)
    assert len(json.loads(cache.read_text(encoding="utf-8"))["files"]) == 2
    os.utime(old, (days_ago(40), days_ago(40)))
    same(*both(world, cache))
    assert list(json.loads(cache.read_text(encoding="utf-8"))["files"]) == ["C--work-shop/new.jsonl"]


def test_a_broken_cache_is_read_past(world, tmp_path):
    cache = tmp_path / "cache.json"
    world.claude_file([world.claude_line(SECRET)])
    for junk in (b"{ not json", b'{"v": 1, "files": {"C--work-shop/s1.jsonl": {"scanned": "x"}}}',
                 b'{"v": 1, "files": {"C--work-shop/s1.jsonl": {"scanned": 0, "head": 1, "lines": [[0, 9999]]}}}'):
        cache.write_bytes(junk)
        same(*both(world, cache))


def test_the_cache_holds_no_words(world, tmp_path):
    cache = tmp_path / "cache.json"
    world.claude_file([world.claude_line(SECRET)])
    msgs, _ = claude_code.read(world.homes.claude_projects, time.time() - 30 * 86400, cache=cache)
    assert [m.text for m in msgs] == [SECRET]
    raw = cache.read_bytes()
    assert SECRET.encode() not in raw and "склад".encode() not in raw and "склад".encode("unicode_escape") not in raw


# `dreamem run --dry-run` promises to keep nothing: a preview leaves ~/.dreaming exactly as it found it — no cache on a
# fresh home, and an existing cache not rewritten even when the transcripts grew since.
def test_a_dry_run_keeps_no_cache(world):
    import io

    from dreamem import run

    world.say({2: ["Поставщик привозит зерно по вторникам"]}, session="t1")
    assert run.run(world.homes, dry_run=True, out=io.StringIO()) == 0
    assert not (world.homes.dreaming / run.CLAUDE_CACHE).exists()


def test_a_dry_run_leaves_the_cache_of_the_last_night(world):
    import io

    from dreamem import run

    world.say({2: ["Поставщик привозит зерно по вторникам"]}, session="t1")
    assert run.run(world.homes, out=io.StringIO()) == 0
    cache = world.homes.dreaming / run.CLAUDE_CACHE
    assert cache.exists()
    world.say({1: ["Склад открывается в восемь утра"]}, session="t2")
    # The lock is not the night's: every command takes it (a dry run too, so it never races a real pass) and stamps
    # its pid and start time in it, for a person reading the file.
    lock = str(world.homes.lock.relative_to(world.root))
    before = world.tree(world.homes.dreaming)
    before.pop(lock)
    assert run.run(world.homes, dry_run=True, out=io.StringIO()) == 0
    after = world.tree(world.homes.dreaming)
    after.pop(lock)
    assert after == before
