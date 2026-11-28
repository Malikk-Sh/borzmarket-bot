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
HISTORY_KEEP = 90 * 86400  # журнал наблюдений цен: 90 дней

_FIELDS = ("title", "title_source", "image_urls", "price", "old_price", "price_note", "conditional_price",
           "conditional_note", "price_source", "wb_dest", "price_at", "price_unavailable", "seller", "variant", "region")
# Поля одного ценового предложения: заменяются только вместе.
PRICE_GROUP = ("price", "old_price", "price_note", "conditional_price", "conditional_note", "price_source",
               "price_at", "wb_dest", "price_unavailable", "seller", "variant", "region")


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    return value


class State:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path) if path is not None else None
        self._blocks: dict[str, tuple[float, str]] = {}
        self._quotes: dict[str, tuple[float, dict]] = {}
        self._observations: list[dict] = []
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
                CREATE TABLE IF NOT EXISTS price_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT NOT NULL,
                    market TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    variant TEXT NOT NULL DEFAULT '',
                    seller TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    payment TEXT NOT NULL,
                    source TEXT NOT NULL,
                    amount TEXT,
                    observed_at REAL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    checks INTEGER NOT NULL DEFAULT 1
                );
                CREATE INDEX IF NOT EXISTS observations_key ON price_observations (key, source, payment, last_seen);
            """)
            self.conn.execute("DELETE FROM quotes WHERE fetched_at < ?", (time.time() - STABLE_FRESH,))
            self.conn.execute("DELETE FROM price_observations WHERE last_seen < ?", (time.time() - HISTORY_KEEP,))

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
        """Сохраняет только поля, подтверждённые источником. Ручной ввод сюда не попадает.

        Цена хранится целым предложением: сумма, зачёркнутая, условная цена, подпись, источник,
        регион и время. Новое предложение заменяет прежнее полностью, а не по одному полю.
        Явная распродажа стирает сохранённую цену. Ответ без цены и без признака распродажи
        оставляет прежнее состояние — в том числе «нет в наличии» — и цену не воскрешает.
        """
        data = {name: _plain(getattr(product, name, None)) for name in _FIELDS}
        data["image_urls"] = list(product.image_urls[:3])
        if not (data["title"] or data["price"] or data["image_urls"] or product.price_unavailable):
            return
        previous = self.recall(key)
        if previous:
            old = previous[1]
            for name in ("title", "title_source", "image_urls"):
                if data.get(name) in (None, "", []) and old.get(name) not in (None, "", []):
                    data[name] = old[name]
            if product.price is None and not product.price_unavailable:
                for name in PRICE_GROUP:
                    data[name] = old.get(name)
        if product.price_unavailable:
            for name in PRICE_GROUP:
                data[name] = None
            data["price_unavailable"] = True
            data["price_source"] = product.price_source or None
            data["price_at"] = int(time.time() if fetched_at is None else fetched_at)
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

    # --- журнал наблюдений цен ---------------------------------------------------------------
    def observe(self, key: str, row: dict, now: float | None = None) -> str:
        """Записывает наблюдение. Та же сумма от того же источника продлевает прошлую запись.

        Возвращает «new» или «same». Смена суммы между проверками видна как новая строка:
        так оценивается фактическая задержка обновления.
        """
        now = time.time() if now is None else now
        keys = ("market", "sku", "variant", "seller", "region", "payment", "source", "amount", "observed_at")
        row = {name: row.get(name) for name in keys}
        last = self.last_observation(key, row["source"], row["payment"])
        if last and last["amount"] == row["amount"] and last["variant"] == (row["variant"] or "") \
                and last["region"] == (row["region"] or ""):
            if self.conn is not None:
                self.conn.execute("""UPDATE price_observations SET last_seen = ?, checks = checks + 1,
                                     observed_at = coalesce(?, observed_at) WHERE id = ?""",
                                  (now, row["observed_at"], last["id"]))
            else:
                last.update(last_seen=now, checks=last["checks"] + 1)
            return "same"
        record = dict(row, key=key, variant=row["variant"] or "", seller=row["seller"] or "",
                      region=row["region"] or "", first_seen=now, last_seen=now, checks=1)
        if self.conn is not None:
            self.conn.execute("""INSERT INTO price_observations (key, market, sku, variant, seller, region, payment,
                                 source, amount, observed_at, first_seen, last_seen, checks)
                                 VALUES (:key, :market, :sku, :variant, :seller, :region, :payment, :source, :amount,
                                 :observed_at, :first_seen, :last_seen, 1)""", record)
        else:
            record["id"] = len(self._observations) + 1
            self._observations.append(record)
        return "new"

    def last_observation(self, key: str, source: str, payment: str = "regular") -> dict | None:
        if self.conn is not None:
            self.conn.row_factory = sqlite3.Row
            row = self.conn.execute("""SELECT * FROM price_observations WHERE key = ? AND source = ? AND payment = ?
                                       ORDER BY last_seen DESC, id DESC LIMIT 1""", (key, source, payment)).fetchone()
            self.conn.row_factory = None
            return dict(row) if row else None
        rows = [r for r in self._observations if r["key"] == key and r["source"] == source and r["payment"] == payment]
        return max(rows, key=lambda r: (r["last_seen"], r["id"])) if rows else None

    def observations(self, key: str) -> list[dict]:
        if self.conn is not None:
            self.conn.row_factory = sqlite3.Row
            rows = [dict(r) for r in self.conn.execute(
                "SELECT * FROM price_observations WHERE key = ? ORDER BY first_seen, id", (key,))]
            self.conn.row_factory = None
            return rows
        return [r for r in self._observations if r["key"] == key]

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
