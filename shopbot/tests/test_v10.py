"""Проверки v10: судьба каждого поля, паузы источников, путь автора, хранение, откат, коллажи."""
import asyncio
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image, ImageDraw

import bot
import collage
import history
import migrate
import monitor
import ozon
import prices
import queue_sync
import report
import slug
import state
import tg_preview
import wb
import wbcon
from card import PROVISIONAL_TITLE, Product, fmt_price
from diagnostics import capture, capture_events
from drafts import DraftItem, DraftStore
from fixtures import (CHALLENGE, OZ_SKU, OZ_SLUG, OZ_TITLE, SHORT, STEAM_SKU, STEAM_SLUG, WB_NM, World, jpeg,
                      ozon_api, ozon_image, ozon_page, ozon_product, ozon_short, redirect_to, wb_basket_card,
                      wb_detail, wb_history, wb_image, wb_page, wb_row, wbcon_ozon, wbcon_wb)
from isolation import CleanState
from links import article_url
from storefront import retry_after

OZ = article_url("ozon", OZ_SKU)
WB = article_url("wb", WB_NM)


@contextmanager
def network(world):
    """Все httpx-клиенты бота (площадки, резервы, фото) ходят в имитацию."""
    original = httpx.AsyncClient.__init__

    def patched(self, *args, **kwargs):
        kwargs["transport"] = world.transport()
        kwargs.pop("proxy", None)
        original(self, *args, **kwargs)
    with patch.object(httpx.AsyncClient, "__init__", patched), \
         patch.dict(os.environ, {"HTTP_IMPERSONATE": "0", "WB_PRICE_FALLBACK": "1", "OZON_PRICE_FALLBACK": "1",
                                 "BHAPI_OZON_TOKEN": "", "BHAPI_WB_TOKEN": "", "WB_DEST": "-1257786"}):
        yield world


def wbcon_limit(request):
    return httpx.Response(429, headers={"Retry-After": "120"}, json={"detail": "Too Many Requests"})


