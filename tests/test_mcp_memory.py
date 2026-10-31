"""The reference MCP `memory` server (2.0.1) as a memory system: its JSONL file read, never written; every answer a
call of its own tools, checked by the exact text. The fixture is a file the server itself wrote (synthetic content)."""
import io
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from dreamem import questions, review, run
from dreamem.providers.mcp_memory import McpMemory

FIXTURES = Path(__file__).parent / "fixtures" / "mcp-memory-2.0.1"
SCHEMAS = json.loads((FIXTURES / "schemas.json").read_text(encoding="utf-8"))


@pytest.fixture
def mem(world, tmp_path):
    path = tmp_path / "graph" / "memory.jsonl"
    path.parent.mkdir()
    shutil.copy(FIXTURES / "memory.jsonl", path)
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text(f'[providers.mcp-memory]\npath = "{path.as_posix()}"\n', encoding="utf-8")
    return path


def night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return world.questions("mcp-memory")


def valid(call):
    """The call's arguments as the server's own inputSchema takes them (2.0.1, tools/list)."""
    schema = SCHEMAS[call["tool"]]
    args = call["args"]
    assert set(schema["required"]) <= set(args) <= set(schema["properties"])
    for key, value in args.items():
        item = schema["properties"][key]["items"]
        for row in value:
            assert set(item["required"]) <= set(row) <= set(item["properties"])
            assert all(isinstance(v, (str, list)) for v in row.values())
    return True


def test_each_observation_is_a_fact_and_relations_are_not(world, mem, tmp_path):
    facts = McpMemory(mem, tmp_path / "st").facts()
    assert sorted(f["content"] for f in facts) == ["Has a cat named Pepper", "Opens at 9 am on weekdays",
                                                   "Prefers oat milk", "Runs a coffee shop on Sadovaya street"]
    kept = (tmp_path / "st" / "ids.json").read_text(encoding="utf-8") + \
        (tmp_path / "st" / "first-seen.json").read_text(encoding="utf-8")
    assert "Pepper" not in kept and "Alex" not in kept  # digests and dates, never the words


def test_a_line_cut_off_leaves_the_rest_and_makes_coverage_partial(world, mem):
    mem.write_bytes(mem.read_bytes() + b'{"type":"entity","name":"Bo')
    world.say({1: ["касса сошлась"]})
    doc = night(world)
    assert doc["store_coverage"]["status"] == "partial"
    prov = McpMemory(mem, world.homes.profile("mcp-memory") / "state")
    assert len(prov.facts()) == 4 and not prov.can_decay()


def test_the_calls_take_the_servers_own_arguments(world, mem):
    world.say({6: ["У меня кошка Pepper, Has a cat named Pepper"], 4: ["Has a cat named Pepper, кошка дома"],
               2: ["кошка Pepper опять Has a cat named Pepper"]})
    q = next(q for q in night(world)["questions"] if q.get("content") == "Has a cat named Pepper")
    assert q["provider"] == "mcp-memory" and q["locator"] == {"entity": "Alex", "text": "Has a cat named Pepper"}
    assert set(questions.answers(q)) == {"yes", "no", "outdated", "wrong", "forget", "reject"}
    out = []
    review.decide(world.homes, q["qid"], "outdated", out=out.append)
    todo = json.loads(out[-1])
    assert todo["call"] == {"tool": "delete_observations", "args": {"deletions": [
        {"entityName": "Alex", "observations": ["Has a cat named Pepper"]}]}} and valid(todo["call"])
    assert todo["check"] == {"memory_file": str(mem), "entity": "Alex", "observation": "Has a cat named Pepper"}
    review.mark(world.homes, q["qid"], "undo", out=lambda *_: None)
    review.decide(world.homes, q["qid"], "wrong", text="Has a dog named Pepper", out=out.append)
    calls = json.loads(out[-1])["calls"]
    assert [c["tool"] for c in calls] == ["add_observations", "delete_observations"]  # add first: nothing is lost
    assert calls[0]["args"] == {"observations": [{"entityName": "Alex", "contents": ["Has a dog named Pepper"]}]}
    assert all(valid(c) for c in calls)


def test_save_goes_to_the_entity_the_agent_names(world, mem):
    q = {"qid": "Q1-abcd", "section": "new_candidate", "status": "shown", "provider": "mcp-memory",
         "store": str(mem), "clause": "c" * 16}
    with pytest.raises(ValueError, match="search_nodes"):
        questions.decide(dict(q), "save", "Alex drinks oat milk")
    todo = questions.decide({**q, "entity": "Alex"}, "save", "Alex drinks oat milk")
    assert todo["call"] == {"tool": "add_observations", "args": {"observations": [
        {"entityName": "Alex", "contents": ["Alex drinks oat milk"]}]}} and valid(todo["call"])


def test_an_observation_deleted_since_is_never_shown_again(world, mem):
    world.say({6: ["кошка Pepper, Has a cat named Pepper"], 4: ["Has a cat named Pepper, кошка"],
               2: ["Has a cat named Pepper снова"]})
    night(world)
    mem.write_text(mem.read_text(encoding="utf-8").replace(',"Has a cat named Pepper"', ""), encoding="utf-8")
    lines = []
    review.show(world.homes, as_json=True, out=lines.append)
    shown = [q for block in json.loads(lines[0]) for q in block["questions"] if q["section"] != "new_candidate"]
    assert not [q for q in shown if "Pepper" in json.dumps(q)]  # (what the person said may come back as a candidate)
    assert run.forget_sweep(world.homes, out=io.StringIO()) == 0
    assert b"Pepper" not in (world.homes.profile("mcp-memory") / "questions.json").read_bytes()


def test_still_true_waits_for_two_windows_of_first_sightings(world, mem):
    state = world.homes.profile("mcp-memory") / "state"
    prov = McpMemory(mem, state)
    prov.facts()
    assert not prov.can_decay()
    journal = json.loads((state / "first-seen.json").read_text(encoding="utf-8"))
    journal["since"] = (datetime.now() - timedelta(days=61)).strftime("%Y-%m-%d")
    (state / "first-seen.json").write_text(json.dumps(journal), encoding="utf-8")
    again = McpMemory(mem, state)
    again.facts()
    assert again.can_decay()


def test_the_file_is_found_in_claudes_mcp_config(world, tmp_path):
    path = tmp_path / "kg.jsonl"
    shutil.copy(FIXTURES / "memory.jsonl", path)
    (world.homes.claude / ".claude.json").write_text(json.dumps({"mcpServers": {"memory": {
        "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"],
        "env": {"MEMORY_FILE_PATH": str(path)}}}}), encoding="utf-8")
    world.say({1: ["касса сошлась"]})
    assert run.run(world.homes, out=io.StringIO()) == 0
    assert (world.homes.profile("mcp-memory") / "questions.json").exists()
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "MCP memory server" in out.getvalue() and str(path) in out.getvalue()
    world.homes.config.write_text("[providers.mcp-memory]\nenabled = false\n", encoding="utf-8")
    out = io.StringIO()
    run.status(world.homes, out=out)
    assert "MCP memory server" not in out.getvalue()


def test_the_pass_never_writes_the_servers_file(world, mem):
    before = mem.read_bytes()
    world.say({1: ["касса сошлась"]})
    doc = night(world)
    assert doc["provider"] == "mcp-memory" and mem.read_bytes() == before
