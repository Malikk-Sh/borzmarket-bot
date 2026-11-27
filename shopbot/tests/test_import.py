import asyncio
import io
import json
import unittest
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import bot
import ozon
import wb
from card import Product
from collage import post_photo, caption_units
from drafts import DraftItem, DraftStore
from links import LinkError, resolve_product_url
from storefront import from_page

OZ_URL = "https://www.ozon.ru/product/ryukzak-12345/"
WB_URL = "https://www.wildberries.ru/catalog/12345/detail.aspx"


def sample_photo():
    buf = io.BytesIO()
    with Image.new("RGB", (70, 190), "orange") as img:
        img.save(buf, "JPEG")
    return buf.getvalue()


class StorefrontTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_stops_at_product_before_loading_its_page(self):
        calls = []
        def reply(req):
            calls.append(req.url.path)
            return httpx.Response(302, headers={"Location": OZ_URL})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            url = await resolve_product_url(client, "https://ozon.ru/t/short", ozon.DOMAINS,
                                            lambda u: u.path.startswith("/product/"), "Ozon")
        self.assertEqual(str(url), OZ_URL)
        self.assertEqual(calls, ["/t/short"])

    async def test_offsite_redirect_is_rejected(self):
        transport = httpx.MockTransport(lambda req: httpx.Response(302, headers={"Location": "https://example.com/product/12345/"}))
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(LinkError):
                await resolve_product_url(client, "https://ozon.ru/t/x", ozon.DOMAINS,
                                          lambda u: u.path.startswith("/product/"), "Ozon")

    def test_composer_merges_widget_and_seo(self):
        doc = {"@type":"Product", "sku":"12345", "name":"Рюкзак",
               "image":[{"contentUrl":"//ir.ozone.ru/s3/photo.jpg"}],
               "offers":{"price":1200, "priceCurrency":"RUB"}}
        data = {"widgetStates":{"webPrice-1":json.dumps({"price":{"text":"1 199,90 ₽"}, "cardPrice":999})},
                "seo":{"script":[{"innerHTML":json.dumps(doc)}]}}
        product = ozon.from_composer(Product("ozon", "", OZ_URL), data)
        self.assertEqual((product.title, product.price, product.price_note), ("Рюкзак", Decimal("1199.90"), ""))
        self.assertEqual(product.image_urls, ["https://ir.ozone.ru/s3/photo.jpg"])

    def test_card_only_price_is_labelled(self):
        product = ozon.from_composer(Product("ozon","",OZ_URL),
                                     {"widgetStates":{"webPrice-a":{"cardPrice":"799 ₽","originalPrice":1200}}})
        self.assertEqual(product.price, 799)
        self.assertEqual(product.price_note, "с Ozon Картой")
        self.assertIsNone(product.old_price)

    def page(self, doc, extra=""):
        payload = f'<html>{extra}<script type="application/ld+json">{json.dumps(doc)}</script></html>'
        response = httpx.Response(200, text=payload, request=httpx.Request("GET",OZ_URL))
        return from_page(Product("ozon", "", OZ_URL), response)

    def test_page_finds_matching_product_and_ruble_offer(self):
        other = {"@type":"Product", "sku":"999", "name":"Другой товар"}
        correct = {"@type":"Product", "sku":"12345", "name":"Термос", "offers":{"price":"999.95","priceCurrency":"RUB"}}
        p = self.page({"@graph":[other, correct]})
        self.assertEqual((p.title, p.price), ("Термос", Decimal("999.95")))

    def test_ranges_and_foreign_prices_are_not_published(self):
        for offer in ({"price":8,"priceCurrency":"USD"}, {"@type":"AggregateOffer","lowPrice":1},
                      {"price":11,"priceCurrency":"RUB","availability":"https://schema.org/OutOfStock"}):
            self.assertIsNone(self.page({"@type":"Product","name":"Товар","offers":offer}).price)

    def test_other_sku_and_challenge(self):
        self.assertFalse(self.page({"@type":"Product","sku":"777","name":"Другой"},
                                   '<meta property="og:type" content="product"><h1>Ошибка</h1>').title)
        self.assertFalse(self.page({}, "<h1>Доступ ограничен</h1>").title)

    async def test_wb_does_not_stop_at_partial_data(self):
        requests = []
        def reply(req):
            requests.append(req.url.path)
            first = len(requests) == 1
            product = {"id":12345, "name":"Рюкзак", "sizes":[{"optionId":3,"price":{"product":175790}}]}
            return httpx.Response(200, json={"products":[{"id":12345, "name":"Рюкзак"}] if first else [product]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            p = await wb._detail(client, 12345, 3, WB_URL+"?size=3")
        self.assertEqual(p.price, Decimal("1757.90"))
        self.assertEqual(len(requests), 2)

    def test_wb_selected_size_and_price_shapes(self):
        data = {"priceU":50000,"salePriceU":42000,
                "sizes":[{"optionId":3, "price":{"product":0,"total":89000}},{"optionId":4,"salePriceU":99000}]}
        self.assertEqual(wb._parse_prices(data, 4)[0], 990)
        self.assertEqual(wb._parse_prices(data, 3)[0], 890)
        self.assertEqual(wb._parse_prices(data, 8), (None,None))
        self.assertEqual(wb._parse_prices({"price":{"product":"24505","basic":29000}}), (Decimal("245.05"),290))

    async def test_wb_rate_limit_and_dated_history(self):
        calls = []
        def reply(req):
            calls.append(req.url.path)
            if req.url.path.endswith("price-history.json"):
                return httpx.Response(200,json=[{"dt":1700000000,"price":{"RUB":29099}}])
            return httpx.Response(429)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            product = await wb._detail(client, 12345, None, WB_URL)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(product.price)
            self.assertIn("HTTP 429", product.import_note)
            self.assertIn("290,99", await wb._history_hint(client,12345,1))

    async def test_retry_preserves_manual_values_and_only_updates_one_record(self):
        store = DraftStore(":memory:")
        self.addCleanup(store.close)
        item = DraftItem(Product("wb","Моё название",WB_URL),sample_photo(),None)
        draft = store.create(1, 2, (item,))
        fresh = DraftItem(Product("wb","Имя из витрины",WB_URL,55))
        fetch = AsyncMock(return_value=(fresh,""))
        with patch.object(bot,"retrieve_item",fetch), patch.object(bot,"refresh",AsyncMock()):
            await bot.retry_missing(AsyncMock(),AsyncMock(),store,draft,0,asyncio.Semaphore(1))
        self.assertFalse(fetch.await_args.kwargs["need_photo"])
        edited = store.get(draft.id)
        self.assertEqual((edited.items[0].product.title,edited.items[0].photo), (item.product.title,item.photo))
        self.assertEqual(edited.items[0].product.price,55)
        self.assertEqual(edited.revision,1)
        self.assertEqual(len(store.page(1,2,False,0,10)),1)

    async def test_shared_title_and_labelled_manual_price(self):
        item=DraftItem(Product("ozon","",OZ_URL))
        for text,title,price in [(f"Рюкзак для ноутбука\n{OZ_URL}","Рюкзак для ноутбука",None),
                                 (f"{OZ_URL}\nНазвание: Рюкзак\nЦена: 1 299,99 ₽","Рюкзак",Decimal("1299.99"))]:
            updated,used=await bot.supplement_message(item,SimpleNamespace(text=text,caption="",photo=[]),AsyncMock())
            self.assertTrue(used)
            self.assertEqual((updated.product.title,updated.product.price), (title,price))


class CollageTests(unittest.TestCase):
    def test_layouts_and_telegram_limits(self):
        store=DraftStore(":memory:")
        self.addCleanup(store.close)
        item=DraftItem(Product("ozon","Кружка & чай 🍵 "*12,OZ_URL,1275,price_note="с Ozon Картой"),sample_photo())
        for count in range(1,7):
            draft=store.create(1,2,(item,)*count)
            with Image.open(io.BytesIO(post_photo(draft))) as img:
                self.assertEqual(img.format,"JPEG")
                self.assertGreater(img.height,0)
                if count > 1:
                    self.assertEqual(img.width,1080)
            self.assertLessEqual(caption_units(bot.preview_text(draft)),1024)
            labels=[b.text for row in bot.preview_keyboard(draft).inline_keyboard for b in row]
            self.assertEqual(labels[0],"🚀 Опубликовать")
        empty=replace(draft,items=(DraftItem(Product("wb","",WB_URL)),)*5)
        with Image.open(io.BytesIO(post_photo(empty))) as img:
            self.assertEqual(img.width,1080)
        self.assertFalse(empty.ready)
        self.assertNotIn("Опубликовать", str(bot.preview_keyboard(empty)))


if __name__ == "__main__":
    unittest.main()