class Base(CleanState, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        bot.INPUTS.clear()
        bot.REFRESHING.clear()
        self.slots = asyncio.Semaphore(2)

    async def item(self, value, routes):
        world = World(routes)
        with network(world):
            market, url = prices.market_url(value)
            responses = await bot.retrieve_links([(market, httpx.URL(url))], self.slots)
        return responses[0], world


class ImportTests(Base):
    async def test_short_link_retried_on_www_and_title_from_slug_when_everything_is_closed(self):
        """Кейс из ТЗ: короткая ссылка, 403 у Ozon, 429 у WBCON, а Telegram показывает превью."""
        def short(request):
            if request.url.host == "ozon.ru":
                return httpx.Response(403, text=CHALLENGE)
            return redirect_to()
        (item, warning), world = await self.item(SHORT, [
            (ozon_short, short), (ozon_product, httpx.Response(403, text=CHALLENGE)), (wbcon_ozon, wbcon_limit)])
        product = item.product
        self.assertEqual(world.calls["ozon_short"], 2)  # ozon.ru → 403, www.ozon.ru → 301
        self.assertEqual(world.calls["ozon_product"], 1)  # одна попытка страницы, без повторов 403
        self.assertEqual(world.calls["ozon_api"], 0)  # внутренний API больше не дёргается
        self.assertIn(STEAM_SKU, product.url)
        self.assertTrue(product.provisional_title)
        self.assertTrue(product.title.startswith("Пароочиститель для уборки дома мощный"))
        self.assertIsNone(product.price)
        self.assertEqual(item.missing, ("название", "цена", "фото"))  # черновое название ждёт подтверждения
        self.assertIn("price", product.gaps)
        self.assertIn("429", product.gaps["price"])
        self.assertIn("повтор после", product.gaps["price"])
        action = report.next_action(product, False, product.trace)
        self.assertIn("фото товара с подписью", action)
        self.assertTrue(state.STATE.blocked("wbcon.ozon"))

    async def test_public_page_gives_all_fields_in_one_request(self):
        (item, _), world = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=ozon_page())), (ozon_image, httpx.Response(200, content=jpeg()))])
        p = item.product
        self.assertEqual((p.title, p.price, p.old_price, p.conditional_price, p.conditional_note),
                         (OZ_TITLE, 2969, 14976, 2672, "С банками"))
        self.assertEqual((p.title_source, p.price_source, p.photo_source), ("ozon.page", "ozon.webPrice", "market"))
        self.assertIsNotNone(p.price_at)
        self.assertFalse(item.missing)
        self.assertEqual(world.calls["ozon_product"], 1)

    async def test_challenge_page_with_http_200_gives_no_false_fields(self):
        (item, _), _ = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=CHALLENGE)), (wbcon_ozon, wbcon_limit)])
        self.assertEqual((item.product.title, item.product.price, item.photo), ("", None, None))
        self.assertTrue(state.STATE.blocked("ozon.page"))

    async def test_substituted_sku_is_rejected(self):
        page = ozon_page(canonical_sku="1000000101", title="Чужой товар")
        (item, _), _ = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=page)), (wbcon_ozon, wbcon_limit)])
        self.assertNotEqual(item.product.title, "Чужой товар")
        self.assertIsNone(item.product.price)

    async def test_sold_out_offer_with_old_price_in_markup_is_not_published(self):
        page = ozon_page(available=False, ld_availability="OutOfStock")
        (item, _), _ = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=page)), (ozon_image, httpx.Response(200, content=jpeg()))])
        self.assertIsNone(item.product.price)
        self.assertTrue(item.product.price_unavailable)
        self.assertIn("price", item.product.gaps)
        self.assertIn("нет в наличии", report.next_action(item.product, True, []))

    async def test_conditional_only_price_keeps_its_condition(self):
        page = ozon_page(price=None, ld_price="2672")
        (item, _), _ = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=page)), (ozon_image, httpx.Response(200, content=jpeg()))])
        self.assertEqual(item.product.price, 2672)
        self.assertTrue(item.product.price_note)

    async def test_expired_price_in_markup_is_not_used(self):
        page = ozon_page(widgets=False, valid_until="2026-01-01")
        (item, _), _ = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=page)), (ozon_image, httpx.Response(200, content=jpeg())),
            (wbcon_ozon, wbcon_limit)])
        self.assertEqual(item.product.title, OZ_TITLE)
        self.assertIsNone(item.product.price)

    async def test_missing_photo_keeps_title_and_price_and_explains(self):
        (item, warning), _ = await self.item("ozon:" + OZ_SKU, [
            (ozon_product, httpx.Response(200, text=ozon_page())), (ozon_image, httpx.Response(404))])
        self.assertEqual((item.product.title, item.product.price, item.photo), (OZ_TITLE, 2969, None))
        self.assertEqual(item.missing, ("фото",))
        self.assertIn("404", item.product.gaps["photo"])
        self.assertIn("Фото не удалось скачать", warning)

    async def test_timeout_is_retried_once_then_succeeds(self):
        attempts = []

        def page(request):
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.ReadTimeout("медленно")
            return httpx.Response(200, text=ozon_page())
        (item, _), _ = await self.item("ozon:" + OZ_SKU, [(ozon_product, page),
                                                           (ozon_image, httpx.Response(200, content=jpeg()))])
        self.assertEqual(len(attempts), 2)
        self.assertEqual(item.product.price, 2969)
        page_events = [e for e in item.product.trace if e.source == "ozon.page"]
        self.assertEqual(page_events[-1].attempts, 2)

    async def test_blocked_page_pauses_ozon_for_the_rest_of_a_collection(self):
        world = World([(ozon_product, httpx.Response(403, text=CHALLENGE)), (wbcon_ozon, wbcon_limit)])
        with network(world):
            links = [("ozon", httpx.URL(article_url("ozon", sku))) for sku in (OZ_SKU, STEAM_SKU, "1000000101")]
            responses = await bot.retrieve_links(links, asyncio.Semaphore(1))
        self.assertEqual(world.calls["ozon_product"], 1)  # v9.1 делал по 5 запросов на каждый товар
        skipped = [e for _, (item, _) in enumerate(responses) for e in item.product.trace if e.outcome == "skipped"]
        self.assertTrue(skipped)

    async def test_wb_block_keeps_title_and_photo_and_history_is_only_a_reference(self):
        routes = [(wb_detail, httpx.Response(403, headers={"server": "wbaas"})),
                  (wb_basket_card, lambda r: httpx.Response(200, json={"nm_id": int(WB_NM), "imt_name": "Робот-пылесос",
                                                                       "media": {"photo_count": 2}})),
                  (wb_history, httpx.Response(200, json=[{"dt": int(time.time()) - 86400, "price": {"RUB": 4099000}}])),
                  (wb_image, httpx.Response(200, content=jpeg())), (wbcon_wb, wbcon_limit), (wb_page, httpx.Response(498))]
        (item, _), world = await self.item("wb:" + WB_NM, routes)
        p = item.product
        self.assertEqual((p.title, p.title_source, p.price), ("Робот-пылесос", "wb.basket", None))
        self.assertIsNotNone(item.photo)
        self.assertIn("40\u00a0990", p.reference_note)
        self.assertNotIn("40\u00a0990", p.import_note)
        self.assertEqual(world.calls["wb_page"], 0)  # страница 498 больше не запрашивается
        self.assertTrue(state.STATE.blocked("wb.detail"))
        # второй товар не трогает цены WB, пока идёт пауза
        with network(world):
            await prices.fetch_product(article_url("wb", "14671602"), direct_only=True)
        self.assertEqual(world.calls["wb_detail"], 1)

    async def test_concurrent_imports_of_one_product_share_a_request(self):
        world = World([(ozon_product, httpx.Response(200, text=ozon_page()))])
        with network(world):
            first, second = await asyncio.gather(prices.fetch_product(OZ, direct_only=True),
                                                 prices.fetch_product(OZ, direct_only=True))
        self.assertEqual(world.calls["ozon_product"], 1)
        self.assertEqual((first.price, second.price), (2969, 2969))
        self.assertIsNot(first, second)


