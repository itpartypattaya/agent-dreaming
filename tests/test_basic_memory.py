"""Basic Memory is optional and read only: existing observations prevent repeat suggestions."""
import io
import time

from dreamem import config, run


def test_basic_memory_observations_are_read_only_dedupe_targets(world):
    root = world.root / "basic-memory"
    root.mkdir()
    note = root / "coffee.md"
    note.write_text("---\ntitle: Coffee\n---\n\n# Coffee\n\n## Observations\n"
                    "- [preference] Prefers oat milk in coffee\n"
                    "- works_with [[Coffee shop]]\n", encoding="utf-8")
    before = note.read_bytes()
    settings = dict(config.DEFAULTS)
    settings["providers"] = {"basic-memory": {"enabled": True, "path": str(root)}}
    data = run.gather(world.homes, settings, time.time(), keep=False)
    assert any("Prefers oat milk in coffee" in n.text for n in data["basic_notes"])
    assert not any("works_with" in n.text for n in data["basic_notes"])
    assert note.read_bytes() == before


def test_basic_memory_is_off_by_default_and_missing_path_is_reported(world):
    settings = dict(config.DEFAULTS)
    assert run.gather(world.homes, settings, time.time(), keep=False)["basic_notes"] == []
    settings["providers"] = {"basic-memory": {"enabled": True, "path": str(world.root / "missing")}}
    data = run.gather(world.homes, settings, time.time(), keep=False)
    assert not data["notes_ok"] and data["notes_problems"]


def test_basic_memory_observation_prevents_a_duplicate_new_candidate(world):
    root = world.root / "basic-memory"
    root.mkdir()
    note = root / "people.md"
    note.write_text("# People\n\n## Observations\n"
                    "- [fact] Анна с мужем Олегом живут в Тбилиси пятый год\n", encoding="utf-8")
    world.fact("Анна работает дизайнером интерфейсов", origin="said", days_old=40)
    world.say({2: ["Мы с мужем Олегом живём в Тбилиси уже пятый год"]})
    world.homes.dreaming.mkdir(parents=True, exist_ok=True)
    world.homes.config.write_text(f'[providers.basic-memory]\nenabled = true\npath = "{root.as_posix()}"\n',
                                  encoding="utf-8")
    assert run.run(world.homes, out=io.StringIO()) == 0
    assert not [q for q in world.questions()["questions"] if q["section"] == "new_candidate"]
