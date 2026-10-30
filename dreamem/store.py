"""holographic-memory's fact store, read-only (``mode=ro``): versions, origins, tombstones. Never written here."""
from __future__ import annotations

import sqlite3
from pathlib import Path

ORIGINS = ("said", "confirmed", "inferred")


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def facts_state(path: Path | None, ids) -> dict[int, dict]:
    """{fact_id: {version_id, origin}} of the current versions; origin as the engine reads it (a confirmation turns an
    inference or an unknown origin into ``confirmed``). Empty when the store is missing or older."""
    ids = sorted({int(i) for i in ids if str(i).isdigit()})
    if path is None or not ids or not Path(path).exists():
        return {}
    out: dict[int, dict] = {}
    conn = connect(path)
    try:
        cols = _columns(conn, "fact_versions")
        if not cols:
            return {}
        marks = ",".join("?" * len(ids))
        origin = "src_origin" if "src_origin" in cols else "null"
        for fid, vid, org in conn.execute(
                f"select fact_id, version_id, {origin} from fact_versions where status='current' and fact_id in ({marks})",
                ids):
            out[int(fid)] = {"version_id": vid, "origin": org if org in ORIGINS else None}
        if _columns(conn, "fact_confirmations"):
            for (fid,) in conn.execute(f"select distinct fact_id from fact_confirmations where fact_id in ({marks})", ids):
                rec = out.setdefault(int(fid), {"version_id": None, "origin": None})
                if rec["origin"] in (None, "inferred"):
                    rec["origin"] = "confirmed"
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def forgotten(path: Path | None) -> set[int]:
    if path is None or not Path(path).exists():
        return set()
    conn = connect(path)
    try:
        if not _columns(conn, "forgotten"):
            return set()
        return {int(r[0]) for r in conn.execute("select fact_id from forgotten")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()