class SourceStateTests(Base):
    def test_retry_after_seconds_date_and_garbage(self):
        self.assertEqual(retry_after("120"), 120)
        now = time.time()
        stamp = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(now + 300))
        self.assertAlmostEqual(retry_after(stamp, now), 300, delta=2)
        self.assertIsNone(retry_after("скоро"))

    async def test_wbcon_pause_survives_restart_and_skips_network(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "borzmarket.sqlite3"
            state.configure(db)
            world = World([(wbcon_wb, lambda r: httpx.Response(429, headers={"Retry-After": "600"}))])
            with network(world):
                with self.assertRaises(Exception):
                    await wbcon.lookup(Product("wb", "Пылесос", WB))
            state.configure(None)
            state.configure(db)  # «перезапуск»
            pause = state.STATE.blocked("wbcon.wb")
            self.assertIsNotNone(pause)
            self.assertGreater(pause[0] - time.time(), 500)
            for window in wbcon._recent.values():
                window.clear()
            with network(world):
                with self.assertRaises(Exception) as error:
                    await wbcon.lookup(Product("wb", "Пылесос", WB))
            self.assertIn("паузе", str(error.exception))
            self.assertEqual(world.calls["wbcon_wb"], 1)
            state.configure(None)

    async def test_background_checks_do_not_take_interactive_quota(self):
        world = World([(wbcon_wb, httpx.Response(200, json=[{"id": int(WB_NM), "salePriceU": 4113300}]))])
        with network(world):
            await wbcon.lookup(Product("wb", "", WB))  # интерактивный запрос занял окно
            with self.assertRaises(Exception):
                await wbcon.lookup(Product("wb", "", article_url("wb", "14671602")), background=True)
        self.assertEqual(world.calls["wbcon_wb"], 1)

    async def test_memory_restores_stable_fields_and_ages_old_price(self):
        good = Product("ozon", OZ_TITLE, OZ, Decimal("2969"), image_urls=["https://ir-20.ozone.ru/a.jpg"],
                       price_source="ozon.webPrice", price_at=int(time.time()) - 3 * 3600)
        prices.remember(good)
        empty = prices.recall(Product("ozon", "", OZ))
        self.assertEqual((empty.title, empty.title_source, empty.image_urls), (OZ_TITLE, "memory", good.image_urls))
        self.assertIsNone(empty.price)  # цене три часа: только справка
        self.assertIn("2\u00a0969", empty.reference_note)
        self.assertIn("3 ч назад", empty.reference_note)
        prices.remember(replace(good, price_at=int(time.time()) - 120))
        fresh = prices.recall(Product("ozon", "", OZ))
        self.assertEqual(fresh.price, 2969)
        self.assertLess(time.time() - fresh.price_at, 200)

    def test_manual_values_and_provisional_titles_never_enter_memory(self):
        prices.remember(Product("ozon", "Термос", OZ, Decimal("1299"), price_source="message", title_source="message"))
        prices.remember(Product("ozon", "Пароочиститель", OZ, title_source=PROVISIONAL_TITLE))
        restored = prices.recall(Product("ozon", "", OZ))
        self.assertEqual((restored.title, restored.price), ("", None))


class SlugTests(unittest.TestCase):
    def test_titles_from_ozon_addresses(self):
        self.assertEqual(slug.title_from_slug(STEAM_SLUG),
                         "Пароочиститель для уборки дома мощный парогенератор профессиональный 15000 Вт 15 бар 15 насадок")
        self.assertTrue(slug.title_from_slug(OZ_SLUG).startswith("Рюкзак мужской городской для ноутбука"))
        self.assertFalse(slug.title_from_slug(OZ_SLUG).endswith("для"))
        self.assertEqual(slug.slug_of(f"/product/{STEAM_SLUG}-{STEAM_SKU}/"), STEAM_SLUG)
        self.assertEqual(slug.slug_of("/product/1526882900/"), "")


class AuthorFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)
        self.api = AsyncMock()
        self.api.send_photo.return_value = SimpleNamespace(message_id=500)
        self.api.send_message.return_value = SimpleNamespace(message_id=501)
        self.editor = (91, 92)
        for setting in (patch.object(bot, "is_admin", return_value=True), patch.object(bot, "AUTO_POST", False),
                        patch.object(bot, "CHANNEL_ID", "@test_channel")):
            setting.start()
            self.addCleanup(setting.stop)

    def message(self, text=None, **extra):
        values = dict(text=text, caption=None, entities=[], caption_entities=[], photo=[], document=None,
                      message_id=77, from_user=SimpleNamespace(id=self.editor[0]),
                      chat=SimpleNamespace(id=self.editor[1]), reply=AsyncMock(), answer=AsyncMock())
        values.update(extra)
        return SimpleNamespace(**values)

    def draft(self, *products, photo=True):
        items = tuple(DraftItem(p, jpeg() if photo else None) for p in products)
        draft = self.store.create(*self.editor, items)
        self.store.bind_view(draft, 10)
        return self.store.get(draft.id)

    async def test_single_missing_price_is_taken_from_the_next_message(self):
        draft = self.draft(Product("wb", "Пылесос", WB))
        await bot.show_preview(self.api, self.store, draft)
        request = bot.INPUTS[self.editor]
        self.assertEqual((request.field, request.auto), ("price", True))
        with patch.object(bot, "refresh", AsyncMock()):
            await bot.on_message(self.message("41 133"), self.api, self.store, self.slots)
        saved = self.store.get(draft.id).items[0].product
        self.assertEqual((saved.price, saved.price_source), (41133, "manual"))
        self.assertIsNotNone(saved.price_at)

    async def test_article_number_is_not_mistaken_for_a_price(self):
        draft = self.draft(Product("wb", "Пылесос", WB))
        await bot.show_preview(self.api, self.store, draft)
        msg = self.message("1526882900")
        await bot.on_message(msg, self.api, self.store, self.slots)
        self.assertIsNone(self.store.get(draft.id).items[0].product.price)
        self.assertNotIn(self.editor, bot.INPUTS)
        self.assertIn("площадке", msg.reply.await_args.args[0])

    async def test_provisional_title_needs_one_tap_to_confirm(self):
        product = Product("ozon", "Пароочиститель для уборки", OZ, Decimal("4009"), title_source=PROVISIONAL_TITLE)
        draft = self.draft(product)
        self.assertFalse(draft.ready)
        labels = [b.text for row in bot.preview_keyboard(draft).inline_keyboard for b in row]
        self.assertIn("✅ Название верно", labels)
        data = next(b.callback_data for row in bot.preview_keyboard(draft).inline_keyboard for b in row
                    if b.text == "✅ Название верно")
        cb = AsyncMock()
        cb.data, cb.from_user.id, cb.message.chat.id, cb.message.message_id = data, *self.editor, 10
        with patch.object(bot, "refresh", AsyncMock()):
            await bot.on_callback(cb, self.api, self.store, self.slots)
        self.assertTrue(self.store.get(draft.id).ready)

    async def test_photo_caption_with_conflicting_market_price_offers_a_choice(self):
        market = DraftItem(Product("ozon", OZ_TITLE, OZ, Decimal("2969"), price_source="ozon.webPrice"), jpeg())
        msg = self.message(None, caption="ozon:1526882900\nТермос — 1 299 ₽")
        item, used = await bot.supplement_message(market, msg, self.api)
        self.assertEqual(item.product.price, 2969)  # витрина того же SKU не заменяется молча
        self.assertEqual(Decimal(item.product.alternatives["price"]["value"]), 1299)
        self.assertEqual(item.product.alternatives["title"]["value"], "Термос")
        labels = [b.text for row in bot.item_buttons(self.draft(item.product), 0) for b in row]
        self.assertTrue(any(label.startswith("💰 Взять цену 1") for label in labels))

    async def test_double_tap_on_retry_does_not_fetch_twice(self):
        draft = self.draft(Product("wb", "Пылесос", WB))
        bot.REFRESHING.add(draft.id)
        cb = AsyncMock()
        with patch.object(bot, "retrieve_item", AsyncMock()) as fetch:
            await bot.retry_missing(cb, self.api, self.store, draft, 0, self.slots)
        fetch.assert_not_awaited()
        self.assertIn("Уже загружаю", cb.answer.await_args.args[0])

    async def test_retry_while_every_source_is_paused_answers_with_time(self):
        draft = self.draft(Product("wb", "Пылесос", WB))
        state.STATE.block("wb.detail", time.time() + 600, "HTTP 403")
        state.STATE.block("wbcon.wb", time.time() + 300, "HTTP 429")
        cb = AsyncMock()
        with patch.object(bot, "retrieve_item", AsyncMock()) as fetch, \
             patch.dict(os.environ, {"WB_PRICE_FALLBACK": "1", "WB_DEST": "-1257786"}):
            await bot.retry_missing(cb, self.api, self.store, draft, 0, self.slots)
        fetch.assert_not_awaited()
        self.assertIn("паузе", cb.answer.await_args.args[0])

    async def test_refill_fetches_only_missing_items_and_sends_one_summary(self):
        ready = Product("wb", "Готовый", article_url("wb", "14671602"), Decimal("1299"))
        empty = Product("wb", "Пылесос", WB)
        draft = self.draft(ready, empty)
        fresh = DraftItem(replace(empty, price=Decimal("41133"), price_source="wbcon"), jpeg())
        cb = AsyncMock()
        with patch.object(bot, "retrieve_links", AsyncMock(return_value=[(fresh, "")])) as load, \
             patch.object(bot, "refresh", AsyncMock()):
            await bot.refill_collection(cb, self.api, self.store, draft, self.slots)
        self.assertEqual(load.await_args.args[0], [("wb", WB)])
        self.assertEqual(self.store.get(draft.id).items[1].product.price, 41133)
        summary = self.api.send_message.await_args.args[1]
        self.assertIn("2. ✅ цена", summary)

    async def test_nearest_slot_is_one_tap_and_shows_the_time(self):
        draft = self.draft(Product("ozon", OZ_TITLE, OZ, Decimal("2969")))
        slot = bot.nearest_slot(self.store, draft)
        keyboard = bot.preview_keyboard(draft, slot)
        first_row = [b.text for b in keyboard.inline_keyboard[0]]
        self.assertEqual(first_row[0], "🚀 Опубликовать")
        self.assertTrue(first_row[1].startswith("📅 "))
        cb = AsyncMock()
        cb.data = keyboard.inline_keyboard[0][1].callback_data
        cb.from_user.id, cb.message.chat.id, cb.message.message_id = *self.editor, 10
        with patch.object(bot, "refresh", AsyncMock()):
            await bot.on_callback(cb, self.api, self.store, self.slots)
        queued = self.store.get(draft.id)
        self.assertEqual((queued.state, queued.due_at), ("queued", slot))

    async def test_move_first_keeps_numbers_captions_and_buttons_in_one_order(self):
        products = [Product("wb", f"Товар {i}", article_url("wb", str(100000 + i)), Decimal(100 + i)) for i in range(3)]
        draft = self.draft(*products)
        cb = AsyncMock()
        with patch.object(bot, "refresh", AsyncMock()):
            await bot.change_item(cb, self.api, self.store, draft, "first", 2)
        moved = self.store.get(draft.id)
        self.assertEqual([i.product.title for i in moved.items], ["Товар 2", "Товар 0", "Товар 1"])
        from outbound import post_keyboard
        urls = tuple(i.product.url for i in moved.items)
        buttons = [b for row in post_keyboard(moved, urls).inline_keyboard for b in row if b.url]
        self.assertEqual([b.url for b in buttons], list(urls))
        self.assertEqual([b.text.split(" · ")[0] for b in buttons], ["1", "2", "3"])
        caption = collage.post_caption(moved)
        self.assertLess(caption.index("Товар 2"), caption.index("Товар 0"))

    async def test_mixed_collection_order_duplicates_sizes_and_emoji_hidden_link(self):
        wb_a = "https://www.wildberries.ru/catalog/1421304340/detail.aspx?size=101"
        wb_b = "https://www.wildberries.ru/catalog/1421304340/detail.aspx?size=102"
        prefix = "🔥 " + SHORT + "\n" + wb_a + "\n👉 "
        text = prefix + "берцы\n" + wb_b + "\n" + wb_a + "\nозон 1526882900"
        msg = self.message(text)
        msg.entities = [SimpleNamespace(type="text_link", offset=len(prefix.encode("utf-16-le")) // 2,
                                        length=len("берцы"), url="https://t.me/borzmarket_channel/1")]
        with patch.object(bot, "collect_links", AsyncMock()) as collect:
            await bot.on_message(msg, self.api, self.store, self.slots)
        urls = collect.await_args.args[-1]
        self.assertEqual(urls[:3], [SHORT, wb_a, "https://t.me/borzmarket_channel/1"])
        self.assertEqual(urls[3:], [wb_b, OZ])  # повтор wb_a склеен, порядок сохранён
        # разные размеры одного товара WB остаются отдельными позициями подборки
        from history import product_key
        self.assertNotEqual(product_key(Product("wb", "", wb_a), variant=True),
                            product_key(Product("wb", "", wb_b), variant=True))

    async def test_diag_starts_with_summary_and_export_has_no_secrets(self):
        secret_env = {"BOT_TOKEN": "123456:SECRET_TOKEN_VALUE", "BHAPI_OZON_TOKEN": "bhapi-secret-key",
                      "ADMIN_IDS": "987654321"}
        def short(request):
            return httpx.Response(403, text=CHALLENGE) if request.url.host == "ozon.ru" else redirect_to()
        world = World([(ozon_short, short), (ozon_product, httpx.Response(403, text=CHALLENGE)),
                       (wbcon_ozon, wbcon_limit), (lambda r: r.url.host == "bhapi.ru", httpx.Response(429, headers={"Retry-After": "60"}))])
        with network(world), patch.dict(os.environ, secret_env):
            text = await bot.diagnose(SHORT, self.slots)
        lines = text.splitlines()
        self.assertIn("Диагностика", lines[0])
        self.assertIn("неполная", lines[1])
        self.assertIn(STEAM_SKU, lines[1])
        self.assertTrue(any(line.startswith("👉") for line in lines[:8]))
        for secret in ("SECRET_TOKEN_VALUE", "bhapi-secret-key", "987654321"):
            self.assertNotIn(secret, text)
        self.assertIn("BM-DIAG v10.0", text)
        self.assertLessEqual(len(text), 4096)


