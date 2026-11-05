"""The delayed LOG_STREAM flush runs in the context of the code that logged."""

import contextvars
import threading

from ondemand.worker import logging as wlog

_marker = contextvars.ContextVar("marker", default=None)


def test_timer_flush_sees_the_callers_context(monkeypatch):
    seen = {}
    done = threading.Event()
    handler = wlog.OndemandLogHandler()
    monkeypatch.setattr(handler, "FLUSH_INTERVAL_SECONDS", 0.01)

    def fake_flush():
        seen["marker"] = _marker.get()
        done.set()

    monkeypatch.setattr(handler, "flush", fake_flush)
    token = _marker.set("activity-ctx")
    try:
        handler.add_line("hello")
    finally:
        _marker.reset(token)
    assert done.wait(2)
    assert seen["marker"] == "activity-ctx"
