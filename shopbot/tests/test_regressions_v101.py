"""Регрессии из ТЗ на собственный источник цен (раздел 8). Без сети и без Telegram-токена."""
import asyncio
import io
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import bot
import prices
import queue_sync
import migrate
import state
from card import Product
from drafts import DraftItem, DraftStore
from fixtures import OZ_SKU, OZ_TITLE, World, jpeg, ozon_image, ozon_page, ozon_product
from history import Receipt
from isolation import CleanState
from links import article_url

OZ = article_url("ozon", OZ_SKU)
ROOT = Path(__file__).resolve().parents[2]


class MemoryOfferTests(CleanState, unittest.TestCase):
    def offer(self, **values):
        base = dict(marketplace="ozon", title=OZ_TITLE, url=OZ, image_urls=["https://ir.ozone.ru/a.jpg"])
        base.update(values)
        return Product(**base)

    def test_new_offer_replaces_all_price_conditions_of_the_old_one(self):
        prices.remember(self.offer(price=Decimal("2969"), old_price=Decimal("14976"), conditional_price=Decimal("2672"),
                                   conditional_note="С банками", price_source="ozon.webPrice", price_at=int(time.time())))
        prices.remember(self.offer(price=Decimal("3083"), price_note="по данным WBCON", price_source="wbcon",
                                   price_at=int(time.time())))
        _, data = state.STATE.recall(prices.memory_key(self.offer()))
        self.assertEqual((data["price"], data["old_price"], data["conditional_price"], data["conditional_note"],
                          data["price_source"]), ("3083", None, None, "", "wbcon"))

    def test_sold_out_invalidates_price_and_incomplete_answer_does_not_bring_it_back(self):
        prices.remember(self.offer(price=Decimal("2969"), price_source="ozon.webPrice", price_at=int(time.time())))
        prices.remember(self.offer(price_unavailable=True, price_source="ozon.webPrice"))
        prices.remember(self.offer())  # следующий ответ без цены и без признака распродажи
        restored = prices.recall(Product("ozon", "", OZ))
        self.assertIsNone(restored.price)
        self.assertEqual(restored.title, OZ_TITLE)


class RecalledPriceAutoPostTests(CleanState, unittest.IsolatedAsyncioTestCase):
    """Цена из памяти после перезапуска видна как восстановленная и не публикуется сама."""

    def setUp(self):
        super().setUp()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.db = Path(self.folder.name) / "borzmarket.sqlite3"
        self.addCleanup(state.configure, None)

    async def import_with_auto_post(self, remembered_source, page=None):
        state.configure(self.db)
        if remembered_source:
            prices.remember(Product("ozon", OZ_TITLE, OZ, Decimal("2969"), image_urls=["https://ir-20.ozone.ru/a.jpg"],
                                    price_source=remembered_source, price_at=int(time.time()) - 60))
        state.configure(None)
        state.configure(self.db)  # «перезапуск»
        prices._RESULT_CACHE.clear()
        no_price = page or ozon_page(price=None, card=None, widgets=False, ld_price="")  # страница без цены
        world = World([(ozon_product, httpx.Response(200, text=no_price)),
                       (ozon_image, httpx.Response(200, content=jpeg())),
                       (lambda r: r.url.host == "oz.wbcon.su", httpx.Response(429, headers={"Retry-After": "60"}))])
        original = httpx.AsyncClient.__init__

        def patched(client, *args, **kwargs):
            kwargs["transport"] = world.transport()
            kwargs.pop("proxy", None)
            original(client, *args, **kwargs)
        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        message = SimpleNamespace(text=OZ, caption=None, entities=[], caption_entities=[], photo=[], document=None,
                                  message_id=5, from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1),
                                  answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())))
        with patch.object(httpx.AsyncClient, "__init__", patched), \
             patch.dict(os.environ, {"HTTP_IMPERSONATE": "0", "OZON_PRICE_FALLBACK": "1", "BHAPI_OZON_TOKEN": ""}), \
             patch.object(bot, "AUTO_POST", True), patch.object(bot, "CHANNEL_ID", "@test_channel"), \
             patch.object(bot, "publish", AsyncMock(return_value=("ok", None))) as publish, \
             patch.object(bot, "show_preview", AsyncMock()) as preview:
            await bot.collect_links(message, AsyncMock(), store, asyncio.Semaphore(1), [OZ])
        return publish, preview, store

    async def test_recalled_storefront_price_waits_for_the_admin(self):
        publish, preview, store = await self.import_with_auto_post("ozon.webPrice")
        publish.assert_not_awaited()
        draft = preview.await_args.args[2]
        product = draft.items[0].product
        self.assertEqual((product.price, product.price_source), (2969, "ozon.webPrice"))
        self.assertEqual(product.price_origin, "memory")  # восстановление видно
        saved = store.get(draft.id).items[0].product
        self.assertEqual(saved.price_origin, "memory")  # и переживает чтение из базы

    async def test_recalled_aggregator_price_also_waits(self):
        publish, _, _ = await self.import_with_auto_post("wbcon")
        publish.assert_not_awaited()

    async def test_only_card_price_waits_for_the_admin(self):
        """Витрина показала только «С банками»: цена сохраняется с условием, но сама не публикуется."""
        page = ozon_page(price=None, card="2 672 ₽", ld_price="")
        publish, preview, _ = await self.import_with_auto_post("", page)
        publish.assert_not_awaited()
        product = preview.await_args.args[2].items[0].product
        self.assertEqual((product.price, product.price_note, product.conditional_price), (2672, "С банками", 2672))

    async def test_full_storefront_price_still_auto_posts(self):
        """Контроль: обычная цена витрины с фото по-прежнему публикуется сама при AUTO_POST=1."""
        publish, _, _ = await self.import_with_auto_post("", ozon_page())
        publish.assert_awaited()


