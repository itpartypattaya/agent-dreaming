"""Memory systems other than holographic-memory: the seam (a fact source for the pass, a writer for the answers) and
holographic-memory kept exactly as it was — every memory call the review can print, byte for byte."""
import json
from pathlib import Path

from dreamem import questions

GOLDEN = Path(__file__).parent / "fixtures" / "holomem-0.7.0-calls.json"


def all_calls() -> str:
    """What `decide` returns and records for every answer of every section, on one synthetic question per section."""
    out = {}
    for section in questions.ORDER:
        if section not in questions.ANSWERS:
            continue  # a Claude Code note's question (claude_notes): not a holographic-memory call
        for answer in [*questions.ANSWERS[section], questions.LATER]:
            q = {"qid": "Q1-abcd", "section": section, "status": questions.SHOWN, "fact_id": 7, "version_id": 12,
                 "content": "x", "store": "C:/holo/memory_store.db", "keep": {"fact_id": 7, "version_id": 12},
                 "copy": {"fact_id": 9, "version_id": 30}, "proposal": "D-1", "clause": "c" * 16}
            try:
                todo = questions.decide(q, answer, "new text")
            except ValueError as exc:
                out[f"{section}/{answer}"] = {"refused": str(exc)}
                continue
            out[f"{section}/{answer}"] = {"todo": todo, "status": q["status"], "answer": q["answer"],
                                          "call": q.get("call"), "text": q.get("text")}
    return json.dumps(out, ensure_ascii=False, sort_keys=True, indent=1) + "\n"


def test_holomem_calls_unchanged():
    assert all_calls() == GOLDEN.read_text(encoding="utf-8")


# -- the seam: a memory system the engine does not read itself ------------------------------------------------------

import io  # noqa: E402

import pytest  # noqa: E402
from world import days_ago  # noqa: E402

from dreamem import providers, review, run  # noqa: E402
from dreamem.providers.base import NOTHING, Provider  # noqa: E402
from dreamem.providers.ids import Ids  # noqa: E402

MARINA = "Кассир Марина работает по субботам и воскресеньям"
FRIDAY = "Магазин закрывается в девять вечера по пятницам"


class Fake(Provider):
    """Facts with string ids, no versions, no trust, no history: it can confirm nothing and end nothing."""
    name = "fake"
    title = "fake memory"
    stored = {"m-1": (MARINA, 50), "m-2": (FRIDAY, 40)}

    def _ids(self):
        return Ids(self.state_dir)

    def facts(self):
        ids, out = self._ids(), []
        for key, (text, age) in self.stored.items():
            stamp = __import__("datetime").datetime.fromtimestamp(days_ago(age)).strftime("%Y-%m-%d %H:%M:%S")
            out.append({"fact_id": ids.of(key), "content": text, "category": "general", "tags": "",
                        "trust_score": None, "retrieval_count": None, "helpful_count": None, "created_at": stamp,
                        "updated_at": stamp})
        ids.save()
        return out

    def heads(self, ids):
        known = {v: k for k, v in self._ids().known().items()}
        return {i: {"version_id": None, "content": self.stored[known[i]][0], "origin": None, "status": "current",
                    "locator": known[i]} for i in ids if i in known}

    def answers(self, section):
        forget = lambda q, text: {"tool": "fake_delete", "args": {"id": q["locator"]}}  # noqa: E731
        return {"yes": NOTHING, "no": NOTHING, "forget": forget} if section in ("review", "new_fact") else {}


@pytest.fixture
def fake_world(tmp_path):
    from world import World
    w = World(tmp_path, store=False)
    providers.register(Fake)
    store = tmp_path / "fake.json"
    store.write_text("{}", encoding="utf-8")
    w.homes.dreaming.mkdir(parents=True, exist_ok=True)
    w.homes.config.write_text(f'[providers.fake]\nenabled = true\npath = "{store.as_posix()}"\n', encoding="utf-8")
    yield w
    providers.PROVIDERS.pop("fake", None)


def _night(world):
    assert run.run(world.homes, out=io.StringIO()) == 0
    return __import__("json").loads((world.homes.profile("fake") / "questions.json").read_text(encoding="utf-8"))


def test_external_source_reaches_the_engine(fake_world):
    fake_world.say({6: ["Марина снова работает по субботам, кассир на месте"],
                    4: ["Кассир Марина в субботу опять работает"], 2: ["Марина в воскресенье тоже работает"]})
    doc = _night(fake_world)
    by_section = {q["section"] for q in doc["questions"] if q.get("content") == MARINA}
    assert by_section == {"review"}  # corroborated; without a known origin a person decides
    assert not {"trust_feedback", "confirm", "promotion"} & {q["section"] for q in doc["questions"]}
    q = next(q for q in doc["questions"] if q.get("content") == MARINA)
    assert q["provider"] == "fake" and q["locator"] == "m-1"


def test_external_ids_are_stable(fake_world):
    first = {f["content"]: f["fact_id"] for f in Fake(None, fake_world.root / "st").facts()}
    again = Fake(None, fake_world.root / "st")
    again.stored = dict(reversed(Fake.stored.items()))  # read in another order the next night
    second = {f["content"]: f["fact_id"] for f in again.facts()}
    assert first == second and sorted(first.values()) == [1, 2]
    kept = (fake_world.root / "st" / "ids.json").read_text(encoding="utf-8")
    assert "Марина" not in kept and "m-1" in kept


def test_unsupported_answer_not_offered(fake_world):
    fake_world.say({6: ["Марина снова работает по субботам, кассир на месте"],
                    4: ["Кассир Марина в субботу опять работает"], 2: ["Марина в воскресенье тоже работает"]})
    _night(fake_world)
    lines = []
    review.show(fake_world.homes, out=lines.append)
    line = next(x for x in "\n".join(lines).split("\n- ") if MARINA in x)
    assert "answers: yes, no, forget, later" in line and "outdated" not in line
    qid = line.split()[0]
    out = []
    assert review.decide(fake_world.homes, qid, "outdated", out=out.append) == 2
    assert review.decide(fake_world.homes, qid, "forget", out=out.append) == 0
    assert __import__("json").loads(out[-1])["call"] == {"tool": "fake_delete", "args": {"id": "m-1"}}