class StorageTests(Base):
    def test_unknown_future_keys_do_not_break_reading(self):
        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        draft = store.create(1, 2, (DraftItem(Product("ozon", "Термос", OZ, Decimal("1299")), jpeg()),))
        data = json.loads(store.conn.execute("SELECT items FROM drafts").fetchone()[0])
        data[0]["field_from_v11"] = {"x": 1}
        for key in ("title_source", "gaps", "alternatives", "price_at"):
            data[0].pop(key)  # как черновик из v9.1
        store.conn.execute("UPDATE drafts SET items = ?", (json.dumps(data),))
        restored = store.get(draft.id).items[0].product
        self.assertEqual((restored.title, restored.price, restored.title_source), ("Термос", 1299, ""))

    def v91_database(self, path: Path, *, queued_ids=("q1", "q2", "q3")):
        """База в схеме v9.1 с черновиком с фото, очередью и историей."""
        store = DraftStore(path)
        items = (DraftItem(Product("ozon", "Термос", OZ, Decimal("1299")), jpeg()),)
        draft = store.create(5, 6, items)
        for key in queued_ids:
            store.conn.execute("""INSERT INTO drafts (id, owner_id, chat_id, heading, items, state, due_at, destination,
                                  updated_at) VALUES (?, 5, 6, '', ?, 'queued', ?, '@chan', ?)""",
                               (key, store.conn.execute("SELECT items FROM drafts WHERE id=?", (draft.id,)).fetchone()[0],
                                int(time.time()) + 3600, int(time.time())))
            store.conn.execute("INSERT INTO photos (draft_id, item_number, data) VALUES (?, 0, ?)", (key, jpeg()))
        store.conn.commit()
        store.close()
        return draft.id

    def test_migration_from_v91_copy_keeps_everything_and_rerun_is_safe(self):
        with tempfile.TemporaryDirectory() as root:
            old, new = Path(root) / "old", Path(root) / "new"
            old.mkdir(), new.mkdir()
            (old / ".env").write_text("BOT_TOKEN=x\nCHANNEL_ID=@borzmarket_channel\nWB_DEST=-1257786\n")
            draft_id = self.v91_database(old / "borzmarket.sqlite3")
            migrate.migrate(old, new)
            migrate.migrate(old, new)  # повтор ничего не ломает
            self.assertIn("@borzmarket_channel", (new / ".env").read_text())
            store = DraftStore(new / "borzmarket.sqlite3")
            self.addCleanup(store.close)
            self.assertIsNotNone(store.get(draft_id).items[0].photo)
            self.assertEqual(store.counts(5, 6), (1, 3, store.counts(5, 6)[2]))
            state.configure(new / "borzmarket.sqlite3")
            self.addCleanup(state.configure, None)
            state.STATE.block("wbcon.wb", time.time() + 60, "HTTP 429")
            self.assertTrue(state.STATE.blocked("wbcon.wb"))

    def test_rollback_does_not_resend_posts_published_by_the_new_version(self):
        with tempfile.TemporaryDirectory() as root:
            old, new = Path(root) / "old" / "borzmarket.sqlite3", Path(root) / "new" / "borzmarket.sqlite3"
            old.parent.mkdir(), new.parent.mkdir()
            self.v91_database(old)
            migrate.migrate(old.parent, new.parent)
            store = DraftStore(new)
            q1 = store.get("q1")
            self.assertTrue(store.claim(q1, "@chan", int(time.time())))
            from history import Receipt
            store.finish(q1, "sent", receipt=Receipt(-100, "@chan", 42, "https://t.me/chan/42", int(time.time())))
            q2 = store.get("q2")
            self.assertTrue(store.unschedule(q2))
            store.conn.execute("DELETE FROM drafts WHERE id = 'q3'")
            store.conn.commit()
            store.close()
            plan = queue_sync.reconcile(old, new, unknown="draft", apply=True)
            self.assertTrue(any(line.startswith("🗑") for line in plan))
            conn = sqlite3.connect(old)
            states = dict(conn.execute("SELECT id, state FROM drafts WHERE id IN ('q1','q2','q3')").fetchall())
            self.assertNotIn("q1", states)  # уже ушёл в канал — в старой очереди его нет
            self.assertEqual((states["q2"], states["q3"]), ("draft", "draft"))
            self.assertEqual(conn.execute("SELECT status FROM published WHERE id = 'q1'").fetchone()[0], "sent")
            conn.close()

    def test_reupgrade_after_rollback_removes_posts_sent_by_the_old_version(self):
        with tempfile.TemporaryDirectory() as root:
            old, new = Path(root) / "old", Path(root) / "new"
            old.mkdir(), new.mkdir()
            self.v91_database(old / "borzmarket.sqlite3")
            migrate.migrate(old, new)
            legacy = DraftStore(old / "borzmarket.sqlite3")
            q2 = legacy.get("q2")
            legacy.claim(q2, "@chan", int(time.time()))
            from history import Receipt
            legacy.finish(q2, "sent", receipt=Receipt(-100, "@chan", 43, "", int(time.time())))
            legacy.close()
            migrate.migrate(old, new)
            store = DraftStore(new / "borzmarket.sqlite3")
            self.addCleanup(store.close)
            self.assertIsNone(store.get("q2"))
            self.assertEqual(store.get("q1").state, "queued")


