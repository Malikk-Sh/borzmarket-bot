"""Диагностика одного импорта. Параллельные запросы получают отдельные отчёты."""
from contextlib import contextmanager
from contextvars import ContextVar

_active = ContextVar("import_trace", default=None)


@contextmanager
def capture():
    lines = []
    token = _active.set(lines)
    try:
        yield lines
    finally:
        _active.reset(token)


def step(label, result):
    lines = _active.get()
    if lines is not None:
        lines.append(f"{label} · {result}")
