"""Память о источниках: паузы после ограничений и последние полученные поля товара.

Хранится в той же SQLite, что и черновики (таблицы source_state и quotes), поэтому
пауза WBCON и возраст последней цены переживают перезапуск бота. Без настроенной базы
(тесты, отдельный CLI без базы) всё живёт в памяти процесса.
"""
from __future__ import annotations

import json
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

# Цена из памяти годится для черновика только свежей: дальше она лишь справка с датой.
PRICE_FRESH = 600
# Название и ссылки на фото меняются редко: их можно подставить из прошлого импорта.
STABLE_FRESH = 30 * 86400
QUOTES_LIMIT = 3000

_FIELDS = ("title", "title_source", "image_urls", "price", "old_price", "price_note", "conditional_price",
           "conditional_note", "price_source", "wb_dest", "price_at", "price_unavailable")


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


class State:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path is not None else None
        self._blocks: dict[str, tuple[float, str]] = {}
        self._quotes: dict[str, tuple[float, dict]] = {}
        self.conn = None
        if self.path:
            self.conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS source_state (
                    source TEXT PRIMARY KEY,
                    blocked_until REAL NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS quotes (
                    key TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    fetched_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS quotes_time ON quotes (fetched_at);
            """)
            self.conn.execute("DELETE FROM quotes WHERE fetched_at < ?", (time.time() - STABLE_FRESH,))

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    # --- паузы источников ---------------------------------------------------------------
    def blocked(self, source: str, now: float | None = None) -> tuple[float, str] | None:
        now = time.time() if now is None else now
        if self.conn is not None:
            row = self.conn.execute("SELECT blocked_until, reason FROM source_state WHERE source = ?",
                                    (source,)).fetchone()
            value = (row[0], row[1]) if row else None
        else:
            value = self._blocks.get(source)
        if value and value[0] > now:
            return value
        return None

    def block(self, source: str, until: float, reason: str) -> None:
        current = self.blocked(source)
        if current and current[0] >= until:
            return
        if self.conn is not None:
            self.conn.execute("""
                INSERT INTO source_state (source, blocked_until, reason, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET blocked_until = excluded.blocked_until,
                    reason = excluded.reason, updated_at = excluded.updated_at
            """, (source, until, reason[:200], time.time()))
        else:
            self._blocks[source] = (until, reason[:200])

    def clear(self, source: str) -> None:
        if self.conn is not None:
            self.conn.execute("DELETE FROM source_state WHERE source = ?", (source,))
        else:
            self._blocks.pop(source, None)

    def blocks(self) -> dict[str, tuple[float, str]]:
        now = time.time()
        if self.conn is not None:
            rows = self.conn.execute("SELECT source, blocked_until, reason FROM source_state WHERE blocked_until > ?",
                                     (now,)).fetchall()
            return {source: (until, reason) for source, until, reason in rows}
        return {k: v for k, v in self._blocks.items() if v[0] > now}

    # --- последние поля товара ----------------------------------------------------------------
    def remember(self, key: str, product, fetched_at: float | None = None) -> None:
        """Сохраняет только поля, подтверждённые источником. Ручной ввод сюда не попадает."""
        data = {name: _plain(getattr(product, name, None)) for name in _FIELDS}
        data["image_urls"] = list(product.image_urls[:3])
        if not (data["title"] or data["price"] or data["image_urls"]):
            return
        previous = self.recall(key)
        if previous:
            # Не затираем название или цену пустым значением из неполного ответа.
            old = previous[1]
            for name in _FIELDS:
                if data.get(name) in (None, "", []) and old.get(name) not in (None, "", []):
                    data[name] = old[name]
            if product.price is None and old.get("price") is not None:
                for name in ("price", "old_price", "price_note", "conditional_price", "conditional_note",
                             "price_source", "price_at", "wb_dest"):
                    data[name] = old.get(name)
        stamp = time.time() if fetched_at is None else fetched_at
        if self.conn is not None:
            self.conn.execute("""
                INSERT INTO quotes (key, data, fetched_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at
            """, (key, json.dumps(data, ensure_ascii=False), stamp))
            count = self.conn.execute("SELECT count(*) FROM quotes").fetchone()[0]
            if count > QUOTES_LIMIT:
                self.conn.execute("""DELETE FROM quotes WHERE key IN (
                    SELECT key FROM quotes ORDER BY fetched_at LIMIT ?)""", (count - QUOTES_LIMIT,))
        else:
            self._quotes[key] = (stamp, data)

    def recall(self, key: str) -> tuple[float, dict] | None:
        if self.conn is not None:
            row = self.conn.execute("SELECT fetched_at, data FROM quotes WHERE key = ?", (key,)).fetchone()
            if not row:
                return None
            try:
                return row[0], json.loads(row[1])
            except ValueError:
                return None
        return self._quotes.get(key)


STATE = State()


def configure(path: str | Path | None) -> State:
    """Подключает базу бота. Возвращает новое состояние; старое закрывается."""
    global STATE
    previous = STATE
    STATE = State(path)
    previous.close()
    return STATE


def reset() -> None:
    """Для тестов: чистая память без базы."""
    configure(None)


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} с назад" if seconds >= 5 else "только что"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes} мин назад"
    hours = minutes // 60
    if hours < 36:
        return f"{hours} ч назад"
    return f"{hours // 24} дн. назад"


def wait_text(until: float, now: float | None = None) -> str:
    """«через 40 с» / «через 3 мин (14:32 МСК)»."""
    from datetime import datetime, timedelta, timezone
    now = time.time() if now is None else now
    left = max(1, int(until - now))
    moscow = datetime.fromtimestamp(until, timezone(timedelta(hours=3))).strftime("%H:%M")
    if left < 90:
        return f"через {left} с ({moscow} МСК)"
    return f"через {(left + 59) // 60} мин ({moscow} МСК)"
