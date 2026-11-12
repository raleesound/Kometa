"""Pushing the watched Letterboxd lists to the bridge (modules/live_watch.py, epic #5634 S5)."""

from __future__ import annotations

from unittest.mock import MagicMock

from modules import live_watch
from tests.conftest import FakeLogger

LIST_A = "https://letterboxd.com/someone/list/a/"


def session(status_ok=True):
    response = MagicMock()
    if not status_ok:
        response.raise_for_status.side_effect = RuntimeError("503 Service Unavailable")
    s = MagicMock()
    s.put.return_value = response
    return s


def test_puts_the_replace_set_with_the_bearer_token(monkeypatch):
    monkeypatch.setenv("LB_BRIDGE_URL", "http://bridge/api/")
    monkeypatch.setenv("LB_BRIDGE_TOKEN", "sekrit")
    s = session()
    assert live_watch.push([LIST_A], FakeLogger(), session=s) is True
    args, kwargs = s.put.call_args
    assert args[0] == "http://bridge/api/watched-lists"
    assert kwargs["json"] == [{"url": LIST_A}]
    assert kwargs["headers"] == {"Authorization": "Bearer sekrit"}


def test_an_empty_set_is_still_pushed_so_removed_collections_stop_being_watched(monkeypatch):
    monkeypatch.setenv("LB_BRIDGE_URL", "http://bridge/api")
    monkeypatch.setenv("LB_BRIDGE_TOKEN", "t")
    s = session()
    assert live_watch.push([], FakeLogger(), session=s) is True
    assert s.put.call_args.kwargs["json"] == []


def test_unconfigured_pushes_nothing(monkeypatch):
    monkeypatch.delenv("LB_BRIDGE_URL", raising=False)
    monkeypatch.delenv("LB_BRIDGE_TOKEN", raising=False)
    s = session()
    assert live_watch.push([LIST_A], FakeLogger(), session=s) is False
    s.put.assert_not_called()


def test_a_bridge_failure_is_logged_and_swallowed_without_leaking_the_token(monkeypatch):
    monkeypatch.setenv("LB_BRIDGE_URL", "http://bridge/api")
    monkeypatch.setenv("LB_BRIDGE_TOKEN", "sekrit")
    log = FakeLogger()
    assert live_watch.push([LIST_A], log, session=session(status_ok=False)) is False
    assert "sekrit" not in repr(vars(log))
