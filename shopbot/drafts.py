"""Хранилище черновиков, фотографий и запланированных публикаций."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, fields as dataclass_fields
from decimal import Decimal
from pathlib import Path

from card import PROVISIONAL_TITLE, Product
from history import History, Receipt, Watch, destination_key

SEND_LEASE = 180
MAX_ITEMS = 6


@dataclass(frozen=True)
class DraftItem:
    product: Product
    photo: bytes | None = None
    photo_index: int | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        """Чего не хватает для публикации. Черновое название из адреса ссылки ждёт подтверждения."""
        return tuple(
            label for value, label in (
                (self.product.title.strip() and not self.product.provisional_title, "название"),
                (self.product.price is not None and self.product.price > 0, "цена"),
                (self.photo, "фото"),
            ) if not value
        )


@dataclass(frozen=True)
class Draft:
    id: str
    owner_id: int
    chat_id: int
    heading: str
    items: tuple[DraftItem, ...]
    state: str
    revision: int
    view_id: int | None
    due_at: int | None
    destination: int | str | None
    error: str
    comment: str = ""
    tags: tuple[str, ...] = ()
    erid: str = ""
    layout: str = "auto"

    @property
    def ready(self) -> bool:
        return all(not item.missing for item in self.items)


@dataclass(frozen=True)
class DraftSummary:
    id: str
    title: str
    state: str
    due_at: int | None
    count: int = 1  # товаров в записи
    ready: bool = False  # у каждого товара есть название, цена и фото
    markets: str = ""  # «wb», «ozon» или «wb+ozon»
    price: str = ""  # цена первого товара, если подборка из одного


def _encode(items: tuple[DraftItem, ...]) -> str:
    result = []
    for item in items:
        p = item.product
        result.append({
            "marketplace": p.marketplace, "title": p.title, "url": p.url,
            "price": str(p.price) if p.price is not None else None,
            "old_price": str(p.old_price) if p.old_price is not None else None,
            "price_note": p.price_note, "wb_dest": p.wb_dest,
            "rating": p.rating, "feedbacks": p.feedbacks,
            "price_unavailable": p.price_unavailable, "price_source": p.price_source,
            "conditional_price": str(p.conditional_price) if p.conditional_price is not None else None,
            "conditional_note": p.conditional_note,
            "image_urls": p.image_urls[:3],
            "photo_index": item.photo_index,
            # v10: происхождение полей и причины пропусков.
            "title_source": p.title_source, "photo_source": p.photo_source, "price_at": p.price_at,
            "reference_note": p.reference_note, "gaps": p.gaps, "alternatives": p.alternatives,
            "price_fetched_at": p.price_fetched_at, "price_origin": p.price_origin, "seller": p.seller,
            "variant": p.variant, "region": p.region,
        })
    return json.dumps(result, ensure_ascii=False)


_PRODUCT_FIELDS = {f.name for f in dataclass_fields(Product)} - {"trace", "import_note", "used_fallback"}


def _decode_product(fields: dict) -> Product:
    """Неизвестные ключи (из более новой версии) пропускаются, а не роняют чтение черновика."""
    for price in ("price", "old_price", "conditional_price"):
        if fields.get(price) is not None:
            fields[price] = Decimal(fields[price])
    for name in ("gaps", "alternatives"):
        if not isinstance(fields.get(name), dict):
            fields.pop(name, None)
    return Product(**{k: v for k, v in fields.items() if k in _PRODUCT_FIELDS})


class DraftStore:
    def __init__(self, path: str | Path) -> None:
        self.conn = sqlite3.connect(path, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA foreign_keys = ON;
            PRAGMA journal_mode = WAL;

            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                heading TEXT NOT NULL,
                items TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'draft'
                    CHECK (state IN ('draft', 'queued', 'sending', 'uncertain')),
                revision INTEGER NOT NULL DEFAULT 0,
                view_id INTEGER,
                due_at INTEGER,
                destination TEXT,
                sending_at INTEGER,
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS photos (
                draft_id TEXT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
                item_number INTEGER NOT NULL,
                data BLOB NOT NULL,
                PRIMARY KEY (draft_id, item_number)
            );

            CREATE INDEX IF NOT EXISTS drafts_owner
                ON drafts (owner_id, chat_id, updated_at DESC);
            CREATE INDEX IF NOT EXISTS drafts_due
                ON drafts (state, due_at);
        """)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(drafts)")}
            for column, default in (("comment", ""), ("tags", "[]"), ("erid", ""), ("layout", "auto")):
                if column not in columns:
                    self.conn.execute(f"ALTER TABLE drafts ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        self.history = History(self.conn)

    def close(self) -> None:
        self.conn.close()

    def create(self, owner_id: int, chat_id: int, items: tuple[DraftItem, ...]) -> Draft:
        with self.conn:
            return self._insert_draft(owner_id, chat_id, items)

    def _insert_draft(self, owner_id: int, chat_id: int, items: tuple[DraftItem, ...]) -> Draft:
        if not 1 <= len(items) <= MAX_ITEMS:
            raise ValueError("A draft must include between one and six items")

        key = uuid.uuid4().hex[:16]
        heading = "Подборка BorzMarket" if len(items) > 1 else ""
        self.conn.execute(
            "INSERT INTO drafts (id, owner_id, chat_id, heading, items, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, owner_id, chat_id, heading, _encode(items), int(time.time())),
        )
        self._save_photos(key, items)
        return Draft(key, owner_id, chat_id, heading, items, "draft", 0, None, None, None, "")

    def _save_photos(self, key: str, items: tuple[DraftItem, ...]) -> None:
        self.conn.executemany(
            "INSERT INTO photos (draft_id, item_number, data) VALUES (?, ?, ?)",
            [(key, index, item.photo) for index, item in enumerate(items) if item.photo],
        )

    def price_alert(self, watch: Watch, item: DraftItem) -> Draft | None:
        with self.conn:
            if item.product.price is None or not self.history.mark_alert(watch, item.product.price):
                return None
            return self._insert_draft(watch.owner_id, watch.chat_id, (item,))

    def get(self, key: str) -> Draft | None:
        rows = self.conn.execute("""
            SELECT d.*, p.item_number, p.data
            FROM drafts AS d LEFT JOIN photos AS p ON d.id = p.draft_id
            WHERE d.id = ?
        """, (key,)).fetchall()
        if not rows:
            return None

        row = rows[0]
        photos = {r["item_number"]: r["data"] for r in rows}
        items = []
        for index, fields in enumerate(json.loads(row["items"])):
            photo_index = fields.pop("photo_index", None)
            items.append(DraftItem(_decode_product(fields), photos.get(index), photo_index))

        destination = row["destination"]
        if destination and destination.lstrip("-").isdigit():
            destination = int(destination)
        return Draft(
            row["id"], row["owner_id"], row["chat_id"], row["heading"], tuple(items),
            row["state"], row["revision"], row["view_id"], row["due_at"], destination, row["error"],
            row["comment"], tuple(json.loads(row["tags"])), row["erid"], row["layout"],
        )

    def counts(self, owner_id: int, chat_id: int) -> tuple[int, int, int | None]:
        """Сколько черновиков, сколько в очереди и время ближайшей публикации."""
        row = self.conn.execute("""
            SELECT coalesce(sum(state IN ('draft', 'uncertain')), 0) AS drafts,
                   coalesce(sum(state IN ('queued', 'sending')), 0) AS queued,
                   min(CASE WHEN state = 'queued' THEN due_at END) AS next_due
            FROM drafts WHERE owner_id = ? AND chat_id = ?
        """, (owner_id, chat_id)).fetchone()
        return int(row["drafts"]), int(row["queued"]), row["next_due"]

    def page(
        self, owner_id: int, chat_id: int, queue: bool, offset: int, limit: int
    ) -> list[DraftSummary]:
        states = ("queued", "sending") if queue else ("draft", "uncertain")
        order = "due_at, rowid" if queue else "updated_at DESC, rowid DESC"
        rows = self.conn.execute(f"""
            SELECT d.id, d.heading, d.items, d.state, d.due_at,
                   (SELECT group_concat(item_number) FROM photos AS p WHERE p.draft_id = d.id) AS photo_numbers
            FROM drafts AS d
            WHERE d.owner_id = ? AND d.chat_id = ? AND d.state IN (?, ?)
            ORDER BY {order}
            LIMIT ? OFFSET ?
        """, (owner_id, chat_id, *states, limit, offset)).fetchall()
        result = []
        for row in rows:
            items = json.loads(row["items"])
            photos = {int(n) for n in (row["photo_numbers"] or "").split(",") if n}
            ready = all(
                (item.get("title") or "").strip() and item.get("title_source") != PROVISIONAL_TITLE
                and Decimal(item["price"] or 0) > 0 and index in photos
                for index, item in enumerate(items)
            )
            markets = "+".join(sorted({item["marketplace"] for item in items}))
            title = row["heading"] or items[0]["title"] or "Товар"
            price = items[0].get("price") if len(items) == 1 else None
            result.append(DraftSummary(
                row["id"], title, row["state"], row["due_at"], len(items), ready, markets,
                str(Decimal(price)) if price else "",
            ))
        return result

    def bind_view(self, draft: Draft, message_id: int) -> bool:
        with self.conn:
            changed = self.conn.execute("""
                UPDATE drafts SET view_id = ? WHERE id = ? AND revision = ?
                AND (view_id IS NULL OR view_id < ?)
            """, (message_id, draft.id, draft.revision, message_id)).rowcount
        return bool(changed)

    def update(
        self, draft: Draft, heading: str, items: tuple[DraftItem, ...], *,
        comment: str | None = None, tags: tuple[str, ...] | None = None, erid: str | None = None,
        layout: str | None = None,
    ) -> bool:
        if not 1 <= len(items) <= MAX_ITEMS or layout not in (None, "auto", "alternate"):
            return False
        with self.conn:
            changed = self.conn.execute("""
                UPDATE drafts SET heading = ?, items = ?, comment = ?, tags = ?, erid = ?, layout = ?, revision = revision + 1,
                    view_id = NULL, error = '', updated_at = ?
                WHERE id = ? AND revision = ? AND state = 'draft'
            """, (
                heading, _encode(items), draft.comment if comment is None else comment,
                json.dumps(draft.tags if tags is None else tags, ensure_ascii=False),
                draft.erid if erid is None else erid, draft.layout if layout is None else layout,
                int(time.time()), draft.id, draft.revision,
            )).rowcount
            if changed and tuple(item.photo for item in draft.items) != tuple(item.photo for item in items):
                self.conn.execute("DELETE FROM photos WHERE draft_id = ?", (draft.id,))
                self._save_photos(draft.id, items)
        return bool(changed)

    def schedule(self, draft: Draft, run_at: int, destination: int | str) -> bool:
        now = int(time.time())
        if not draft.ready or run_at <= now:
            return False
        with self.conn:
            return self._schedule(draft, run_at, destination, now)

    def _schedule(self, draft: Draft, run_at: int, destination: int | str, now: int) -> bool:
        changed = self.conn.execute("""
            UPDATE drafts SET state = 'queued', due_at = ?, destination = ?, error = '',
                revision = revision + 1, view_id = NULL, updated_at = ?
            WHERE id = ? AND revision = ? AND state IN ('draft', 'queued')
        """, (run_at, destination_key(destination), now, draft.id, draft.revision)).rowcount
        return bool(changed)

    def free_slot(self, draft: Draft, destination: int | str, candidates: Iterable[int]) -> int | None:
        """Ближайший свободный слот без записи: для подписи кнопки под черновиком."""
        now = int(time.time())
        occupied = {row["due_at"] // 60 for row in self.conn.execute("""
            SELECT due_at FROM drafts WHERE state IN ('queued', 'sending')
            AND lower(destination) = ? AND due_at > ? AND id != ?
        """, (destination_key(destination), now, draft.id))}
        for stamp in candidates:
            if stamp > now and stamp // 60 not in occupied:
                return stamp
        return None

    def schedule_slot(self, draft: Draft, destination: int | str, candidates: Iterable[int]) -> int | None:
        now = int(time.time())
        if not draft.ready:
            return None
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            occupied = {row["due_at"] // 60 for row in self.conn.execute("""
                SELECT due_at FROM drafts WHERE state IN ('queued', 'sending')
                AND lower(destination) = ? AND due_at > ? AND id != ?
            """, (destination_key(destination), now, draft.id))}
            slot = next((stamp for stamp in candidates if stamp > now and stamp // 60 not in occupied), None)
            if slot is not None and self._schedule(draft, slot, destination, now):
                return slot
        return None

    def unschedule(self, draft: Draft) -> bool:
        with self.conn:
            changed = self.conn.execute("""
                UPDATE drafts SET state = 'draft', due_at = NULL, destination = NULL,
                    revision = revision + 1, view_id = NULL, updated_at = ?
                WHERE id = ? AND revision = ? AND state = 'queued'
            """, (int(time.time()), draft.id, draft.revision)).rowcount
        return bool(changed)

    def drop(self, draft: Draft) -> bool:
        with self.conn:
            changed = self.conn.execute("""
                DELETE FROM drafts WHERE id = ? AND revision = ? AND state IN ('draft', 'uncertain')
            """, (draft.id, draft.revision)).rowcount
        return bool(changed)

    def next_due(self, now: int) -> Draft | None:
        row = self.conn.execute("""
            SELECT id FROM drafts WHERE state = 'queued' AND due_at <= ? ORDER BY due_at, rowid LIMIT 1
        """, (now,)).fetchone()
        if row:
            draft = self.get(row["id"])
            if draft and draft.state == "queued" and draft.due_at is not None and draft.due_at <= now:
                return draft
        return None

    def claim(
        self, draft: Draft, destination: int | str, now: int, *,
        urls: tuple[str, ...] | None = None, tracked: bool = False, avoid_repeat: bool = False,
    ) -> bool:
        if not draft.ready:
            return False
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if self.conn.execute("SELECT 1 FROM published WHERE id = ?", (draft.id,)).fetchone():
                return False  # запись с этим ID уже в истории (перенос между версиями): второй отправки не будет
            if avoid_repeat:
                if any(self.history.known_product(item.product, destination) for item in draft.items):
                    return False
            changed = self.conn.execute("""
                UPDATE drafts SET state = 'sending', destination = ?, sending_at = ?, error = '',
                    revision = revision + 1, view_id = NULL, updated_at = ?
                WHERE id = ? AND revision = ? AND state IN ('draft', 'queued')
            """, (destination_key(destination), now, now, draft.id, draft.revision)).rowcount
            if changed:
                self.history.reserve(
                    draft, destination, now, urls or tuple(item.product.url for item in draft.items), tracked,
                )
        return bool(changed)

    def finish(
        self, draft: Draft, outcome: str, error: str = "", retry_at: int | None = None,
        receipt: Receipt | None = None,
    ) -> None:
        # draft — снимок до захвата; только тот же захват может завершить отправку.
        owner = (draft.id, draft.revision + 1)
        with self.conn:
            if outcome == "sent":
                if receipt is None:
                    raise ValueError("Confirmed publication requires a Telegram receipt")
                changed = self.conn.execute("""
                    DELETE FROM drafts WHERE id = ? AND revision = ? AND state = 'sending'
                """, owner).rowcount
                if changed:
                    self.history.confirm(draft, receipt)
            else:
                changed = self.conn.execute("""
                    UPDATE drafts SET state = ?, error = ?, due_at = ?, sending_at = NULL,
                        revision = revision + 1, view_id = NULL, updated_at = ?
                    WHERE id = ? AND revision = ? AND state = 'sending'
                """, (outcome, error, retry_at, int(time.time()), *owner)).rowcount
                if changed:
                    self.history.stop_attempt(draft.id, outcome == "uncertain")

    def retire_published(self) -> list[str]:
        """Снимает черновики, чей ID уже опубликован (в истории status = 'sent').

        Обычная отправка удаляет черновик сама, поэтому такое бывает только после переноса
        базы между версиями. Вызывается при запуске до обработчика очереди.
        """
        with self.conn:
            rows = [row["id"] for row in self.conn.execute("""
                SELECT d.id FROM drafts AS d JOIN published AS p ON p.id = d.id
                WHERE p.status = 'sent' AND d.state IN ('draft', 'queued')
            """)]
            for key in rows:
                self.conn.execute("DELETE FROM drafts WHERE id = ?", (key,))
        return rows

    def recover_expired(self, now: int) -> None:
        with self.conn:
            self.conn.execute("""
                UPDATE published SET status = 'uncertain' WHERE status = 'sending' AND id IN (
                    SELECT id FROM drafts WHERE state = 'sending' AND sending_at <= ?
                )
            """, (now - SEND_LEASE,))
            self.conn.execute("""
                UPDATE drafts SET state = 'uncertain', revision = revision + 1, view_id = NULL,
                    error = 'Предыдущая отправка прервана. Проверьте канал.', updated_at = ?
                WHERE state = 'sending' AND sending_at <= ?
            """, (now, now - SEND_LEASE))
