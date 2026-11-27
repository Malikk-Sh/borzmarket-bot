"""Сверка очереди двух установок BorzMarket, чтобы пост не ушёл в канал второй раз.

Нужна при откате (старая папка + новая база) и при повторном обновлении после отката.
«Источник» — база версии, которая работала последней; «цель» — база версии, которую
сейчас запустят. Источник открывается только на чтение.

Правила для записей, стоящих в очереди цели (queued, sending, uncertain):
  • источник уже опубликовал запись (published.status = 'sent') → запись убирается из очереди
    цели, а публикация копируется в её историю (подсказки о повторах и статистика);
  • источник не получил подтверждение Telegram → запись в цели становится «неизвестно»
    (uncertain): бот её не отправит, человек проверит канал;
  • в источнике запись в черновиках → в цели тоже черновик; в очереди с другим временем → время
    берётся из источника;
  • источник о записи не знает → по выбору: в черновики (откат: значит, её удалили) или без изменений.
Перед записью делается копия базы цели через SQLite Backup.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

MOSCOW = timezone(timedelta(hours=3))
NOTE = "Сверка версий: запись снята с очереди. Проверьте её перед отправкой."
UNCERTAIN = "Другая версия бота не получила подтверждение Telegram. Проверьте канал."


def _title(row) -> str:
    if row["heading"]:
        return row["heading"]
    try:
        items = json.loads(row["items"])
        return (items[0].get("title") or "товар")[:60]
    except (ValueError, IndexError, AttributeError, TypeError):
        return "запись"


def _when(stamp) -> str:
    return datetime.fromtimestamp(stamp, MOSCOW).strftime("%d.%m %H:%M") if stamp else "—"


def _columns(conn, table) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _has(conn, table) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def backup(path: Path) -> Path:
    stamp = datetime.now(MOSCOW).strftime("%Y%m%d-%H%M%S")
    copy = path.with_name(f"{path.name}.before-sync-{stamp}")
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(copy)) as target:
        source.backup(target)
    return copy


def reconcile(target: Path, authority: Path, *, unknown: str = "keep", apply: bool = False) -> list[str]:
    """Возвращает план по-русски. С apply=True выполняет его одной транзакцией."""
    if not (target.is_file() and authority.is_file()):
        return ["Сверка не нужна: одной из баз нет."]
    plan: list[str] = []
    actions: list[tuple] = []
    with closing(sqlite3.connect(authority.resolve().as_uri() + "?mode=ro", uri=True)) as auth, \
            closing(sqlite3.connect(target)) as tgt:
        auth.row_factory = tgt.row_factory = sqlite3.Row
        if not all(_has(c, t) for c in (auth, tgt) for t in ("drafts", "published")):
            return ["Сверка не нужна: в одной из баз нет очереди или истории."]
        published = {row["id"]: row for row in auth.execute("SELECT * FROM published")}
        drafts = {row["id"]: row for row in auth.execute("SELECT id, state, due_at FROM drafts")}
        known_posts = {row[0] for row in tgt.execute("SELECT id FROM published")}
        queued = tgt.execute("""SELECT id, heading, items, state, due_at FROM drafts
                                WHERE state IN ('queued', 'sending', 'uncertain') ORDER BY due_at""").fetchall()
        copied = set()
        for row in queued:
            key, title = row["id"], _title(row)
            post = published.get(key)
            if post is not None and post["status"] == "sent":
                link = f" · {post['post_url']}" if post["post_url"] else ""
                plan.append(f"🗑 «{title}» — уже опубликовано другой версией {_when(post['sent_at'])} МСК{link}. "
                            "Убираю из очереди.")
                actions.append(("delete", key))
                if key not in known_posts:
                    actions.append(("copy", key))
                    copied.add(key)
            elif post is not None:
                plan.append(f"⚠️ «{title}» — другая версия не дождалась подтверждения Telegram. "
                            "Отмечаю «неизвестно», проверьте канал.")
                actions.append(("uncertain", key))
            elif key in drafts:
                other = drafts[key]
                if other["state"] == "queued" and other["due_at"] != row["due_at"] and row["state"] == "queued":
                    plan.append(f"🕓 «{title}» — время из другой версии: {_when(other['due_at'])} МСК.")
                    actions.append(("due", key, other["due_at"]))
                elif other["state"] in ("draft", "uncertain") and row["state"] == "queued":
                    plan.append(f"↩️ «{title}» — в другой версии не в очереди. Возвращаю в черновики.")
                    actions.append(("draft", key))
            elif row["state"] == "queued":
                if unknown == "draft":
                    plan.append(f"↩️ «{title}» ({_when(row['due_at'])} МСК) — в другой версии записи нет "
                                "(удалена или отправлена вручную). Возвращаю в черновики.")
                    actions.append(("draft", key))
                else:
                    plan.append(f"ℹ️ «{title}» ({_when(row['due_at'])} МСК) — есть только здесь, остаётся в очереди.")
        extra = [key for key, post in published.items()
                 if post["status"] == "sent" and key not in known_posts and key not in copied]
        for key in extra:
            actions.append(("copy", key))
        if extra:
            plan.append(f"📚 В историю добавлю публикаций другой версии: {len(extra)}.")
        if not plan:
            plan.append("✅ Очереди совпадают, менять нечего.")
        if apply and actions:
            _apply(tgt, auth, actions)
    return plan


def _apply(tgt, auth, actions) -> None:
    tgt.execute("PRAGMA foreign_keys = ON")
    post_cols = [c for c in _columns(auth, "published") if c in set(_columns(tgt, "published"))]
    item_cols = []
    if _has(auth, "published_items") and _has(tgt, "published_items"):
        item_cols = [c for c in _columns(auth, "published_items") if c in set(_columns(tgt, "published_items"))]
    now = int(time.time())
    with tgt:
        tgt.execute("BEGIN IMMEDIATE")
        for action in actions:
            kind, key = action[0], action[1]
            if kind == "delete":
                tgt.execute("DELETE FROM drafts WHERE id = ?", (key,))
                tgt.execute("DELETE FROM published WHERE id = ? AND status != 'sent'", (key,))
            elif kind == "uncertain":
                tgt.execute("""UPDATE drafts SET state = 'uncertain', error = ?, revision = revision + 1,
                               view_id = NULL, updated_at = ? WHERE id = ?""", (UNCERTAIN, now, key))
            elif kind == "draft":
                tgt.execute("""UPDATE drafts SET state = 'draft', due_at = NULL, destination = NULL, error = ?,
                               revision = revision + 1, view_id = NULL, updated_at = ? WHERE id = ?""",
                            (NOTE, now, key))
            elif kind == "due":
                tgt.execute("""UPDATE drafts SET due_at = ?, revision = revision + 1, view_id = NULL,
                               updated_at = ? WHERE id = ?""", (action[2], now, key))
            elif kind == "copy":
                row = auth.execute(f"SELECT {', '.join(post_cols)} FROM published WHERE id = ?", (key,)).fetchone()
                if row is None:
                    continue
                tgt.execute(f"INSERT OR IGNORE INTO published ({', '.join(post_cols)}) VALUES "
                            f"({', '.join('?' * len(post_cols))})", tuple(row))
                if item_cols:
                    for item in auth.execute(f"SELECT {', '.join(item_cols)} FROM published_items WHERE post_id = ?",
                                             (key,)):
                        tgt.execute(f"INSERT OR IGNORE INTO published_items ({', '.join(item_cols)}) VALUES "
                                    f"({', '.join('?' * len(item_cols))})", tuple(item))
