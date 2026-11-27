import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import bot
import ozon
import prices
import wbcon
from card import ParseError, Product
from drafts import DraftItem, DraftStore
from isolation import CleanState
from links import article_url, extract_urls

SKU = "1526882900"
OZ = article_url("ozon", SKU)
WB = article_url("wb", "866768707")
SHORT = "https://ozon.ru/t/377XHKT"


class ReleaseTests(CleanState, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)
        self.editor = (91, 92)
        self.addCleanup(bot.INPUTS.pop, self.editor, None)
        self.api = AsyncMock()
        self.slots = asyncio.Semaphore(1)
        for setting in (patch.object(bot, "is_admin", return_value=True), patch.object(bot, "AUTO_POST", False)):
            setting.start()
            self.addCleanup(setting.stop)

    def message(self, text):
        return SimpleNamespace(text=text, caption=None, entities=[], caption_entities=[], photo=[],
                               from_user=SimpleNamespace(id=self.editor[0]), chat=SimpleNamespace(id=self.editor[1]),
                               reply=AsyncMock(), answer=AsyncMock())

    async def handle(self, msg):
        await bot.on_message(msg, self.api, self.store, self.slots)

    def callback(self, data, origin):
        cb = AsyncMock()
        cb.data = data
        cb.from_user.id, cb.message.chat.id = self.editor
        cb.message.reply_to_message = origin
        return cb

    async def test_photo_caption_builds_a_ready_offline_draft(self):
        msg = self.message(None)
        msg.caption = "Озон: 1526882900\nТермос — 1 299 ₽"
        msg.photo = [SimpleNamespace(file_size=100, file_id="sample")]
        def download(file_id, destination):
            Image.new("RGB", (8, 12), "blue").save(destination, "PNG")
        self.api.download.side_effect = download
        empty = DraftItem(Product("ozon", "", OZ))
        with patch.object(bot, "retrieve_links", AsyncMock(return_value=[(empty, "HTTP 403")])), \
             patch.object(bot, "show_preview", AsyncMock()) as show:
            await self.handle(msg)
        draft = self.store.get(show.await_args.args[2].id)
        item = draft.items[0]
        self.assertTrue(draft.ready)
        self.assertEqual((item.product.title, item.product.price, item.product.price_source), ("Термос", 1299, "message"))
        with Image.open(io.BytesIO(item.photo)) as photo:
            self.assertEqual(photo.format, "JPEG")

    async def test_link_order_visible_hidden_and_labelled(self):
        prefix = "🛒 ozon.ru/t/377XHKT\n"
        msg = self.message(prefix + "сапоги\nОзон 1526882900\n" + WB)
        msg.entities = [SimpleNamespace(type="text_link", offset=len(prefix.encode("utf-16-le")) // 2,
                                        length=len("сапоги"), url=WB)]
        with patch.object(bot, "collect_links", AsyncMock()) as collect:
            await self.handle(msg)
        self.assertEqual(collect.await_args.args[-1], [SHORT, WB, OZ])
        self.assertEqual(extract_urls("ozon.ru/t/377XHKT и " + WB), [SHORT, WB])

    async def test_selected_market_appends_to_original_draft_only_once(self):
        draft = self.store.create(*self.editor, (DraftItem(Product("wb", "Первый товар", WB)),))
        bot.INPUTS[self.editor] = bot.Input(draft.id, draft.revision, "append")
        msg = self.message(SKU)
        await self.handle(msg)
        buttons = msg.reply.await_args.kwargs["reply_markup"].inline_keyboard[0]
        self.assertEqual(len(buttons), 2)
        cb = self.callback(buttons[1].callback_data, msg)
        product = DraftItem(Product("ozon", "Второй товар", OZ))
        with patch.object(bot, "retrieve_links", AsyncMock(return_value=[(product, "")])) as load, \
             patch.object(bot, "refresh", AsyncMock()):
            await bot.on_callback(cb, self.api, self.store, self.slots)
            await bot.on_callback(cb, self.api, self.store, self.slots)
        load.assert_awaited_once()
        self.assertEqual(self.store.counts(*self.editor)[0], 1)
        self.assertEqual([i.product.url for i in self.store.get(draft.id).items], [WB, OZ])

    async def test_cancelled_append_choice_cannot_edit_another_draft(self):
        first, second = [self.store.create(*self.editor, (DraftItem(Product("wb", "", WB)),)) for _ in range(2)]
        bot.INPUTS[self.editor] = bot.Input(first.id, first.revision, "append")
        msg = self.message(SKU)
        await self.handle(msg)
        data = msg.reply.await_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data
        bot.INPUTS[self.editor] = bot.Input(second.id, second.revision, "append")
        with patch.object(bot, "retrieve_links", AsyncMock()) as load:
            await bot.on_callback(self.callback(data, msg), self.api, self.store, self.slots)
        load.assert_not_awaited()
        self.assertEqual(bot.INPUTS[self.editor].key, second.id)

    async def test_diag_choice_and_redirect_report_resolved_sku(self):
        msg = self.message("/diag " + SKU)
        await bot.on_diag(msg, self.slots)
        data = msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][1].callback_data
        cb = self.callback(data, msg)
        with patch.object(bot, "diagnose", AsyncMock(return_value="Отчёт")) as diag:
            await bot.on_callback(cb, self.api, self.store, self.slots)
        diag.assert_awaited_once_with(OZ, self.slots)
        def reply(request):
            if request.url.path.startswith("/t/"):
                return httpx.Response(301, headers={"Location": "/product/paroochistitel-6307910379/"})
            return httpx.Response(403)
        with patch.object(ozon, "make_client", return_value=httpx.AsyncClient(transport=httpx.MockTransport(reply))), \
             patch.object(wbcon, "lookup", AsyncMock(side_effect=ParseError("HTTP 429"))), \
             patch.object(prices.bhapi, "token", return_value=None):
            report = await bot.diagnose(SHORT, self.slots)
        for expected in ("Артикул Ozon · 6307910379", "HTTP 301", "HTTP 403", "HTTP 429"):
            self.assertIn(expected, report)

    async def test_mixed_wb_batch_keeps_available_quote_on_429(self):
        first = Product("wb", "Первый", WB)
        second = Product("wb", "Второй", article_url("wb", "12345"))
        responses = [[{"id": 866768707, "salePriceU": 149900}], ParseError("HTTP 429")]
        with patch.object(wbcon, "request_prices", AsyncMock(side_effect=responses)) as fetch:
            await wbcon.lookup(first)
            items = await prices.supplement_wb([second, first])
        self.assertEqual([p.price for p in items], [None, 1499])
        self.assertIn("429", items[0].import_note)
        self.assertEqual(fetch.await_args.args, ("wb", ["12345"]))

    async def test_concurrent_imports_share_one_successful_fallback(self):
        async def quote(*_):
            await asyncio.sleep(0)
            return {"article": SKU, "name":"Термос", "price":1299, "imageUrl":"https://ir.ozone.ru/photo.jpg"}
        with patch.object(prices, "fetch_ozon", AsyncMock(return_value=Product("ozon", "", OZ))), \
             patch.object(wbcon, "request_prices", AsyncMock(side_effect=quote)) as fetch:
            results = await asyncio.gather(prices.fetch_product(OZ), prices.fetch_product(OZ))
        self.assertEqual([p.price for p in results], [1299, 1299])
        fetch.assert_awaited_once()
