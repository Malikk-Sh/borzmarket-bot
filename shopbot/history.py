"""История публикаций, счётчики и задачи проверки цен."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

import httpx

from card import Product

if TYPE_CHECKING:
    from drafts import Draft

CHANNEL_MATCH = "(p.destination = ? OR CAST(p.channel_id AS TEXT) = ? OR (p.username != '' AND p.username = ?))"
DAY = 86400


@dataclass(frozen=True)
class Receipt:
    channel_id: int
    username: str
    message_id: int
    url: str
    sent_at: int


@dataclass(frozen=True)
class Watch:
    key: str
    version: int
    owner_id: int
    chat_id: int
    product: Product
    reference: Decimal
    photo: bytes | None
    post_url: str
    source: str = ""  # откуда взята опорная цена: витрина, резерв или ручной ввод


MANUAL_SOURCES = {"manual", "message", "screenshot", "memory"}
FALLBACK_SOURCES = {"wbcon", "bhapi"}


def source_class(source: str) -> str:
    """Сопоставимы только цены одного класса: витрина с витриной, резерв с резервом.

    Пустой источник — наблюдение из v9.1 и раньше: сравнивается как витрина, как и прежде.
    """
    if source in FALLBACK_SOURCES:
        return "fallback"
    if source in MANUAL_SOURCES:
        return "manual"
    return "market"


def destination_key(value: int | str | None) -> str:
    return str(value).lower() if value is not None else ""


def product_key(product: Product, variant: bool = False) -> str:
    url = httpx.URL(product.url)
    if product.marketplace == "wb":
        found = re.search(r"/catalog/([0-9]+)(?:/|$)", url.path)
    else:
        found = re.search(r"/product/(?:[^/]+-)?([0-9]+)/?$", url.path)

    suffix = str(int(found.group(1))) if found else f"{url.host.lower()}{url.path.rstrip('/')}"
    if variant and product.marketplace == "wb" and (size := url.params.get("size")):
        suffix += f"/{size}"
    return f"{product.marketplace}:{suffix}"


def click_token(post_id: str, index: int) -> str:
    return f"{post_id}-{index + 1}"


class History:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS published (
                id TEXT PRIMARY KEY,
                destination TEXT NOT NULL,
                channel_id INTEGER,
                username TEXT NOT NULL DEFAULT '',
                message_id INTEGER,
                post_url TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'uncertain')),
                sent_at INTEGER NOT NULL,
                title TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                views INTEGER
            );
            CREATE INDEX IF NOT EXISTS published_time ON published (status, sent_at DESC);

            CREATE TABLE IF NOT EXISTS published_items (
                post_id TEXT NOT NULL REFERENCES published(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                sku TEXT NOT NULL,
                marketplace TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                outbound_url TEXT NOT NULL,
                price TEXT NOT NULL,
                old_price TEXT,
                price_note TEXT NOT NULL,
                token TEXT UNIQUE,
                clicks INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (post_id, position)
            );
            CREATE INDEX IF NOT EXISTS published_sku ON published_items (sku);

            CREATE TABLE IF NOT EXISTS reactions (
                post_id TEXT NOT NULL REFERENCES published(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL,
                PRIMARY KEY (post_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS fetch_events (
                checked_at INTEGER NOT NULL,
                marketplace TEXT NOT NULL,
                result TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS fetch_time ON fetch_events (checked_at);

            CREATE TABLE IF NOT EXISTS price_watch (
                key TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                marketplace TEXT NOT NULL,
                title TEXT NOT NULL,
                url TEXT NOT NULL,
                price_note TEXT NOT NULL,
                reference TEXT NOT NULL,
                last_notice TEXT,
                photo BLOB,
                post_url TEXT NOT NULL,
                check_at INTEGER NOT NULL,
                version INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS prices_due ON price_watch (check_at);
        """)

        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(price_watch)")}
        if "wb_dest" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE price_watch ADD COLUMN wb_dest TEXT NOT NULL DEFAULT ''")
        if "price_source" not in columns:
            with self.conn:
                self.conn.execute("ALTER TABLE price_watch ADD COLUMN price_source TEXT NOT NULL DEFAULT ''")

    def reserve(
        self, draft: Draft, destination: int | str, at: int,
        urls: tuple[str, ...], tracked: bool,
    ) -> None:
        self.conn.execute("""
            INSERT INTO published (id, destination, status, sent_at, title, item_count)
            VALUES (?, ?, 'sending', ?, ?, ?)
        """, (
            draft.id, destination_key(destination), at,
            draft.heading or draft.items[0].product.title, len(draft.items),
        ))
        rows = []
        for index, (item, url) in enumerate(zip(draft.items, urls)):
            product = item.product
            rows.append((
                draft.id, index, product_key(product), product.marketplace, product.title,
                product.url, url, str(product.price),
                str(product.old_price) if product.old_price is not None else None,
                product.price_note, click_token(draft.id, index) if tracked else None,
            ))
        self.conn.executemany("""
            INSERT INTO published_items (
                post_id, position, sku, marketplace, title, url, outbound_url,
                price, old_price, price_note, token
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

    def confirm(self, draft: Draft, receipt: Receipt) -> None:
        self.conn.execute("""
            UPDATE published SET status = 'sent', sent_at = ?, channel_id = ?, username = ?,
                message_id = ?, post_url = ? WHERE id = ? AND status = 'sending'
        """, (
            receipt.sent_at, receipt.channel_id, receipt.username,
            receipt.message_id, receipt.url, draft.id,
        ))
        for item in draft.items:
            product = item.product
            key = f"{receipt.channel_id}|{product_key(product, variant=True)}|{product.price_note.casefold()}"
            self.conn.execute("""
                INSERT INTO price_watch (
                    key, owner_id, chat_id, marketplace, title, url, price_note,
                    reference, photo, post_url, check_at, wb_dest, price_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    owner_id = excluded.owner_id, chat_id = excluded.chat_id, title = excluded.title,
                    url = excluded.url, price_note = excluded.price_note, reference = excluded.reference,
                    last_notice = NULL, photo = excluded.photo, post_url = excluded.post_url,
                    check_at = excluded.check_at, wb_dest = excluded.wb_dest,
                    price_source = excluded.price_source, version = version + 1
            """, (
                key, draft.owner_id, draft.chat_id, product.marketplace, product.title,
                product.url, product.price_note, str(product.price), item.photo, receipt.url,
                receipt.sent_at + DAY, product.wb_dest, product.price_source,
            ))

    def stop_attempt(self, key: str, uncertain: bool) -> None:
        if uncertain:
            self.conn.execute("UPDATE published SET status = 'uncertain' WHERE id = ? AND status = 'sending'", (key,))
        else:
            self.conn.execute("DELETE FROM published WHERE id = ? AND status = 'sending'", (key,))

    def previous(self, product: Product, destination: int | str | None) -> sqlite3.Row | None:
        dest = destination_key(destination)
        return self.conn.execute(f"""
            SELECT i.title, i.price, i.price_note, p.sent_at, p.post_url
            FROM published_items AS i JOIN published AS p ON p.id = i.post_id
            WHERE i.sku = ? AND p.status = 'sent' AND {CHANNEL_MATCH}
            ORDER BY p.sent_at DESC, p.rowid DESC, i.position LIMIT 1
        """, (product_key(product), dest, dest, dest)).fetchone()

    def known_product(self, product: Product, destination: int | str) -> bool:
        dest = destination_key(destination)
        return self.conn.execute(f"""
            SELECT 1 FROM published_items AS i JOIN published AS p ON p.id = i.post_id
            WHERE i.sku = ? AND {CHANNEL_MATCH} LIMIT 1
        """, (product_key(product), dest, dest, dest)).fetchone() is not None

    def recent(self, destination: int | str | None, limit: int = 10) -> list[sqlite3.Row]:
        dest = destination_key(destination)
        return self.conn.execute(f"""
            SELECT p.* FROM published AS p WHERE p.status = 'sent' AND {CHANNEL_MATCH}
            ORDER BY sent_at DESC, rowid DESC LIMIT ?
        """, (dest, dest, dest, limit)).fetchall()

    def set_views(
        self, destination: int | str | None, message: int | str, views: int
    ) -> bool:
        dest = destination_key(destination)
        field = "p.message_id" if isinstance(message, int) else "p.post_url"
        with self.conn:
            changed = self.conn.execute(f"""
                UPDATE published SET views = ? WHERE id IN (
                    SELECT p.id FROM published AS p WHERE p.status = 'sent'
                    AND {field} = ? AND {CHANNEL_MATCH}
                )
            """, (views, message, dest, dest, dest)).rowcount
        return bool(changed)

    def statistics(
        self, destination: int | str | None, since: int
    ) -> tuple[list[sqlite3.Row], list[sqlite3.Row], list[sqlite3.Row]]:
        dest = destination_key(destination)
        params = (since, dest, dest, dest)
        selected = f"p.status = 'sent' AND p.sent_at >= ? AND {CHANNEL_MATCH}"
        posts = self.conn.execute(f"""
            SELECT p.*, (SELECT count(*) FROM reactions AS r WHERE r.post_id = p.id) AS likes
            FROM published AS p WHERE {selected}
        """, params).fetchall()
        items = self.conn.execute(f"""
            SELECT i.* FROM published_items AS i JOIN published AS p ON p.id = i.post_id
            WHERE {selected}
        """, params).fetchall()
        fetches = self.conn.execute("""
            SELECT marketplace, result, count(*) AS total FROM fetch_events
            WHERE checked_at >= ? GROUP BY marketplace, result
        """, (since,)).fetchall()
        return posts, items, fetches

    def record_fetches(self, at: int, results: list[tuple[str, str]]) -> None:
        with self.conn:
            self.conn.executemany("INSERT INTO fetch_events VALUES (?, ?, ?)", [
                (at, market, result) for market, result in results
            ])

    def click_destination(self, token: str) -> sqlite3.Row | None:
        return self.conn.execute("""
            SELECT marketplace, outbound_url FROM published_items WHERE token = ?
        """, (token,)).fetchone()

    def count_click(self, token: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE published_items SET clicks = clicks + 1 WHERE token = ?", (token,)
            )

    def react(self, key: str, channel_id: int, message_id: int, user_id: int) -> tuple[int, bool] | None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            post = self.conn.execute("""
                SELECT id FROM published WHERE id = ? AND status = 'sent'
                AND channel_id = ? AND message_id = ?
            """, (key, channel_id, message_id)).fetchone()
            if post is None:
                return None

            removed = self.conn.execute(
                "DELETE FROM reactions WHERE post_id = ? AND user_id = ?", (key, user_id)
            ).rowcount
            if not removed:
                self.conn.execute("INSERT INTO reactions VALUES (?, ?)", (key, user_id))
            total = self.conn.execute(
                "SELECT count(*) FROM reactions WHERE post_id = ?", (key,)
            ).fetchone()[0]
        return total, not bool(removed)

    def claim_watch(self, now: int) -> Watch | None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute("""
                SELECT * FROM price_watch WHERE check_at <= ? ORDER BY check_at, key LIMIT 1
            """, (now,)).fetchone()
            if row is None:
                return None
            self.conn.execute("""
                UPDATE price_watch SET check_at = ?, version = version + 1 WHERE key = ?
            """, (now + DAY, row["key"]))
        reference = min(Decimal(row["reference"]), Decimal(row["last_notice"] or row["reference"]))
        product = Product(row["marketplace"], row["title"], row["url"], price_note=row["price_note"], wb_dest=row["wb_dest"])
        return Watch(
            row["key"], row["version"] + 1, row["owner_id"], row["chat_id"],
            product, reference, row["photo"], row["post_url"], row["price_source"],
        )

    def rebase(self, watch, current) -> None:
        """Ручная опорная цена заменяется первой ценой витрины без уведомления."""
        with self.conn:
            self.conn.execute("""
                UPDATE price_watch SET reference = ?, last_notice = NULL, price_source = ?, wb_dest = ?,
                    version = version + 1 WHERE key = ? AND version = ?
            """, (str(current.price), current.price_source, current.wb_dest, watch.key, watch.version))

    def reset_wb_reference(self, watch, current):
        with self.conn:
            self.conn.execute("""
                UPDATE price_watch SET wb_dest = ?, reference = ?, last_notice = NULL, version = version+1
                WHERE key = ? AND version = ?
            """, (current.wb_dest, str(current.price), watch.key, watch.version))

    def mark_alert(self, watch: Watch, price: Decimal | int) -> bool:
        changed = self.conn.execute("""
            UPDATE price_watch SET last_notice = ?, version = version + 1 WHERE key = ? AND version = ?
        """, (str(price), watch.key, watch.version)).rowcount
        return bool(changed)
