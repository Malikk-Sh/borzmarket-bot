"""Текстовые отчёты и история в личном чате администратора."""
from __future__ import annotations

import html
import time
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal

from card import MARKET_NAMES, fmt_price, short_text
from delivery import MOSCOW, format_time
from history import History, DAY

PERIODS = {1: "24 часа", 7: "7 дней", 30: "30 дней"}


def render_stats(history: History, destination: int | str | None, days: int) -> str:
    posts, items, fetches = history.statistics(destination, int(time.time()) - days * DAY)
    singles = sum(post["item_count"] == 1 for post in posts)
    market = Counter(item["marketplace"] for item in items)
    lines = [
        f"<b>Статистика за {PERIODS[days]}</b>",
        f"Карточки — {singles} · подборки — {len(posts) - singles}",
        f"Товары WB — {market['wb']} · Ozon — {market['ozon']}",
        f"Реакции 🔥 — {sum(post['likes'] for post in posts)}",
    ]

    if items:
        prices = [Decimal(row["price"]) for row in items]
        lines.append(f"Средняя цена товара — {fmt_price(sum(prices) / len(prices))}")
        discounts = []
        for row, current in zip(items, prices):
            if row["old_price"]:
                old = Decimal(row["old_price"])
                if old > current:
                    discounts.append((old - current) * 100 / old)
        if discounts:
            lines.append(f"Средняя скидка — {sum(discounts) / len(discounts):.1f}% по {len(discounts)} товарам")

    lines.append("\n<b>Получение данных</b>")
    for key, name in MARKET_NAMES.items():
        results = {r["result"]: r["total"] for r in fetches if r["marketplace"] == key}
        if results:
            lines.append(
                f"{name} · собрано {results.get('ok', 0)} · неполных {results.get('partial', 0)}"
                f" · HTTP 403 {results.get('403', 0)} · HTTP 429 {results.get('429', 0)}"
                f" · прочих ошибок {results.get('error', 0)}"
            )
    if not fetches:
        lines.append("За этот период запросов пока нет.")

    tracked = [item for item in items if item["token"]]
    if not tracked:
        lines.append("\nПереходы доступны для постов со счётчиком CLICK_BASE_URL.")
        return "\n".join(lines)

    totals = Counter()
    names = {}
    totals_by_post = Counter()
    for item in tracked:
        totals[item["sku"]] += item["clicks"]
        totals_by_post[item["post_id"]] += item["clicks"]
        names[item["sku"]] = item["title"]

    lines.append(f"\n<b>Переходы</b> · {sum(totals.values())}")
    for index, (sku, clicks) in enumerate((p for p in totals.most_common(5) if p[1]), 1):
        lines.append(f"{index}. {html.escape(short_text(names[sku], 55))} · {clicks}")
    by_hour = defaultdict(list)
    for post in posts:
        if post["id"] in totals_by_post:
            hour = datetime.fromtimestamp(post["sent_at"], MOSCOW).hour
            by_hour[hour].append(totals_by_post[post["id"]])
    comparable = {hour: hits for hour, hits in by_hour.items() if len(hits) >= 2}
    if comparable and sum(totals.values()):
        hour = max(comparable, key=lambda h: (sum(comparable[h]) / len(comparable[h]), -h))
        observed = comparable[hour]
        lines.append(
            f"Больше всего переходов на пост у {hour:02d}:00 МСК"
            f" · {sum(observed) / len(observed):.1f} по {len(observed)} постам"
        )
    measured = [post for post in posts if post["views"] and post["id"] in totals_by_post]
    if measured:
        clicks = sum(totals_by_post[post["id"]] for post in measured)
        views = sum(post["views"] for post in measured)
        lines.append(f"Переходы / просмотры · {100 * clicks / views:.1f}% по {len(measured)} постам")
    else:
        lines.append("Для отношения переходов к просмотрам добавьте просмотры через /views.")
    return "\n".join(lines)


def render_history(history: History, destination: int | str | None) -> str:
    posts = history.recent(destination)
    if not posts:
        return "История пока пуста. Здесь будут публикации, подтверждённые Telegram."
    lines = ["<b>Последние публикации</b>"]
    for post in posts:
        title = html.escape(short_text(post["title"], 56))
        if post["post_url"]:
            title = f'<a href="{html.escape(post["post_url"], quote=True)}">{title}</a>'
        lines.append(f"{format_time(post['sent_at'], short=True)} МСК · №{post['message_id']} · {title}")
    lines.append("\nПросмотры можно записать командой <code>/views 42 1200</code>, где 42 — номер поста.")
    return "\n".join(lines)
