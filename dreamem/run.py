"""``dreamem run``: one pass over every profile, under the lock.

Order, each step only after the one before it succeeded: lock → remove what a killed pass left (``*.partial``) →
due check (``--if-due``: already done today → exit at once) → read the sessions and notes → screen them → per
profile: reopen the questions nobody answered, build the shadow home, run the engine, delete the shadow ``state.db``,
publish (``dream.json``, ``questions.json``, ``report.md`` — each replaced in one step) → ``summary.json`` with
``last_success`` last. A pass killed anywhere leaves the previous night's files whole and ``last_success`` unmoved.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import config, notes, questions, report, screen, shadow, store
from .engineapi import forget_sweep as engine_forget_sweep
from .engineapi import run_pass
from .fsutil import read_json, remove_partials, write_json, write_private
from .lock import Lock
from .paths import Homes
from .sources import COMPLETE
from .sources import claude_code as src_claude
from .sources import codex as src_codex


def today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def due(homes: Homes) -> bool:
    last = (read_json(homes.summary, {}) or {}).get("last_success") or ""
    return last[:10] < today()


class _Projects:
    """cwd → holographic-memory's project key, cached: a night reads thousands of lines from a few dozen folders."""

    def __init__(self):
        self.cache: dict[str, str | None] = {}

    def __call__(self, m) -> str | None:
        if not m.cwd:
            return None
        if m.cwd not in self.cache:
            try:
                self.cache[m.cwd] = config.project_key(config.project_root(Path(m.cwd)))
            except (OSError, ValueError):
                self.cache[m.cwd] = None
        return self.cache[m.cwd]

    def roots(self) -> list[Path]:
        out = []
        for cwd in self.cache:
            try:
                root = config.project_root(Path(cwd))
            except (OSError, ValueError):
                continue
            if (root / ".git").exists() and root not in out:
                out.append(root)
        return out


def gather(homes: Homes, settings: dict, now: float) -> dict:
    """Everything a night reads, screened, with the coverage of each source."""
    days = int((settings.get("windows") or {}).get("corroboration_days", 30))
    since = now - days * 86400
    msgs, coverage = [], {}
    clients = settings.get("clients") or []
    if "claude-code" in clients:
        got, cov = src_claude.read(homes.claude_projects, since, now)
        msgs += got
        coverage["claude-code"] = cov.as_dict()
    if "codex" in clients:
        got, cov = src_codex.read(homes.codex_session_dirs(), since, now)
        msgs += got
        coverage["codex"] = cov.as_dict()
    kept, screened = screen.messages(msgs, int(settings.get("max_message_chars", 2000)))
    projects = _Projects()
    for m in kept:
        projects(m)
    auto, pressure, problems = notes.auto_memory(homes.claude_auto_memory_dirs()) if "claude-code" in clients \
        else ([], [], [])
    codex_notes, codex_problems = notes.codex_memory(homes.codex_memory_dirs()) if "codex" in clients else ([], [])
    auto, auto_screened = screen.notes(auto)
    codex_notes, codex_secret = screen.dedupe_targets(codex_notes)
    instr, instr_secret = screen.dedupe_targets(
        notes.instructions(notes.instruction_files(homes.claude, homes.codex, projects.roots())))
    return {"messages": kept, "coverage": coverage, "screened": screened, "projects": projects,
            "auto": auto, "pressure": pressure, "codex_notes": codex_notes, "instructions": instr,
            "notes_ok": not problems and not codex_problems,
            "notes_screened": {**auto_screened, "dedupe_secret_paragraphs": codex_secret + instr_secret}}


def _prepare_dry(pdir: Path, real: Path) -> None:
    """A dry run works on a copy of the profile's state, in a folder deleted afterwards."""
    for name in ("questions.json",):
        if (real / name).exists():
            shutil.copy2(real / name, pdir / name)
    if (real / "home" / "cache").is_dir():
        shutil.copytree(real / "home" / "cache", pdir / "home" / "cache", dirs_exist_ok=True)
    if (real / "home" / "memories" / "DREAMS.md").exists():
        (pdir / "home" / "memories").mkdir(parents=True, exist_ok=True)
        shutil.copy2(real / "home" / "memories" / "DREAMS.md", pdir / "home" / "memories" / "DREAMS.md")


