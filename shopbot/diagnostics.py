"""Диагностика одного импорта. Параллельные запросы получают отдельные отчёты.

step() пишет строку для людей, event() — структурированную запись об обращении к источнику:
что спросили, чем кончилось, сколько заняло и когда можно повторить.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_active = ContextVar("import_trace", default=None)
_events = ContextVar("import_events", default=None)


@dataclass
class Event:
    source: str  # «ozon.page», «wb.detail», «wbcon.wb», «bhapi.ozon», «photo» …
    outcome: str  # ok · empty · blocked · limit · timeout · error · skipped · cached
    http: int | None = None
    ms: int | None = None
    retry_at: float | None = None  # unix time, когда источник снова можно спрашивать
    detail: str = ""
    fields: tuple[str, ...] = ()
    attempts: int = 1
    at: float = field(default_factory=time.time)


@contextmanager
def capture():
    lines: list[str] = []
    events: list[Event] = []
    token = _active.set(lines)
    token_events = _events.set(events)
    try:
        yield lines
    finally:
        _active.reset(token)
        _events.reset(token_events)


def current_events() -> list[Event] | None:
    return _events.get()


@contextmanager
def capture_events():
    """Только структурированные события, без текстовых строк. Можно вкладывать в capture()."""
    outer = _events.get()
    events: list[Event] = []
    token = _events.set(events)
    try:
        yield events
    finally:
        _events.reset(token)
        if outer is not None:
            outer.extend(events)


def step(label, result):
    lines = _active.get()
    if lines is not None:
        lines.append(f"{label} · {result}")


def event(source: str, outcome: str, **details) -> Event:
    record = Event(source, outcome, **details)
    events = _events.get()
    if events is not None:
        events.append(record)
    return record


class Timer:
    """with Timer() as t: …; t.ms — длительность в миллисекундах."""

    def __enter__(self):
        self.started = time.perf_counter()
        self.ms = 0
        return self

    def __exit__(self, *exc):
        self.ms = int((time.perf_counter() - self.started) * 1000)
        return False
