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
    assert env.sync() == []  # gone on a second pass in a row: the row goes
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
    live_record.queue_build(conn, "Movies", "Old", "f.yml", renew=True)
    conn.execute("INSERT INTO arrivals (library, rating_key, payload, created_at) VALUES ('Movies', 1, '{}', 'now')")
    conn.execute("INSERT INTO watch_seen (url, hash, seen_at) VALUES ('u', 'h', 'now')")
    conn.commit()
    before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("builds", "arrivals", "watch_seen")}
    conn.execute("DROP TABLE collection_defs")
    conn.execute("PRAGMA user_version = 5")
    conn.close()
    conn = live_record.connect(path)
    assert {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before} == before and before == {"builds": 1, "arrivals": 1, "watch_seen": 1}
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

    def nudge(self):
        self.nudged = getattr(self, "nudged", 0) + 1


def test_watcher_loop_seeds_then_runs_once_per_burst(env):
    stop, woke = threading.Event(), []
    fake = FakeWatcher([False, True], stop)
    t = threading.Thread(target=env.trig.collection_watcher, args=(lambda: woke.append(1),), kwargs={"quiet": 0, "watcher": fake, "stop": stop})
    env.mine.write_text(FILE)
    t.start()
    t.join(5)
    assert not t.is_alive() and woke == [] and fake.settled == 1 and str(env.mine.parent) in fake.dirs  # seeded; the irrelevant event woke nothing
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


def test_a_file_truncated_to_nothing_is_not_a_mass_removal_and_its_completion_is_not_a_mass_rebuild(env):
    env.sync()
    env.mine.write_text("collections:\n")  # cut at a line boundary: parses, and holds nothing
    env.sync()
    env.mine.write_text(FILE)  # the write completes: Cats comes back, unchanged
    assert env.sync() == [] and env.builds() == []


def test_a_truncated_config_is_not_a_mass_removal_either(env):
    env.sync()
    env.config.write_text("plex:\n  url: x\nlibraries:\n")
    env.sync()
    env.config.write_text("plex:\n  url: x\nlibraries:\n  Movies:\n    collection_files:\n    - file: config/movies/mine.yml\n  TV Shows:\n    collection_files:\n    - file: config/movies/mine.yml\n")
    assert env.sync() == [] and env.builds() == []


def test_a_collection_gone_on_two_passes_in_a_row_is_dropped(env):
    env.sync()
    env.mine.write_text("collections:\n  Cats:\n    letterboxd_list: https://letterboxd.com/a/list/cats/\n")
    env.sync()
    env.sync()
    conn = live_record.connect(env.trig.RECORD_PATH)
    assert {r["mapping_name"] for r in conn.execute("SELECT mapping_name FROM collection_defs")} == {"Cats"}


def test_a_file_held_at_the_seed_is_seeded_silently_while_other_files_queue_from_the_start(env):
    other = env.mine.parent / "other.yml"
    other.write_text("collections:\n  Owls:\n    letterboxd_list: o\n")
    env.config.write_text(env.config.read_text().replace("    - default: basic\n", "    - default: basic\n    - file: config/movies/other.yml\n"))
    other.write_text("collections:\n  Owls: [\n")  # held at the first pass
    env.sync()  # seeds from mine.yml
    assert env.builds() == []
    mine_text = env.mine.read_text()
    env.mine.write_text(mine_text + "  Birds:\n    letterboxd_list: y\n")  # an edit elsewhere is not swallowed by the held file
    assert {n for _l, n in env.sync()} == {"Birds"}
    other.write_text("collections:\n  Owls:\n    letterboxd_list: o\n")  # the held file's first good read: silent
    assert env.sync() == []
    other.write_text("collections:\n  Owls:\n    letterboxd_list: o2\n")  # then it is an ordinary file
    assert {n for _l, n in env.sync()} == {"Owls"}


def test_a_pathological_file_is_held_and_the_rest_still_work(env):
    env.sync()
    (env.mine.parent / "odd.yml").write_text("collections:\n  Odd:\n    1: a\n    b: 2\n")
    env.config.write_text(env.config.read_text().replace("    - default: basic\n", "    - default: basic\n    - file: config/movies/odd.yml\n"))
    env.sync()
    env.mine.write_text(FILE + "  Birds:\n    letterboxd_list: y\n")
    assert {n for _l, n in env.sync()} == {"Birds"}


def test_a_recreated_directory_is_watched_again(env, tmp_path):
    watcher = collection_defs.Watcher(collection_defs.relevant_name)
    directory = tmp_path / "config" / "tv"
    directory.mkdir()
    watcher.watch({str(directory)})
    assert str(directory) in watcher.dirs
    directory.rmdir()
    assert watcher.wait(1) and watcher.drain() is True
    assert str(directory) not in watcher.dirs
    directory.mkdir()
    watcher.watch({str(directory)})
    assert str(directory) in watcher.dirs
    (directory / "x.yml").write_text("a: 1")
    assert watcher.wait(1) and watcher.drain() is True
    watcher.watch({str(tmp_path / "missing")})
    assert str(tmp_path / "missing") in watcher.failed
    watcher.close()


def test_folder_entries_are_watched_even_when_empty(env, tmp_path):
    (tmp_path / "config" / "extra").mkdir()
    env.config.write_text(env.config.read_text().replace("    - default: basic\n", "    - folder: config/extra\n"))
    _found, _held, dirs = collection_defs.scan(str(env.config), str(tmp_path))
    assert str(tmp_path / "config" / "extra") in dirs


