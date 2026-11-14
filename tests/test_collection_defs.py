"""Tests for #5634 S8: a new or edited collection definition runs by itself (modules/collection_defs.py,
live_record.apply_collection_defs and the trigger's watcher). Real inotify on tmp_path, real SQLite, no Kometa."""

from __future__ import annotations

import importlib.util
import threading
from pathlib import Path

import pytest

from modules import collection_defs, live_record

TRIGGER_PATH = Path(__file__).resolve().parent.parent / "deploy" / "trigger" / "import_trigger.py"

FILE = "collections:\n  Dogs:\n    letterboxd_list: https://letterboxd.com/a/list/dogs/\n    file_poster: /config/movies/assets/dogs.png\n    sync_mode: sync\n  Cats:\n    letterboxd_list: https://letterboxd.com/a/list/cats/\n"


@pytest.fixture
def env(monkeypatch, tmp_path):
    root = tmp_path
    (root / "config" / "movies").mkdir(parents=True)
    config = root / "config" / "config.yml"
    config.write_text("plex:\n  url: x\nlibraries:\n  Movies:\n    collection_files:\n    - file: config/movies/mine.yml\n    - default: basic\n  TV Shows:\n    collection_files:\n    - file: config/movies/mine.yml\n")
    spec = importlib.util.spec_from_file_location("import_trigger_s8", TRIGGER_PATH)
    trig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trig)
    monkeypatch.setattr(trig, "CONFIG_PATH", str(config))
    monkeypatch.setattr(trig, "RECORD_PATH", str(tmp_path / "rec.db"))
    mine = root / "config" / "movies" / "mine.yml"
    mine.write_text(FILE)
    return SimpleEnv(trig, mine, config)


class SimpleEnv:
    def __init__(self, trig, mine, config):
        self.trig, self.mine, self.config = trig, mine, config

    def sync(self):
        return self.trig.sync_collection_defs()[0]

    def builds(self):
        conn = live_record.connect(self.trig.RECORD_PATH)
        try:
            return sorted((b["library"], b["mapping_name"]) for b in live_record.pending_builds(conn))
        finally:
            conn.close()


def test_first_start_seeds_without_a_run(env):
    assert env.sync() == []
    assert env.builds() == []
    assert env.sync() == []


def test_new_collection_is_queued_in_every_library_that_lists_the_file(env):
    env.sync()
    env.mine.write_text(FILE + "  Birds:\n    letterboxd_list: https://letterboxd.com/a/list/birds/\n")
    assert env.sync() == [("Movies", "Birds"), ("TV Shows", "Birds")]
    assert env.builds() == [("Movies", "Birds"), ("TV Shows", "Birds")]


@pytest.mark.parametrize("old,new", [("https://letterboxd.com/a/list/dogs/", "https://letterboxd.com/a/list/dogs-2/"), ("/config/movies/assets/dogs.png", "/config/movies/assets/dogs2.png"), ("sync_mode: sync", "sync_mode: append")])
def test_changed_definition_queues_only_that_collection(env, old, new):
    env.sync()
    env.mine.write_text(FILE.replace(old, new))
    assert {name for _lib, name in env.sync()} == {"Dogs"}


def test_template_change_counts_and_unrelated_template_does_not(env):
    env.mine.write_text("templates:\n  t:\n    sort_title: a\n  u:\n    sort_title: z\ncollections:\n  Dogs:\n    template: {name: t}\n    letterboxd_list: x\n")
    env.sync()
    env.mine.write_text(env.mine.read_text().replace("sort_title: z", "sort_title: zz"))
    assert env.sync() == []
    env.mine.write_text(env.mine.read_text().replace("sort_title: a", "sort_title: b"))
    assert {n for _l, n in env.sync()} == {"Dogs"}


def test_rewrite_with_same_content_queues_nothing(env):
    env.sync()
    env.mine.write_text("# touched\n" + FILE.replace("    sync_mode: sync\n", "    sync_mode:   sync\n"))
    assert env.sync() == []
    assert env.builds() == []


def test_half_written_file_is_held_then_judged_on_the_next_write(env):
    env.sync()
    env.mine.write_text("collections:\n  Dogs:\n    letterboxd_list: [https://x\n")
    queued, held, _dirs = env.trig.sync_collection_defs()
    assert queued == [] and held == {str(env.mine)}
    for text in ("", "collections: [1"):
        env.mine.write_text(text)
        assert env.trig.sync_collection_defs()[1] == {str(env.mine)}
    assert env.builds() == []
    env.mine.write_text(FILE)  # complete and unchanged: still nothing, and the rows survived the hold
    assert env.sync() == []
    env.mine.write_text(FILE + "  Birds:\n    letterboxd_list: y\n")
    assert {n for _l, n in env.sync()} == {"Birds"}


def test_removed_collection_runs_nothing_and_drops_its_row(env):
    env.sync()
    env.mine.write_text("collections:\n  Cats:\n    letterboxd_list: https://letterboxd.com/a/list/cats/\n")
    assert env.sync() == []
    conn = live_record.connect(env.trig.RECORD_PATH)
    assert sorted({r["mapping_name"] for r in conn.execute("SELECT mapping_name FROM collection_defs")}) == ["Cats"]
    env.mine.write_text(FILE)  # coming back is a new definition
    assert {n for _l, n in env.sync()} == {"Dogs"}