class MonitorTests(Base):
    async def test_manual_reference_is_rebased_without_alert(self):
        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        product = Product("ozon", "Термос", OZ, Decimal("1299"), price_source="manual")
        draft = store.create(1, 2, (DraftItem(product, jpeg()),))
        store.claim(draft, "@chan", int(time.time()))
        from history import Receipt
        store.finish(draft, "sent", receipt=Receipt(-1, "@chan", 1, "", int(time.time()) - 2 * 86400))
        watch = store.history.claim_watch(int(time.time()))
        self.assertEqual(watch.source, "manual")
        current = DraftItem(Product("ozon", "Термос", OZ, Decimal("999"), price_source="ozon.webPrice"), jpeg())
        fetch = AsyncMock(return_value=(current, ""))
        result = await monitor.check_price(AsyncMock(), store, watch, Decimal("15"), fetch, AsyncMock(), self.slots)
        self.assertIsNone(result)  # 999 vs ручные 1299 — не повод для тревоги
        row = store.conn.execute("SELECT reference, price_source FROM price_watch").fetchone()
        self.assertEqual((row[0], row[1]), ("999", "ozon.webPrice"))


class CollageTests(unittest.TestCase):
    def setUp(self):
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)

    def product_photo(self, size=(900, 1200), box=(330, 300, 570, 1000), background="white"):
        image = Image.new("RGB", size, background)
        ImageDraw.Draw(image).rectangle(box, fill=(60, 70, 80))
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=92)
        return buffer.getvalue()

    def test_every_size_is_square_for_full_width_in_the_feed(self):
        items = [DraftItem(Product("ozon", f"Товар {i}", OZ, Decimal(1000 + i), Decimal(2000)), self.product_photo())
                 for i in range(6)]
        for count in range(1, 7):
            draft = self.store.create(1, 2, tuple(items[:count]))
            with Image.open(io.BytesIO(collage.post_photo(draft))) as image:
                self.assertEqual(image.size, (1080, 1080))

    def test_uniform_margins_are_trimmed_but_living_backgrounds_are_not(self):
        info = collage.analyze(self.product_photo())
        left, top, right, bottom = info.box
        self.assertLess((right - left) * (bottom - top), 0.4 * 900 * 1200)
        self.assertTrue(left <= 330 and top <= 300 and right >= 570 and bottom >= 1000)  # товар не обрезан
        gradient = Image.linear_gradient("L").resize((600, 800)).convert("RGB")
        buffer = io.BytesIO()
        gradient.save(buffer, "JPEG")
        living = collage.analyze(buffer.getvalue())
        self.assertIsNone(living.background)
        self.assertEqual(living.box, (0, 0, 600, 800))

    def test_original_photo_setting_is_kept(self):
        raw = self.product_photo()
        draft = self.store.create(1, 2, (DraftItem(Product("ozon", "Термос", OZ, Decimal("1299")), raw),))
        with patch.dict(os.environ, {"IMAGE_BADGES": "0"}):
            self.assertEqual(collage.post_photo(draft), raw)

    def test_condition_price_is_in_the_caption_of_a_single_card(self):
        p = Product("ozon", "Пароочиститель", OZ, Decimal("4009"), Decimal("25000"), conditional_price=Decimal("3608"),
                    conditional_note="С банками")
        draft = self.store.create(1, 2, (DraftItem(p, self.product_photo()),))
        self.assertIn("С банками — 3 608 ₽", collage.post_caption(draft))