class QueueSyncTests(unittest.TestCase):
    def database(self, path, *drafts):
        store = DraftStore(path)
        for key, state_name in drafts:
            store.conn.execute("""INSERT INTO drafts (id, owner_id, chat_id, heading, items, state, due_at, destination,
                                  updated_at) VALUES (?, 1, 1, '', ?, ?, ?, '@chan', 0)""",
                               (key, json.dumps([{"marketplace": "ozon", "title": "Рюкзак", "url": OZ, "price": "2969",
                                                  "image_urls": []}]), state_name,
                                int(time.time()) + 3600 if state_name == "queued" else None))
            store.conn.execute("INSERT INTO photos VALUES (?, 0, ?)", (key, jpeg()))
        store.conn.commit()
        return store

    def test_draft_already_published_elsewhere_is_retired_and_cannot_be_sent_twice(self):
        with tempfile.TemporaryDirectory() as root:
            new, old = Path(root) / "new.sqlite3", Path(root) / "old.sqlite3"
            current = self.database(new, ("x1", "queued"))
            draft = current.get("x1")
            self.assertTrue(current.claim(draft, "@chan", int(time.time())))
            current.finish(draft, "sent", receipt=Receipt(-1, "@chan", 9, "https://t.me/chan/9", int(time.time())))
            current.close()
            legacy = self.database(old, ("x1", "draft"))  # в старой базе тот же ID остался черновиком
            legacy.close()
            for _ in range(2):  # повторная сверка безопасна
                queue_sync.reconcile(old, new, unknown="draft", apply=True)
            legacy = DraftStore(old)
            self.addCleanup(legacy.close)
            self.assertIsNone(legacy.get("x1"))
            self.assertEqual(legacy.conn.execute("SELECT count(*) FROM published WHERE id='x1'").fetchone()[0], 1)

    def test_claim_refuses_a_draft_whose_id_is_already_in_history(self):
        with tempfile.TemporaryDirectory() as root:
            store = self.database(Path(root) / "db.sqlite3", ("y1", "draft"))
            self.addCleanup(store.close)
            store.conn.execute("""INSERT INTO published (id, destination, status, sent_at, title, item_count)
                                  VALUES ('y1', '@chan', 'sent', 1, 'Рюкзак', 1)""")
            store.conn.commit()
            self.assertFalse(store.claim(store.get("y1"), "@chan", int(time.time())))  # без UNIQUE constraint failed

    def test_double_migration_after_rollback_keeps_history_consistent(self):
        with tempfile.TemporaryDirectory() as root:
            old_dir, new_dir = Path(root) / "old", Path(root) / "new"
            old_dir.mkdir(), new_dir.mkdir()
            self.database(old_dir / "borzmarket.sqlite3", ("z1", "queued"), ("z2", "draft")).close()
            migrate.migrate(old_dir, new_dir)
            legacy = DraftStore(old_dir / "borzmarket.sqlite3")
            z1 = legacy.get("z1")
            legacy.claim(z1, "@chan", int(time.time()))
            legacy.finish(z1, "sent", receipt=Receipt(-1, "@chan", 10, "", int(time.time())))
            legacy.close()
            migrate.migrate(old_dir, new_dir)
            migrate.migrate(old_dir, new_dir)
            store = DraftStore(new_dir / "borzmarket.sqlite3")
            self.addCleanup(store.close)
            self.assertIsNone(store.get("z1"))
            self.assertEqual(store.get("z2").state, "draft")
            self.assertEqual(store.conn.execute("SELECT count(*) FROM published WHERE id='z1'").fetchone()[0], 1)


