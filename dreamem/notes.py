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


def _read(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
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


def auto_memory(dirs: list[Path]) -> tuple[list[Note], list[dict], list[str]]:
    """(notes, index pressure per project, problems)."""
    notes, pressure, problems = [], [], []
    for folder in dirs:
        project = folder.parent.name if folder.name == "memory" else folder.name
        for path in sorted(folder.glob("*.md")):
            text = _read(path)
            if text is None:
                problems.append(f"unreadable:{project}")
                continue
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
            notes.append(Note(note_id(path), AUTO, project, str(path), name, entry))
    return notes, pressure, problems


def codex_memory(dirs: list[Path]) -> tuple[list[Note], list[str]]:
    notes, problems = [], []
    for folder in dirs:
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
    return notes, problems


def instructions(paths: list[Path]) -> list[Note]:
    notes = []
    for path in paths:
        text = _read(path) if path.is_file() else None
        if text and text.strip():
            notes.append(Note(note_id(path), INSTRUCTIONS, path.parent.name, str(path), path.name, _flat(text)))
    return notes


def instruction_files(claude: Path, codex: Path, project_roots: list[Path]) -> list[Path]:
    out = [claude / "CLAUDE.md", codex / "AGENTS.override.md", codex / "AGENTS.md"]
    for root in project_roots:
        out += [root / "CLAUDE.md", root / ".claude" / "CLAUDE.md", root / "AGENTS.md"]
    seen, unique = set(), []
    for p in out:
        k = str(p).lower()
        if k not in seen:
            seen.add(k)
            unique.append(p)
    return unique