def test_a_watcher_that_cannot_start_does_not_kill_the_thread(env, monkeypatch):
    def boom(*_a, **_k):
        raise OSError(24, "too many open files")

    monkeypatch.setattr(collection_defs, "Watcher", boom)
    env.trig.collection_watcher(lambda: None)


def test_queued_collections_wake_the_builder_even_when_the_second_pass_fails(env, monkeypatch):
    stop, woke = threading.Event(), []
    real = env.trig.sync_collection_defs
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("record busy")
        return real()

    env.mine.write_text(FILE)
    real()
    env.mine.write_text(FILE + "  Birds:\n    letterboxd_list: y\n")
    monkeypatch.setattr(env.trig, "sync_collection_defs", flaky)
    fake = FakeWatcher([], stop)
    env.trig.collection_watcher(lambda: woke.append(1), quiet=0, watcher=fake, stop=stop)
    assert woke == [1] and len(calls) >= 2


def test_a_failed_pass_is_retried_once_the_dirs_are_watched_and_on_gate_idle(env):
    gate = env.trig.BuildGate()
    stop, woke = threading.Event(), []
    env.config.write_text("libraries: [")
    fake = FakeWatcher([], stop)
    t = threading.Thread(target=env.trig.collection_watcher, args=(lambda: woke.append(1),), kwargs={"quiet": 0, "watcher": fake, "stop": stop, "gate": gate})
    t.start()
    t.join(5)
    assert not t.is_alive()
    gate.worker_idle()
    assert getattr(fake, "nudged", 0) == 1  # still failing: the idle gate asks for another pass


def test_a_write_during_a_failed_first_pass_is_not_absorbed(env, monkeypatch):
    real, calls = env.trig.sync_collection_defs, []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("half written")
        return real(*a, **k)

    monkeypatch.setattr(env.trig, "sync_collection_defs", flaky)
    stop = threading.Event()
    fake = FakeWatcher([], stop)
    env.trig.collection_watcher(lambda: None, quiet=0, watcher=fake, stop=stop)
    assert len(calls) >= 2 and env.builds() == []  # retried at once, and that retry seeded


def test_a_watcher_whose_reads_raise_backs_off_instead_of_spinning(env, monkeypatch):
    stop, waits = threading.Event(), []
    monkeypatch.setattr(stop, "wait", lambda t: waits.append(t) or len(waits) >= 3 and stop.set())

    class Broken(FakeWatcher):
        def wait(self, timeout=None):
            raise OSError(9, "bad fd")

    env.trig.collection_watcher(lambda: None, quiet=0, watcher=Broken([], stop), stop=stop)
    assert waits[:3] == [2.0, 4.0, 8.0]


def test_a_new_subdirectory_and_a_moved_directory_are_noticed(env, tmp_path):
    watcher = collection_defs.Watcher(collection_defs.relevant_name)
    base = tmp_path / "config"
    watcher.watch({str(base)})
    (base / "tv").mkdir()  # a new directory: relevant, so the next pass can watch it
    assert watcher.wait(1) and watcher.drain() is True
    (base / "movies").rename(base / "movies_old")
    assert watcher.wait(1) and watcher.drain() is True
    watcher.close()


def test_the_watcher_nudge_wakes_wait_and_counts_as_relevant(env):
    watcher = collection_defs.Watcher(collection_defs.relevant_name)
    watcher.nudge()
    assert watcher.wait(1) and watcher.drain() is True and watcher.wait(0) is False
    watcher.close()


def test_a_burst_through_the_real_loop_and_real_inotify_is_one_wake_with_the_final_content(env):
    stop, woke, ready = threading.Event(), [], threading.Event()
    real = env.trig.sync_collection_defs
    passes = []

    def counted(*a, **k):
        out = real(*a, **k)
        passes.append(1)
        ready.set()
        return out

    env.trig.sync_collection_defs = counted
    t = threading.Thread(target=env.trig.collection_watcher, args=(lambda: woke.append(1),), kwargs={"quiet": 0.3, "stop": stop}, daemon=True)
    t.start()
    assert ready.wait(5)  # seeded, watching
    import time

    time.sleep(0.5)  # its second read (after the first watch) is done
    base = len(passes)
    for i in range(5):  # a deploy rsync: temp file renamed into place, five times in a burst
        tmp = env.mine.parent / f".mine.yml.{i}"
        tmp.write_text(FILE + f"  B{i}:\n    letterboxd_list: y\n")
        tmp.rename(env.mine)
    deadline = time.monotonic() + 5
    while not woke and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    stop.set()
    (env.mine.parent / "poke.yml").write_text("a: 1")  # unblocks select so the loop sees stop
    t.join(5)
    assert not t.is_alive() and woke and len(woke) <= 2  # a deploy burst wakes the builder, not once per write
    assert env.builds() == [("Movies", "B4"), ("TV Shows", "B4")]  # the final content, once each
    assert len(passes) - base < 5


def test_a_watched_directory_that_is_moved_away_and_recreated_is_watched_again(env, tmp_path):
    watcher = collection_defs.Watcher(collection_defs.relevant_name)
    directory = tmp_path / "config" / "movies"
    watcher.watch({str(directory)})
    directory.rename(tmp_path / "config" / "movies_old")
    assert watcher.wait(1) and watcher.drain() is True
    assert str(directory) not in watcher.dirs  # the IN_MOVE_SELF branch dropped it
    directory.mkdir()
    watcher.watch({str(directory)})
    (directory / "x.yml").write_text("a: 1")
    assert watcher.wait(1) and watcher.drain() is True
    watcher.close()