def test_file_newly_listed_in_config_is_new(env):
    env.sync()
    (env.mine.parent / "extra.yml").write_text("collections:\n  Owls:\n    letterboxd_list: z\n")
    env.config.write_text(env.config.read_text().replace("    - default: basic\n", "    - default: basic\n    - file: config/movies/extra.yml\n"))
    assert env.sync() == [("Movies", "Owls")]


def test_file_template_variables_change_counts(env):
    env.sync()
    env.config.write_text(env.config.read_text().replace("- file: config/movies/mine.yml\n    - default", "- file: config/movies/mine.yml\n      template_variables: {a: 1}\n    - default"))
    assert {lib for lib, _n in env.sync()} == {"Movies"}


def test_unreadable_config_raises_so_the_caller_keeps_its_last_answer(env):
    env.sync()
    env.config.write_text("libraries: [")
    with pytest.raises(Exception):
        env.sync()
    assert env.builds() == []


def test_a_queued_request_is_renewed_not_duplicated(env):
    env.sync()
    env.mine.write_text(FILE.replace("dogs/", "dogs-2/"))
    env.sync()
    env.mine.write_text(FILE.replace("dogs/", "dogs-3/"))
    env.sync()
    conn = live_record.connect(env.trig.RECORD_PATH)
    rows = live_record.pending_builds(conn)
    assert [(r["mapping_name"], r["gen"]) for r in rows if r["library"] == "Movies"] == [("Dogs", 1)]


def test_schema_upgrade_from_v5_keeps_the_record_and_seeds_silently(tmp_path):
    path = str(tmp_path / "r.db")
    conn = live_record.connect(path)
    conn.execute("DROP TABLE collection_defs")
    conn.execute("PRAGMA user_version = 5")
    conn.close()
    conn = live_record.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == live_record.SCHEMA_VERSION
    assert live_record.apply_collection_defs(conn, {("Movies", "A"): ("f", "h")}) == []
    assert live_record.apply_collection_defs(conn, {("Movies", "A"): ("f", "h2")}) == [("Movies", "A")]


def test_burst_of_writes_is_one_pass_and_noise_is_ignored(env, tmp_path):
    watcher = collection_defs.Watcher(collection_defs.relevant_name)
    env.sync()
    watcher.watch({str(env.mine.parent), str(env.config.parent)})
    (env.config.parent / "config.cache").write_text("noise")
    assert watcher.wait(1) and watcher.drain() is False
    for i in range(5):  # a deploy rsync: many files, each written via a temp name and renamed into place
        tmp = env.mine.parent / f".mine.yml.{i}"
        tmp.write_text(FILE + f"  B{i}:\n    letterboxd_list: y\n")
        tmp.rename(env.mine)
    assert watcher.wait(1) and watcher.drain() is True
    watcher.settle(0.05)
    assert watcher.wait(0) is False
    assert {n for _l, n in env.sync()} == {"B4"}
    watcher.close()


class FakeWatcher:
    def __init__(self, events, stop):
        self.events, self.stop, self.dirs, self.settled, self.drains = list(events), stop, set(), 0, 0

    def watch(self, dirs):
        self.dirs |= set(dirs)

    def wait(self, timeout=None):
        if not self.events:
            self.stop.set()
        return True

    def drain(self):
        self.drains += 1
        return self.events.pop(0) if self.events else False

    def settle(self, quiet):
        self.settled += 1


def test_watcher_loop_seeds_then_runs_once_per_burst(env):
    stop, woke = threading.Event(), []
    fake = FakeWatcher([False, True], stop)
    t = threading.Thread(target=env.trig.collection_watcher, args=(lambda: woke.append(1),), kwargs={"quiet": 0, "watcher": fake, "stop": stop})
    env.mine.write_text(FILE)
    t.start()
    t.join(5)
    assert woke == [] and fake.settled == 1 and str(env.mine.parent) in fake.dirs  # seeded; the irrelevant event woke nothing
    stop2 = threading.Event()
    fake2 = FakeWatcher([True], stop2)
    env.mine.write_text(FILE + "  Birds:\n    letterboxd_list: y\n")
    t = threading.Thread(target=env.trig.collection_watcher, args=(lambda: woke.append(1),), kwargs={"quiet": 0, "watcher": fake2, "stop": stop2})
    t.start()
    t.join(5)
    assert woke == [1] and env.builds() == [("Movies", "Birds"), ("TV Shows", "Birds")]


def test_watcher_loop_survives_a_failed_pass(env):
    stop, woke = threading.Event(), []
    env.config.write_text("libraries: [")
    fake = FakeWatcher([True], stop)
    t = threading.Thread(target=env.trig.collection_watcher, args=(lambda: woke.append(1),), kwargs={"quiet": 0, "watcher": fake, "stop": stop})
    t.start()
    t.join(5)
    assert not t.is_alive() and woke == [] and str(env.config.parent) in fake.dirs