def pass_profile(pdir: Path, profile: config.Profile, settings: dict, tz: str, data: dict) -> dict:
    """One profile's night; returns its summary entry (``error`` set when the engine failed)."""
    home = pdir / "home"
    (home / "cache").mkdir(parents=True, exist_ok=True)
    previous = read_json(pdir / "questions.json")
    reopened = questions.reopen_undecided(home, previous)
    ack = questions.ack_for(home, previous)
    project_of = data["projects"]
    msgs = data["messages"]
    if profile.project_key:
        msgs = [m for m in msgs if project_of(m) == profile.project_key]
    index = shadow.build(pdir, profile, settings, tz, msgs, project_of, data["auto"], data["codex_notes"],
                         data["instructions"], ack)
    out = home / "dream.json.partial"
    try:
        proc = run_pass(home, out)
    finally:
        shadow.drop_state_db(home)  # the person's words do not stay between nights
    write_private(pdir / "engine.log", proc.stderr[-20000:])
    if proc.returncode != 0 or not out.exists():
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        return {"error": f"engine exited {proc.returncode}: {tail[0][:300]}"}
    result = json.loads(out.read_text(encoding="utf-8"))
    out.unlink()
    coverage_ok = all(c["status"] == COMPLETE for c in data["coverage"].values()) and data["notes_ok"]
    ids = {i.get("fact_id") for k in ("promotions", "new_facts", "confirm", "trust_feedback", "fact_decays", "conflicts")
           for i in result.get(k) or []}
    facts = store.facts_state(profile.store, ids)
    qs, skipped = questions.build(result, store=str(profile.store) if profile.store else None, facts=facts,
                                  notes_index=index, coverage_ok=coverage_ok,
                                  max_questions=int(settings.get("max_questions", 7)), profile=profile.name)
    alerts = [a for a in result.get("alerts") or []
              if coverage_ok or a.get("kind") not in ("memory_loss", "entry_truncated", "probe_missing")]
    doc = {"generated_at": result["generated_at"], "profile": profile.name,
           "store": str(profile.store) if profile.store else None, "coverage": data["coverage"],
           "stats": result.get("stats") or {}, "questions": qs, "skipped": skipped, "alerts": alerts,
           "index_pressure": data["pressure"] if profile.notes else [],
           "screened": {**data["screened"], **{f"note_{k}": v for k, v in data["notes_screened"].items()}},
           "reopened": reopened, "acknowledged": bool(ack)}
    write_json(pdir / "dream.json", result)
    write_json(pdir / "questions.json", doc)
    write_private(pdir / "report.md", report.render(doc))
    sections: dict[str, int] = {}
    for q in qs:
        if q["status"] in questions.OPEN and q["status"] != questions.DEFERRED:
            sections[q["section"]] = sections.get(q["section"], 0) + 1
    return {"generated_at": doc["generated_at"], "open": sum(sections.values()), "sections": sections,
            "coverage": {k: v["status"] for k, v in data["coverage"].items()}}


