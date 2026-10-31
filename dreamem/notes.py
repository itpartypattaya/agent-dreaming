"""The CLIs' own memory, read-only: Claude Code auto-memory notes, Codex memories, CLAUDE.md / AGENTS.md.

- Auto-memory notes (``projects/<project>/memory/*.md`` with frontmatter, ``MEMORY.md`` the index) are the "still
  relevant?" material: each topic file becomes one note the engine reads, with a stable id (sha1 of its path) to
  map its answer back to the file. Claude rewrites these files itself; the pass never does.
- Codex memories (``memories*/``) and the instruction files are dedupe targets only: "this fact is already written
  there" — never questions, never edited.
- The index ``MEMORY.md`` of each project is measured as Claude Code loads it: the first 200 lines / 25 KB.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

INDEX_LINES, INDEX_BYTES = 200, 25 * 1024
AUTO, CODEX, INSTRUCTIONS = "auto-memory", "codex-memory", "instructions"
_FRONT_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_KEY_RE = re.compile(r"^(\w[\w-]*)\s*:\s*(.*)$")


@dataclass
class Note:
    id: str
    kind: str
    project: str
    path: str
    name: str
    text: str           # what the pass reads: description and body, no frontmatter
    sha256: str = ""    # of the file as the pass read it: an edit is made only to this very file (dreamem verify)


def _read(path: Path) -> str | None:
    got = _read_both(path)
    return got[0] if got else None


def _read_both(path: Path) -> tuple[str, str] | None:
    """(text, sha256 of the bytes it was decoded from): one read, so the digest is of the very text shown."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(raw).hexdigest()
    return _decode(raw), digest


def _decode(raw: bytes) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")


def frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        km = _KEY_RE.match(line.strip())
        if km and not line.startswith((" ", "\t")):
            meta[km.group(1)] = km.group(2).strip().strip("'\"")
    return meta, text[m.end():]


def note_id(path: Path) -> str:
    return hashlib.sha1(str(path).replace("\\", "/").lower().encode("utf-8")).hexdigest()[:12]


def _flat(text: str) -> str:
    """One §-entry: the § delimiter of the engine's format must not appear inside a note."""
    return text.replace("\n§\n", "\n").replace("§", "").strip()


def file_sha256(path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def auto_memory(dirs: list[Path]) -> tuple[list[Note], list[dict], list[str]]:
    """(notes, index pressure per project, problems). A subagent's memory folder is named ``agent:<name>``."""
    notes, pressure, problems = [], [], []
    for folder in dirs:
        project = folder.parent.name if folder.name == "memory" else folder.name
        if folder.parent.name in ("agent-memory", "agent-memory-local"):
            project = f"agent:{folder.name}"
        for path in sorted(folder.glob("*.md")):
            got = _read_both(path)
            if got is None:
                problems.append(f"unreadable:{project}")
                continue
            text, digest = got
            if path.name == "MEMORY.md":
                lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
                size = len(text.encode("utf-8"))
                pressure.append({"project": project, "lines": lines, "bytes": size,
                                 "lines_pct": round(100 * lines / INDEX_LINES), "bytes_pct": round(100 * size / INDEX_BYTES)})
                continue
            meta, body = frontmatter(text)
            name = meta.get("name") or path.stem
            parts = [p for p in (meta.get("description", ""), body.strip()) if p]
            entry = _flat("\n".join(parts))
            if len(entry) <= 10:
                continue
            notes.append(Note(note_id(path), AUTO, project, str(path), name, entry, digest))
    return notes, pressure, problems


def codex_memory(dirs: list[Path]) -> tuple[list[Note], list[str]]:
    """(notes, problems). A folder holding Markdown but none of the files we know is a format we do not read yet:
    ``unknown-format:<folder>`` — the coverage is partial, not silently empty."""
    notes, problems = [], []
    for folder in dirs:
        before = len(notes)
        for name in ("MEMORY.md", "memory_summary.md"):
            for path in sorted(folder.rglob(name)):
                if ".git" in path.parts:
                    continue
                text = _read(path)
                if text is None:
                    problems.append(f"unreadable:{folder.name}")
                    continue
                if text.strip():
                    notes.append(Note(note_id(path), CODEX, folder.name, str(path), path.name, _flat(text)))
        if len(notes) == before and any(".git" not in p.parts and p.stat().st_size for p in folder.rglob("*.md")):
            problems.append(f"unknown-format:{folder.name}")
    return notes, problems


def note_pairs(notes: list[Note], facts: list[tuple[int, str]]) -> list[dict]:
    """Notes that say what another note or a fact says (the same significant words, nearly all of them). Each
    ``{note, other_note | fact}``; the note proposed to go is the shorter of two notes, or the note beside a fact."""
    from .engine import dream

    def stems(text):
        return dream._stems(dream.sig_tokens(text))

    def same(a, b):
        shared = a & b
        return len(shared) >= PAIR_MIN_SHARED and len(shared) / len(a | b) >= PAIR_MIN_JACCARD

    noted = [(n, stems(n.text)) for n in notes]
    out, taken = [], set()
    for i, (a, sa) in enumerate(noted):
        for b, sb in noted[i + 1:]:
            if a.id in taken or b.id in taken or not same(sa, sb):
                continue
            drop, keep = (a, b) if len(a.text) <= len(b.text) else (b, a)
            out.append({"note": drop, "other_note": keep})
            taken |= {a.id, b.id}
    for fid, text in facts:
        sf = stems(text)
        for n, sn in noted:
            if n.id not in taken and same(sn, sf):
                out.append({"note": n, "fact": {"fact_id": fid, "content": text}})
                taken.add(n.id)
                break
    return out


PAIR_MIN_SHARED, PAIR_MIN_JACCARD = 4, 0.75


def instructions(paths: list[Path]) -> list[Note]:
    notes = []
    for path in paths:
        text = _read(path) if path.is_file() else None
        if text and text.strip():
            notes.append(Note(note_id(path), INSTRUCTIONS, path.parent.name, str(path), path.name, _flat(text)))
    return notes


def instruction_files(claude: Path, codex: Path, project_roots: list[Path], gemini: Path | None = None) -> list[Path]:
    """The CLIs' instruction files; ``~/.gemini/GEMINI.md`` is also where Gemini CLI's ``save_memory`` writes."""
    out = [claude / "CLAUDE.md", codex / "AGENTS.override.md", codex / "AGENTS.md"]
    if gemini is not None:
        out.append(gemini / "GEMINI.md")
    for root in project_roots:
        out += [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md", root / "AGENTS.md", root / "GEMINI.md"]
    seen, unique = set(), []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique
