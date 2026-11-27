import asyncio
import json
import os
import unittest
from dataclasses import replace
from decimal import Decimal
from html import escape
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import bhapi
import bot
import prices
import wbcon
from card import ParseError, Product, parse_price
from diagnostics import capture, step
from drafts import DraftItem, DraftStore
from history import Receipt
from monitor import check_price
from outbound import post_keyboard, refresh_reaction
from screenshot_price import amounts
from store_prices import wb_quote
from storefront import from_page

OZ = "https://www.ozon.ru/product/12345/"
WB = "https://www.wildberries.ru/catalog/12345/detail.aspx"
PIC = "https://ir.ozone.ru/img.jpg"


class V8(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"BHAPI_WB_TOKEN": "", "BHAPI_OZON_TOKEN": "", "OZON_PRICE_FALLBACK": "1", "WB_DEST": "-1257786"})
        env.start()
        self.addCleanup(env.stop)

    def test_ozon_embedded_fields_and_foreign_canonical(self):
        raw = {"webProductHeading": {"title": "Термос"}, "webGallery": {"images": [{"src": PIC}]},
               "webPrice": {"price": "2 000 ₽", "cardPrice": "1 550 ₽"}}
        markup = "".join(f'<div id="state-{key}-abcd" data-state="{escape(json.dumps(val))}"></div>' for key, val in raw.items())
        base = Product("ozon", "", OZ)
        for canon, expected in [(OZ, "Термос"), (WB.replace("12345","555"), "")]:
            response = httpx.Response(200, request=httpx.Request("GET", OZ), text=f'<link rel="canonical" href="{canon}">' + markup)
            p = from_page(base, response)
            self.assertEqual(p.title, expected)
            if expected:
                self.assertEqual((p.price, p.conditional_price, p.image_urls), (2000, 1550, [PIC]))

    def test_wb_uses_top_level_only_for_the_identified_variant(self):
        product = Product("wb", "", WB)
        row = {"sizes": [{"optionId": 9}], "salePriceU": 188850}
        self.assertEqual(wb_quote(product, row, 9).price, Decimal("1888.50"))
        self.assertIsNone(wb_quote(product, row, 10).price)
        row["sizes"].append({"optionId": 10})
        self.assertIsNone(wb_quote(product, row, 9).price)
        self.assertEqual(parse_price("1\u2060299 рублей"), 1299)

    def test_bhapi_ozon_contract_and_wrong_sku(self):
        p = Product("ozon", "", OZ)
        row = {"sku": "12345", "title": "Термос", "images": [PIC],
               "prices": {"currency": "RUB", "regular": 1990, "bank_card": 1890, "original": 2490}}
        loaded = bhapi.parse(p, {"status":"ok", "data":row})
        self.assertEqual((loaded.title, loaded.price, loaded.conditional_price, loaded.image_urls), ("Термос",1990,1890,[PIC]))
        row["prices"]["currency"] = "USD"
        self.assertIsNone(bhapi.parse(p, {"status":"ok", "data":row}).price)
        row["sku"] = "777"
        with self.assertRaises(ParseError):
            bhapi.parse(p, {"status":"ok", "data":row})

    def test_bhapi_wb_size_region_and_price_unit(self):
        p = Product("wb", "", WB + "?size=77")
        row = {"item_id":12345, "currency":"RUB", "title":"Товар",
               "data_freshness":{"dest":-1257786},
               "sizes":[{"option_id":77, "availability":1, "price_info":{"pre_sale_price":1345.5}}]}
        payload = {"status":"ok","data":{"data":row}}
        got = bhapi.parse(p,payload)
        self.assertEqual((got.price, got.wb_dest), (Decimal("1345.50"), "-1257786"))
        with patch.dict(os.environ, {"WB_DEST":"17"}):
            self.assertIsNone(bhapi.parse(p,payload).price)
        self.assertIsNone(bhapi.parse(replace(p,url=WB+"?size=99"),payload).price)
        row["sizes"][0]["availability"] = 0
        self.assertIsNone(bhapi.parse(p,payload).price)

    async def test_real_import_path_loads_secondary_photo_and_holds_auto_post(self):
        msg = SimpleNamespace(text=OZ, caption=None, from_user=SimpleNamespace(id=1),chat=SimpleNamespace(id=2),
                              answer=AsyncMock())
        item = DraftItem(Product("ozon", "", OZ, import_note="HTTP 403"))
        quote = Product("ozon", "Термос", OZ, 1490, image_urls=[PIC], price_source="wbcon",price_note="по данным WBCON")
        with patch.object(bot,"retrieve_item", AsyncMock(return_value=(item, "HTTP 403"))), \
             patch.object(wbcon,"lookup", AsyncMock(return_value=quote)), \
             patch.object(bot,"download_jpeg", AsyncMock(return_value=(b"photo",0))), \
             patch.object(bot,"show_preview", AsyncMock()) as preview, patch.object(bot,"publish", AsyncMock()) as send, \
             patch.object(bot,"AUTO_POST",True), patch.object(bot,"CHANNEL_ID",123):
            await bot.collect_links(msg, AsyncMock(), self.store, asyncio.Semaphore(1), [OZ])
        draft = self.store.get(preview.await_args.args[-1].id)
        self.assertTrue(draft.ready)
        self.assertEqual(draft.items[0].photo, b"photo")
        send.assert_not_awaited()

    async def test_explicit_price_beats_reserve_and_model_number_is_not_a_price(self):
        original = DraftItem(Product("ozon", "", OZ, 1490, price_source="wbcon"))
        item, used = await bot.supplement_message(original, SimpleNamespace(text=OZ+"\nТермос 500 — 1 390 ₽"),AsyncMock())
        self.assertTrue(used)
        self.assertEqual((item.product.title,item.product.price,item.product.price_source),("Термос 500",1390,"message"))
        item, _ = await bot.supplement_message(DraftItem(Product("ozon","",OZ)),SimpleNamespace(text="Термос 500\n"+OZ),AsyncMock())
        self.assertEqual((item.product.title,item.product.price), ("Термос 500",None))

    async def test_diag_isolated_and_informative_on_all_failed_sources(self):
        async def fetch(market, url, slots, **kw):
            step("Страница Ozon","HTTP 403")
            return DraftItem(Product(market,"",url,import_note="HTTP 403")), "HTTP 403"
        with capture() as outer, patch.object(bot,"retrieve_item",fetch), patch.object(wbcon,"lookup",AsyncMock(side_effect=ParseError("HTTP 429"))):
            report = await bot.diagnose("ozon:12345",asyncio.Semaphore(1))
            self.assertEqual(outer, [])
        for expected in ("403", "429", "Цена · нет", "файл не получен"):
            self.assertIn(expected, report)

    async def test_saved_page_creates_draft_and_checks_identity(self):
        doc = '<link rel="canonical" href="'+OZ+'"><script type="application/ld+json">'+json.dumps(
            {"@type":"Product","sku":"12345","name":"Термос","offers":{"priceCurrency":"RUB","price":1190}})+'</script>'
        async def download(file_id, destination):
            destination.write(doc.encode())
        client=AsyncMock()
        client.download.side_effect=download
        msg=SimpleNamespace(document=SimpleNamespace(file_id="file",file_size=1000),caption=None,
                            from_user=SimpleNamespace(id=1),chat=SimpleNamespace(id=1),answer=AsyncMock())
        with patch.object(bot,"is_admin",return_value=True), patch.object(bot,"show_preview",AsyncMock()) as preview:
            await bot.on_saved_page(msg,client,self.store,asyncio.Semaphore(1))
            self.assertEqual(preview.await_args.args[-1].items[0].product.price,1190)
        with self.assertRaises(ParseError):
            prices.saved_markup("wb:12345", doc)

    def test_purchase_links_survive_reaction(self):
        items = (DraftItem(Product("wb","Пылесос",WB,1190)), DraftItem(Product("ozon","Термос",OZ,999)))
        draft = self.store.create(1,1,items)
        markup = refresh_reaction(post_keyboard(draft,(WB,OZ)),draft.id,4)
        self.assertEqual([b.url for row in markup.inline_keyboard for b in row if b.url], [WB,OZ])
        self.assertEqual(markup.inline_keyboard[-1][0].text,"🔥 4")

    async def test_screenshot_requires_confirmation_and_preserves_cover(self):
        self.assertEqual(amounts("Код 555\n1 299,90 ₽\nДоставка 100 руб.\n1 299,90 ₽"),(Decimal("1299.90"),100))
        key=(1,2)
        self.addCleanup(bot.INPUTS.pop,key,None)
        item=DraftItem(Product("ozon","Термос",OZ),b"cover")
        draft=self.store.create(*key,(item,))
        req=bot.Input(draft.id,draft.revision,"screenshot")
        bot.INPUTS[key]=req
        msg=AsyncMock()
        msg.text=None
        with patch.object(bot,"attached_photo",AsyncMock(return_value=b"screen")),patch.object(bot,"recognize",AsyncMock(return_value=(Decimal("1299.90"),))):
            await bot.edit_value(msg,AsyncMock(),self.store,key,req)
        self.assertIsNone(self.store.get(draft.id).items[0].product.price)
        cb=AsyncMock()
        cb.from_user.id,cb.message.chat.id=key
        cb.data=msg.answer.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
        with patch.object(bot,"is_admin",return_value=True),patch.object(bot,"refresh",AsyncMock()):
            await bot.on_callback(cb,AsyncMock(),self.store,asyncio.Semaphore(1))
            await bot.on_callback(cb,AsyncMock(),self.store,asyncio.Semaphore(1))
        result=self.store.get(draft.id)
        self.assertEqual((result.items[0].product.price,result.items[0].photo,result.revision),(Decimal("1299.90"),b"cover",1))

    async def test_region_change_resets_baseline_before_comparing_prices(self):
        old = Product("wb","Пылесос",WB,2000,wb_dest="111")
        d = self.store.create(1,1,(DraftItem(old,b"photo"),))
        self.assertTrue(self.store.claim(d,77,0))
        self.store.finish(d,"sent",receipt=Receipt(77,"",2,"",0))
        watch=self.store.history.claim_watch(100000)
        new=DraftItem(replace(old,price=1500,wb_dest="222"),b"photo")
        result=await check_price(AsyncMock(),self.store,watch,Decimal(15),AsyncMock(return_value=(new,"")),AsyncMock(),asyncio.Semaphore(1))
        self.assertIsNone(result)
        again=self.store.history.claim_watch(200000)
        self.assertEqual((again.reference,again.product.wb_dest),(1500,"222"))