def run(homes: Homes, *, if_due: bool = False, dry_run: bool = False, only: list[str] | None = None,
        out=sys.stdout) -> int:
    settings, warnings = config.load(homes)
    for w in warnings:
        print(f"warning: {w}", file=out)
    lock = Lock(homes.lock, wait=0 if if_due else 5)
    if not lock.acquire():
        if not if_due:
            print("another dreamem pass holds ~/.dreaming right now; try again in a minute", file=out)
        return 0 if if_due else 3
    try:
        if if_due and not due(homes):
            return 0
        remove_partials(homes.dreaming)
        now = time.time()
        tz = config.timezone_name(settings.get("timezone", "auto"))
        data = gather(homes, settings, now)
        summary = read_json(homes.summary, {}) or {}
        entries = dict(summary.get("profiles") or {})
        failed = 0
        for profile in config.profiles(homes, settings):
            if only and profile.name not in only:
                continue
            real = homes.profile(profile.name)
            if dry_run:
                pdir = homes.dreaming / f"dry-run-{uuid.uuid4().hex[:8]}" / profile.name
                pdir.mkdir(parents=True)
                _prepare_dry(pdir, real)
            else:
                pdir = real
                pdir.mkdir(parents=True, exist_ok=True)
            try:
                entry = pass_profile(pdir, profile, settings, tz, data)
                if dry_run and "error" not in entry:
                    print((pdir / "report.md").read_text(encoding="utf-8"), file=out)
            finally:
                if dry_run:
                    shutil.rmtree(pdir.parent, ignore_errors=True)
            if "error" in entry:
                failed += 1
                print(f"{profile.name}: {entry['error']}", file=out)
                entries.setdefault(profile.name, {})["error"] = entry["error"]
                continue
            entries[profile.name] = entry
            if not dry_run:
                print(f"{profile.name}: {entry['open']} question(s); report {pdir / 'report.md'}", file=out)
        if dry_run:
            return 1 if failed else 0
        summary["profiles"] = entries
        if not failed:
            summary["last_success"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json(homes.summary, summary)
        return 1 if failed else 0
    finally:
        lock.release()


def forget_sweep(homes: Homes, out=sys.stdout) -> int:
    """Remove facts forgotten since the night from our copies: the questions (and the report rendered from them), the
    night's JSON, and — through the engine's own sweep — its cooldowns, reject list, proposals and diary."""
    with Lock(homes.lock, wait=10):
        problems = 0
        for pdir in sorted(homes.profiles.glob("*")) if homes.profiles.is_dir() else []:
            doc = read_json(pdir / "questions.json")
            if not doc:
                continue
            gone = store.forgotten(Path(doc["store"])) if doc.get("store") else set()
            scrubbed = 0
            for q in doc["questions"]:
                ids = {q.get("fact_id")} | {(q.get(s) or {}).get("fact_id") for s in ("keep", "copy")}
                if gone & {i for i in ids if isinstance(i, int)}:
                    for k in ("content", "keep", "copy", "why", "conflicts", "call", "text"):
                        q.pop(k, None)
                    if q["status"] in questions.OPEN:
                        q["status"], q["failure"] = questions.FAILED, "forgotten"
                    scrubbed += 1
            if scrubbed:
                write_json(pdir / "questions.json", doc)
                write_private(pdir / "report.md", report.render(doc))
                (pdir / "dream.json").unlink(missing_ok=True)
            if doc.get("store") and (pdir / "home" / "dreaming.json").exists():
                problems += engine_forget_sweep(pdir / "home").returncode != 0
            print(f"{pdir.name}: {scrubbed} question(s) scrubbed", file=out)
        return 1 if problems else 0


def status(homes: Homes, out=sys.stdout) -> int:
    settings, warnings = config.load(homes)
    summary = read_json(homes.summary, {}) or {}
    store_cfg = config.holomem_store(homes)
    print(f"home: {homes.dreaming}", file=out)
    print(f"clients: {', '.join(settings.get('clients') or [])}; timezone: "
          f"{config.timezone_name(settings.get('timezone', 'auto'))}", file=out)
    found = store_cfg.path is not None and store_cfg.path.exists()
    print(f"holographic-memory: {store_cfg.path} (scope {store_cfg.scope}){'' if found else ' — not found'}", file=out)
    for p in config.profiles(homes, settings):
        entry = (summary.get("profiles") or {}).get(p.name) or {}
        err = f" — error: {entry['error']}" if entry.get("error") else ""
        cov = ", ".join(f"{k} {v}" for k, v in (entry.get("coverage") or {}).items())
        print(f"profile {p.name}: store {p.store or 'none'}; last night {entry.get('generated_at', 'never')}; "
              f"open {entry.get('open', 0)}{'; coverage ' + cov if cov else ''}{err}", file=out)
    print(f"last success: {summary.get('last_success', 'never')}; due now: {due(homes)}", file=out)
    for w in warnings:
        print(f"warning: {w}", file=out)
    return 0
