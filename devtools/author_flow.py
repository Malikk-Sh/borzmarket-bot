"""Путь автора через настоящие обработчики бота с имитацией Telegram и сети.

получить товар → увидеть проблему → дополнить ровно одно поле → добавить в подборку →
переставить → поставить в ближайший слот. Считает действия пользователя (сообщения и
нажатия), ответы бота и сетевые вызовы. Одинаково запускается для v9.1 и v10:

    python devtools/author_flow.py --bot-dir путь/к/shopbot --out flow.json
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent


class FakeMessage:
    def __init__(self, bot, chat_id, message_id, *, text=None, caption=None, reply_markup=None, photo=None,
                 from_user=None, reply_to=None):
        self.bot = bot
        self.chat = SimpleNamespace(id=chat_id, username=None)
        self.message_id = message_id
        self.text = text
        self.caption = caption
        self.reply_markup = reply_markup
        self.photo = photo
        self.from_user = from_user
        self.reply_to_message = reply_to
        self.entities = []
        self.caption_entities = []
        self.document = None
        self.deleted = False

    async def answer(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, **kw)

    async def reply(self, text, **kw):
        return await self.bot.send_message(self.chat.id, text, reply_to=self, **kw)

    async def edit_text(self, text, **kw):
        self.text = text
        if "reply_markup" in kw:
            self.reply_markup = kw["reply_markup"]
        self.bot.log.append(("edit", self.message_id, text[:80]))
        return self

    async def delete(self):
        self.deleted = True
        self.bot.log.append(("delete", self.message_id, ""))

    def get_url(self):
        return None


class FakeBot:
    def __init__(self):
        self.messages: list[FakeMessage] = []
        self.next_id = 100
        self.log = []

    def _new(self, chat_id, **kw):
        self.next_id += 1
        message = FakeMessage(self, chat_id, self.next_id, **kw)
        self.messages.append(message)
        return message

    async def send_message(self, chat_id, text, reply_markup=None, reply_to=None, **kw):
        self.log.append(("text", self.next_id + 1, text[:80]))
        return self._new(chat_id, text=text, reply_markup=reply_markup, reply_to=reply_to)

    async def send_photo(self, chat_id, photo, caption=None, reply_markup=None, **kw):
        self.log.append(("photo", self.next_id + 1, (caption or "")[:80]))
        message = self._new(chat_id, caption=caption, reply_markup=reply_markup)
        message.photo = [SimpleNamespace(file_id="sent", file_size=1)]
        message.date = SimpleNamespace(timestamp=lambda: time.time())
        return message

    async def edit_message_reply_markup(self, chat_id=None, message_id=None, reply_markup=None, **kw):
        for message in self.messages:
            if message.message_id == message_id:
                message.reply_markup = reply_markup

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        for message in self.messages:
            if message.message_id == message_id:
                message.deleted = True

    async def download(self, file_id, destination):
        from PIL import Image
        Image.new("RGB", (60, 80), "blue").save(destination, "PNG")


def buttons(message):
    markup = message.reply_markup
    if markup is None or not hasattr(markup, "inline_keyboard"):
        return []
    return [b for row in markup.inline_keyboard for b in row]


def find_button(bot, label, *, startswith=True):
    for message in reversed(bot.messages):
        if message.deleted:
            continue
        for b in buttons(message):
            text = b.text or ""
            if (text.startswith(label) if startswith else label in text) and getattr(b, "callback_data", None):
                return message, b
    raise LookupError(f"Кнопка «{label}» не найдена")


class Driver:
    def __init__(self, botmod, store, user=(7, 7)):
        self.botmod = botmod
        self.store = store
        self.fake = FakeBot()
        self.user = user
        self.actions = []
        self.slots = asyncio.Semaphore(2)

    async def send(self, text):
        self.actions.append(("сообщение", text))
        message = FakeMessage(self.fake, self.user[1], 1000 + len(self.actions), text=text,
                              from_user=SimpleNamespace(id=self.user[0]))
        await self.botmod.on_message(message, self.fake, self.store, self.slots)

    async def tap(self, label, **kw):
        message, b = find_button(self.fake, label, **kw)
        self.actions.append(("нажатие", b.text))
        answers = []

        async def answer(text=None, show_alert=False, **_):
            answers.append((text, show_alert))
        cb = SimpleNamespace(data=b.callback_data, from_user=SimpleNamespace(id=self.user[0]), message=message,
                             answer=answer)
        await self.botmod.on_callback(cb, self.fake, self.store, self.slots)
        return answers

    def last_draft(self):
        rows = self.store.conn.execute("SELECT id FROM drafts ORDER BY updated_at DESC, rowid DESC LIMIT 1").fetchall()
        return self.store.get(rows[0][0]) if rows else None


async def scenario(botmod, store, version):
    driver = Driver(botmod, store)
    steps = []

    def mark(name):
        draft = driver.last_draft()
        steps.append(dict(step=name, actions=len(driver.actions),
                          missing=[list(i.missing) for i in draft.items] if draft else None,
                          state=draft.state if draft else None,
                          order=[i.product.url.split("/")[-2] if "ozon" in i.product.url else
                                 i.product.url.split("/")[-2] for i in draft.items] if draft else None))

    # 1. Получить товар: цена WB закрыта (403), резерв WBCON на лимите (429)
    await driver.send("wb:866768707")
    mark("товар получен, цены нет")
    # 2. Дополнить ровно одно поле — цену
    if version >= 10:
        await driver.send("41 133")  # v10: недостающее поле принимается следующим сообщением
    else:
        await driver.tap("💰 Ввести цену")
        await driver.send("41 133")
    mark("цена введена")
    # 3. Добавить в подборку второй товар
    await driver.tap("＋ Товар")
    await driver.send("ozon:1526882900")
    mark("подборка из двух")
    # 4. Переставить: Ozon первым
    await driver.tap("✏️ 2")
    try:
        await driver.tap("⏫ Первым")
    except LookupError:
        await driver.tap("↑ Раньше")
    mark("порядок изменён")
    # 5. В ближайший слот
    try:
        await driver.tap("📅 ")
    except LookupError:
        await driver.tap("🕓 Запланировать")
        await driver.tap("📅 В ближайший слот")
    mark("в очереди")
    draft = driver.last_draft()
    return dict(actions=[f"{kind}: {text}" for kind, text in driver.actions], total_actions=len(driver.actions),
                user_messages=sum(kind == "сообщение" for kind, _ in driver.actions),
                taps=sum(kind == "нажатие" for kind, _ in driver.actions),
                bot_messages=len(driver.fake.messages), steps=steps,
                final_state=draft.state if draft else None,
                due_at=draft.due_at if draft else None)


async def main_async(args):
    sys.path.insert(0, str(args.bot_dir.resolve()))
    sys.path.insert(0, str(HERE))
    os.environ.update({"HTTP_IMPERSONATE": "0", "WB_PRICE_FALLBACK": "1", "OZON_PRICE_FALLBACK": "1",
                       "BHAPI_OZON_TOKEN": "", "BHAPI_WB_TOKEN": "", "WB_DEST": "-1257786",
                       "QUEUE_SLOTS": "10:00,13:00,16:00,19:00"})
    os.environ["CORPUS_LATENCY_SCALE"] = "0.05"
    import corpus
    corpus.BOT_DIR = args.bot_dir.resolve()
    corpus.JPEG, corpus.WEBP = corpus.image_bytes("JPEG"), corpus.image_bytes("WEBP")
    import bot as botmod
    from drafts import DraftStore
    import httpx
    botmod.CHANNEL_ID = "@test_channel"
    botmod.AUTO_POST = False
    botmod.is_admin = lambda user_id: True
    world = corpus.World({"flags": {"wb_detail": "403", "wbcon": "429"}})
    original = httpx.AsyncClient.__init__

    def patched(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(world)
        kw.pop("proxy", None)
        original(self, *a, **kw)
    httpx.AsyncClient.__init__ = patched
    store = DraftStore(":memory:")
    version = 10 if (args.bot_dir / "report.py").exists() else 9
    try:
        result = await scenario(botmod, store, version)
    finally:
        httpx.AsyncClient.__init__ = original
    result["network_calls"] = dict(world.calls)
    result["version"] = version
    print(json.dumps(result, ensure_ascii=False, indent=1))
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
