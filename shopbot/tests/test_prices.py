import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from html import escape
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import bot
import ozon
import prices
import wb
import wbcon
from isolation import CleanState
from card import ParseError, Product, manual_price
from drafts import DraftItem, DraftStore
from store_prices import shared_quote, wb_quote
from storefront import from_page

OZ = "https://www.ozon.ru/product/rukzak-1526882900/"
WB = "https://www.wildberries.ru/catalog/866768707/detail.aspx"


def html_page(url, content):
    return httpx.Response(200, text=content, request=httpx.Request("GET", url))


def size(option, amount, stock):
    return {"optionId": option, "name": str(option), "stocks": [{"qty": stock}],
            "price": {"product": amount, "basic": 500000}}


class PriceTests(CleanState, unittest.IsolatedAsyncioTestCase):
    def test_ozon_embedded_widget_and_payment_condition(self):
        w = {"price": "2 969 ₽", "cardPrice": "2 672 ₽", "originalPrice": 14976,
             "lexemes": {"withOzonCard": "С банками"}}
        content = f'<link rel="canonical" href="{OZ}"><div id="state-webPrice-42" data-state="{escape(json.dumps(w))}"></div>'
        p = from_page(Product("ozon", "", OZ), html_page(OZ, content))
        self.assertEqual((p.price, p.old_price, p.conditional_price, p.conditional_note, p.price_source),
                         (2969, 14976, 2672, "С банками", "ozon.webPrice"))

    def test_composer_with_explicitly_other_sku(self):
        data = {"widgetStates": {"webPrice-1": {"price": 1}},
                "seo": {"script": [{"innerHTML": '{"@type":"Product","sku":"111"}'}]}}
        self.assertIsNone(ozon.from_composer(Product("ozon", "", OZ), data).price)

    def test_sold_out_ambiguity_and_expired_price_not_replaced_by_metadata(self):
        def product(offers):
            node = {"@type": "Product", "sku": "1526882900", "name": "Рюкзак", "offers": offers}
            content = ('<meta property="og:type" content="product"><meta property="product:price:currency" content="RUB">'
                       '<meta property="product:price:amount" content="22"><script type="application/ld+json">'
                       + json.dumps(node) + '</script>')
            return from_page(Product("ozon", "", OZ), html_page(OZ, content))
        offer = {"priceCurrency": "RUB", "price": 33}
        self.assertTrue(product(dict(offer, availability="https://schema.org/OutOfStock")).price_unavailable)
        self.assertIsNone(product([offer, dict(offer, price=44)]).price)
        self.assertIsNone(product(dict(offer, priceValidUntil="2000-01-01")).price)
        self.assertIsNone(product(dict(offer, validFrom="2100-01-01")).price)
        self.assertEqual(product({"priceSpecification": offer}).price, 33)
        self.assertIsNone(product({"priceSpecification": dict(offer, priceType="ListPrice")}).price)

    def test_sizes_stock_and_kopecks(self):
        base = Product("wb", "", WB)
        data = {"sizes": [size(1, 5000, 0), size(2, 46700, 2), size(3, 14999, 1)]}
        p = wb_quote(base, data)
        self.assertEqual((p.price, p.price_note), (Decimal("149.99"), "минимум по доступным вариантам"))
        self.assertEqual(wb_quote(base, data, 2).price, 467)
        self.assertTrue(wb_quote(base, data, 1).price_unavailable)
        self.assertIsNone(wb_quote(base, data, 4).price)
        self.assertTrue(wb_quote(base, {"sizes": [size(1, 5000, 0)], "salePriceU": 30000}).price_unavailable)

    async def test_invalid_size_keeps_its_scope(self):
        async with httpx.AsyncClient() as client:
            for suffix in ["?size=", "?size=blue", "?size=2&size=3"]:
                with self.assertRaises(ParseError):
                    await wb._resolve_product(client, WB + suffix)

    async def test_secondary_batch_matches_skus_and_preserves_partial_cards(self):
        a = Product("wb", "Пылесос", WB, image_urls=["https://example.com/image"])
        b = replace(a, url=WB.replace("866768707", "1097975275"))
        unavailable = replace(a, price_unavailable=True)
        sized = replace(a, url=WB + "?size=19")
        response = [{"id": 1097975275, "salePriceU": 0}, {"id": 866768707, "salePriceU": 4113300}]
        with patch.dict("os.environ", {"WB_DEST": "-1257786", "WB_PRICE_FALLBACK": "1"}), \
             patch.object(wbcon, "request_prices", AsyncMock(return_value=response)) as fetch:
            got = await prices.supplement_wb([a, b, unavailable, sized, replace(a, price=5)])
        fetch.assert_awaited_once_with("wb", ["866768707", "1097975275"])
        self.assertEqual((got[0].price, got[0].price_source, got[0].title, got[0].image_urls),
                         (41133, "wbcon", a.title, a.image_urls))
        self.assertEqual([p.price for p in got[1:]], [None, None, None, 5])
        self.assertIsNone(a.price)
        with patch.dict("os.environ", {"WB_DEST": "111"}), patch.object(wbcon, "request_prices", AsyncMock()) as fetch:
            self.assertEqual((await prices.supplement_wb([a]))[0].price, None)
            fetch.assert_not_awaited()

    async def test_direct_only_works_without_external_access(self):
        p = Product("wb", "Фотоаппарат", WB)
        with patch.object(prices, "fetch_wb", AsyncMock(return_value=p)), \
             patch.object(wbcon, "lookup_batch", AsyncMock()) as secondary:
            got = await prices.fetch_product(WB, direct_only=True)
            # v10 отдаёт копию: общий результат параллельных запросов не должен меняться у соседа.
            self.assertEqual((got.title, got.url, got.price), (p.title, p.url, p.price))
            secondary.assert_not_awaited()

    async def test_telegram_import_handles_secondary_failure(self):
        item = DraftItem(Product("wb", "Пылесос", WB, import_note="Цены WB — HTTP 403"))
        with patch.dict("os.environ", {"WB_DEST": "-1257786", "WB_PRICE_FALLBACK": "1"}), \
             patch.object(bot, "retrieve_item", AsyncMock(return_value=(item, item.product.import_note))), \
             patch.object(wbcon, "lookup_batch", AsyncMock(side_effect=ParseError("Лимит WBCON"))):
            responses = await bot.retrieve_links([("wb", WB)], asyncio.Semaphore(1))
        self.assertEqual(responses[0][0].product.title, "Пылесос")
        self.assertIsNone(responses[0][0].product.price)
        self.assertIn("Лимит WBCON", responses[0][1])

    def test_draft_round_trip_manual_override_and_existing_record(self):
        with tempfile.TemporaryDirectory() as temp:
            store = DraftStore(Path(temp) / "db")
            self.addCleanup(store.close)
            p = Product("ozon", "Рюкзак", OZ, price=Decimal("1199"), conditional_price=Decimal("999"),
                        conditional_note="С банками", price_source="ozon.webPrice")
            d = store.create(1, 2, (DraftItem(p),))
            original = store.get(d.id).items[0].product
            self.assertEqual(original.conditional_price, Decimal("999"))
            self.assertIsNone(manual_price(original, Decimal("1150")).conditional_price)
            encoded = json.loads(store.conn.execute("SELECT items FROM drafts WHERE id=?", (d.id,)).fetchone()[0])
            for key in ("price_source", "conditional_price", "conditional_note", "price_unavailable"):
                encoded[0].pop(key)
            store.conn.execute("UPDATE drafts SET items=? WHERE id=?", (json.dumps(encoded), d.id))
            self.assertEqual(store.get(d.id).items[0].product.price, 1199)

    async def test_import_with_external_price_remains_a_ready_draft(self):
        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        message = AsyncMock()
        message.from_user.id, message.chat.id = 1, 2
        data = DraftItem(Product("wb", "Пылесос", WB), b"photo")
        with patch.dict("os.environ", {"WB_DEST": "-1257786", "WB_PRICE_FALLBACK": "1"}), \
             patch.object(bot, "AUTO_POST", True), patch.object(bot, "CHANNEL_ID", 3), \
             patch.object(bot, "retrieve_item", AsyncMock(return_value=(data, ""))), \
             patch.object(wbcon, "request_prices", AsyncMock(return_value=[{"id": 866768707, "salePriceU": 4113300}])), \
             patch.object(bot, "publish", AsyncMock()) as publish, patch.object(bot, "show_preview", AsyncMock()) as show:
            await bot.collect_links(message, AsyncMock(), store, asyncio.Semaphore(1), [WB])
        publish.assert_not_awaited()
        draft = store.get(show.await_args.args[-1].id)
        self.assertTrue(draft.ready)
        self.assertEqual((draft.items[0].product.price, draft.items[0].product.price_source), (41133, "wbcon"))

    def test_shared_price_and_payment_labels(self):
        p = Product("wb", "", WB)
        quote = shared_quote(p, "Сапоги\n" + WB + "\n1 190 ₽ с WB Кошельком\nБез WB Кошелька: 1 290 ₽")
        self.assertEqual((quote.price, quote.conditional_price, quote.price_source), (1290, 1190, "message"))
        self.assertEqual(shared_quote(p, "С WB Кошельком: 790 ₽").price_note, "с WB Кошельком")
        self.assertIsNone(shared_quote(p, "Доставка 150 ₽\nЦена: 100\nЦена: 200").price)

    def test_offline_requires_same_market_and_sku(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "card.html"
            for url in ["https://www.ozon.ru/product/123/", "https://www.wildberries.ru/catalog/1526882900/detail.aspx"]:
                path.write_text(f'<link rel="canonical" href="{url}">', encoding="utf-8")
                with self.assertRaises(ParseError):
                    prices.saved_page(OZ, path)
