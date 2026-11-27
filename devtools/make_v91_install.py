"""Тестовая установка v9.1 с данными, как на телефоне, но с фиктивными настройками.

Запускается интерпретатором из папки v9.1 (код v9.1 создаёт свою базу сам):
    python make_v91_install.py ПАПКА_SHOPBOT_V91

Создаёт .env с типовыми настройками владельца (токен фиктивный), черновик с фото,
две записи в очереди, одну публикацию в истории и наблюдение за ценой.
"""
from __future__ import annotations

import io
import sys
import time
from decimal import Decimal
from pathlib import Path

folder = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(folder))

from PIL import Image  # noqa: E402

from card import Product  # noqa: E402
from drafts import DraftItem, DraftStore  # noqa: E402
from history import Receipt  # noqa: E402


def jpeg(color):
    buffer = io.BytesIO()
    Image.new("RGB", (120, 160), color).save(buffer, "JPEG")
    return buffer.getvalue()


(folder / ".env").write_text(
    "BOT_TOKEN=123456789:TEST_TOKEN_NOT_REAL_xxxxxxxxxxxxxxxxxxxx\n"
    "ADMIN_IDS=1001\nCHANNEL_ID=@borzmarket_channel\nAUTO_POST=0\nWB_DEST=-1257786\n"
    "QUEUE_SLOTS=10:00,13:00,16:00,19:00\nPRICE_MONITOR=1\n", encoding="utf-8")
store = DraftStore(folder / "borzmarket.sqlite3")
ozon = Product("ozon", "Рюкзак городской", "https://www.ozon.ru/product/1526882900/", Decimal("2969"),
               Decimal("14976"), price_source="ozon.webPrice", conditional_price=Decimal("2672"),
               conditional_note="С банками")
wb = Product("wb", "Робот-пылесос", "https://www.wildberries.ru/catalog/866768707/detail.aspx", Decimal("41133"),
             price_source="wbcon", price_note="по данным WBCON")
draft = store.create(1001, 1001, (DraftItem(ozon, jpeg("orange"), 0),))
queued_a = store.create(1001, 1001, (DraftItem(wb, jpeg("purple"), 0),))
queued_b = store.create(1001, 1001, (DraftItem(ozon, jpeg("green"), 0), DraftItem(wb, jpeg("blue"), 0)))
now = int(time.time())
assert store.schedule(queued_a, now + 3600, "@borzmarket_channel")
assert store.schedule(store.get(queued_b.id), now + 7200, "@borzmarket_channel")
sent = store.create(1001, 1001, (DraftItem(ozon, jpeg("red"), 0),))
assert store.claim(sent, "@borzmarket_channel", now - 86400)
store.finish(sent, "sent", receipt=Receipt(-1001234567890, "@borzmarket_channel", 77,
                                           "https://t.me/borzmarket_channel/77", now - 86400))
store.close()
print(f"draft={draft.id} queued={queued_a.id},{queued_b.id} sent={sent.id}")