class PreviewTests(Base):
    def test_disabled_without_keys_and_title_cleanup(self):
        with patch.dict(os.environ, {"TG_API_ID": "", "TG_API_HASH": ""}):
            self.assertFalse(tg_preview.enabled())
        self.assertEqual(tg_preview.clean_title("Рюкзак городской купить на OZON по низкой цене (1526882900)", "OZON"),
                         "Рюкзак городской")

    async def test_preview_fills_title_and_photo_only_for_the_same_link(self):
        item = DraftItem(Product("ozon", "Пароочиститель для уборки", article_url("ozon", STEAM_SKU),
                                 title_source=PROVISIONAL_TITLE))
        link = ("ozon", httpx.URL(SHORT))
        same = tg_preview.Preview(SHORT, "OZON", "Пароочиститель профессиональный 15000 Вт", "", jpeg())
        with patch.object(tg_preview, "enabled", return_value=True), \
             patch.object(tg_preview, "fetch", AsyncMock(return_value=same)):
            filled = await bot.apply_preview(item, SimpleNamespace(message_id=5), link)
        self.assertEqual((filled.product.title, filled.product.title_source), ("Пароочиститель профессиональный 15000 Вт", "tg.preview"))
        self.assertIsNotNone(filled.photo)
        other = tg_preview.Preview("https://ozon.ru/t/OTHER", "OZON", "Чужой", "", jpeg())
        with patch.object(tg_preview, "enabled", return_value=True), \
             patch.object(tg_preview, "fetch", AsyncMock(return_value=other)):
            kept = await bot.apply_preview(item, SimpleNamespace(message_id=5), link)
        self.assertEqual(kept.product.title, "Пароочиститель для уборки")
        self.assertIsNone(kept.photo)


if __name__ == "__main__":
    unittest.main()
