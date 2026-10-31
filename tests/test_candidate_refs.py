"""References to a person's message that keep none of its words.

Requirements: a candidate names a message by session file and keyed digests only — no word of it, no prefix, no
sentence, in any encoding; read again later it is the same message, from Claude Code or Codex, also after Codex
compressed the session or moved it to the archive; a line rewritten since, a file removed, a digest of another
install's salt — not found, nothing shown.
"""
import json
import shutil
import time

import zstandard
from world import days_ago

from dreamem import candidates
from dreamem.sources import claude_code, codex

TEXT = "У меня дочка Ева, ей семь лет, и у неё аллергия на арахис"


def claude_message(world):
    world.claude_file([world.claude_line(TEXT, ts=days_ago(2), uid="u-eva")], session="talk")
    msgs, _ = claude_code.read(world.homes.claude_projects, time.time() - 30 * 86400)
    return next(m for m in msgs if m.text == TEXT)


def codex_message(world, **kw):
    paths = world.rollout([(days_ago(2), "user", TEXT)], **kw)
    msgs, _ = codex.read(world.homes.codex_session_dirs(), time.time() - 30 * 86400)
    return next(m for m in msgs if m.text == TEXT), paths


def test_a_reference_holds_no_word(world):
    key = candidates.salt(world.homes)
    for m in (claude_message(world), codex_message(world)[0]):
        ref = candidates.reference(key, m)
        raw = json.dumps(ref, ensure_ascii=False) + json.dumps(ref)
        for piece in (TEXT, TEXT[:20], "дочка Ева", "арахис", "Ева", TEXT.lower()):
            assert piece not in raw and piece.encode("unicode_escape").decode() not in raw
        assert set(ref) == {"client", "source", "ts", "id", "text"}


def test_read_again_it_is_the_same_message(world):
    key = candidates.salt(world.homes)
    for m in (claude_message(world), codex_message(world)[0]):
        again = candidates.resolve(world.homes, key, candidates.reference(key, m))
        assert again is not None and again.text == TEXT and again.client == m.client


def test_a_line_rewritten_since_is_gone(world):
    key = candidates.salt(world.homes)
    ref = candidates.reference(key, claude_message(world))
    path = world.homes.claude_projects / ref["source"]
    path.write_bytes(path.read_bytes().replace("семь".encode(), "восемь".encode()))  # same uuid, other words
    assert candidates.resolve(world.homes, key, ref) is None


def test_a_codex_session_compressed_and_archived_is_still_found(world):
    key = candidates.salt(world.homes)
    m, (plain,) = codex_message(world)
    ref = candidates.reference(key, m)
    archived = world.homes.codex / "archived_sessions"
    archived.mkdir()
    (archived / (plain.name + ".zst")).write_bytes(zstandard.ZstdCompressor().compress(plain.read_bytes()))
    plain.unlink()
    again = candidates.resolve(world.homes, key, ref)
    assert again is not None and again.text == TEXT


def test_a_removed_file_is_gone(world):
    key = candidates.salt(world.homes)
    ref = candidates.reference(key, claude_message(world))
    shutil.rmtree(world.homes.claude_projects)
    world.homes.claude_projects.mkdir()
    assert candidates.resolve(world.homes, key, ref) is None


def test_the_salt_is_made_once_and_is_this_installs_own(world, tmp_path):
    first = candidates.salt(world.homes)
    assert len(first) == 32 and candidates.salt(world.homes) == first
    ref = candidates.reference(first, claude_message(world))
    other = candidates.salt(type(world.homes)(dreaming=tmp_path / "other", claude=world.homes.claude,
                                              codex=world.homes.codex, holomem=world.homes.holomem))
    assert other != first and candidates.resolve(world.homes, other, ref) is None


def test_an_old_claude_line_found_by_a_newer_session_is_found_again(world):
    """Codex review of 0.5.0: a line of a version before turnOrigin counts as a person's only when a newer session
    shows the boundary; read alone again, its file had no boundary, and the candidate was lost."""
    key = candidates.salt(world.homes)
    world.claude_file([world.claude_line(TEXT, ts=days_ago(3), origin=None, version="2.0.10", uid="u-old")],
                      session="old")
    world.claude_file([world.claude_line("свежая реплика", ts=days_ago(1), version="2.1.309")], session="new")
    msgs, _ = claude_code.read(world.homes.claude_projects, time.time() - 30 * 86400)
    m = next(m for m in msgs if m.text == TEXT)
    again = candidates.resolve(world.homes, key, candidates.reference(key, m))
    assert again is not None and again.text == TEXT


def test_a_message_dated_after_its_files_mtime_is_found_again(world):
    """Codex review of 0.5.0: the file's mtime a little before the message's own time (clock skew) hid it."""
    import os

    key = candidates.salt(world.homes)
    m = claude_message(world)
    path = world.homes.claude_projects / m.source
    os.utime(path, (m.ts - 10, m.ts - 10))
    assert candidates.resolve(world.homes, key, candidates.reference(key, m)) is not None