class CheckTelegramTests(unittest.IsolatedAsyncioTestCase):
    async def run_check(self, proxy, error=None, use_real_session=False):
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, patch.object(bot, "BOT_TOKEN", "123456:TEST"), \
                patch.object(bot, "DB_PATH", Path(folder) / "borzmarket.sqlite3"), \
                patch.dict(os.environ, {"TELEGRAM_PROXY": proxy}), redirect_stdout(output):
            if use_real_session:
                code = await bot.check_telegram()
            else:
                manager = AsyncMock()
                manager.__aenter__.return_value.get_me.side_effect = error
                with patch.object(bot, "create_bot", return_value=manager):
                    code = await bot.check_telegram()
            created = list(Path(folder).iterdir())
        return code, output.getvalue(), created

    async def test_dns_error_is_short_and_hides_credentials(self):
        code, text, created = await self.run_check("socks5://user:secret@ip1.2.3.4:1080",
                                                   socket.gaierror(-2, "Name or service not known"))
        self.assertEqual(code, 1)
        self.assertNotIn("secret", text)
        self.assertNotIn("Traceback", text)
        self.assertIn("прокси", text)
        self.assertEqual(created, [])

    async def test_unreachable_socks_proxy_real_session(self):
        port = socket.socket()
        port.bind(("127.0.0.1", 0))
        number = port.getsockname()[1]
        port.close()  # закрытый порт: прокси недоступен
        code, text, created = await self.run_check(f"socks5://user:secret@127.0.0.1:{number}", use_real_session=True)
        self.assertEqual(code, 1)
        self.assertNotIn("secret", text)
        self.assertIn("прокси", text)
        self.assertEqual(created, [])

    async def test_unauthorized_and_success(self):
        from aiogram.exceptions import TelegramUnauthorizedError
        code, text, _ = await self.run_check("", TelegramUnauthorizedError(method=SimpleNamespace(), message="Unauthorized"))
        self.assertEqual(code, 1)
        self.assertIn("BOT_TOKEN", text)
        manager = AsyncMock()
        manager.__aenter__.return_value.get_me.return_value = SimpleNamespace(username="BorzMarket_bot")
        with patch.object(bot, "BOT_TOKEN", "123456:TEST"), patch.object(bot, "create_bot", return_value=manager), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(await bot.check_telegram(), 0)
        self.assertIn("@BorzMarket_bot", out.getvalue())


class VerifyScriptTests(unittest.TestCase):
    def test_release_check_never_deletes_the_folder_it_was_given(self):
        script = ROOT / "devtools" / "verify_release.sh"
        if not script.exists():
            self.skipTest("devtools нет в архиве релиза")
        with tempfile.TemporaryDirectory() as folder:
            work = Path(folder) / "work"
            work.mkdir()
            keep = work / "важный_файл.txt"
            keep.write_text("не удалять")
            dummy = [Path(folder) / name for name in ("a.zip", "b.zip")]
            for path in dummy:
                path.write_bytes(b"not a zip")
            result = subprocess.run(["bash", str(script), str(dummy[0]), str(dummy[1]), str(work)],
                                    capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": folder})
            self.assertTrue(keep.exists(), result.stdout + result.stderr)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
