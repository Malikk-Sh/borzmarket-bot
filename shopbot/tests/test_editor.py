import asyncio
import io
import os
import unittest
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import bot
import wbcon
from card import Product
from collage import layout_boxes, post_photo
from drafts import DraftItem, DraftStore
from storefront import from_jsonld


def item(sku, price=None, market="ozon"):
    url = f"https://www.ozon.ru/product/{sku}/" if market == "ozon" else f"https://www.wildberries.ru/catalog/{sku}/detail.aspx"
    return DraftItem(Product(market, f"Товар {sku}", url, price))


class EditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fallback = patch.dict(os.environ, {"OZON_PRICE_FALLBACK": "0", "BHAPI_OZON_TOKEN": "", "BHAPI_WB_TOKEN": ""})
        fallback.start()
        self.addCleanup(fallback.stop)
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)
        self.editor = (212, 315)
        self.addCleanup(bot.INPUTS.pop, self.editor, None)

    def test_manual_prices_atomic(self):
        items = (item(1, 99), item(2), item(3, 45))
        result = bot.apply_prices("3 = 777\n2 = 1 299,90", items)
        self.assertEqual([p.product.price for p in result], [99, Decimal("1299.90"), 777])
        for bad in ("2 = 50\n2 = 100", "3 = 50\n9 = 22", "без номера", "", "1 = -50"):
            with self.assertRaises(ValueError):
                bot.apply_prices(bad,items)
        self.assertIsNone(items[1].product.price)

    async def test_append_in_chat_keeps_draft_and_removes_duplicate(self):
        existing = item(111, 15)
        new = item(222, 30)
        draft = self.store.create(*self.editor,(existing,))
        req = bot.Input(draft.id,draft.revision,"append")
        bot.INPUTS[self.editor] = req
        message = AsyncMock()
        message.text = f"{existing.product.url}\n{new.product.url}"
        message.from_user.id, message.chat.id = self.editor
        message.entities = message.caption_entities = []
        with patch.object(bot,"is_admin",return_value=True), patch.object(bot,"refresh",AsyncMock()), \
             patch.object(bot,"retrieve_item",AsyncMock(return_value=(new,""))) as fetch:
            await bot.on_message(message,AsyncMock(),self.store,asyncio.Semaphore(1))
        self.assertEqual(fetch.await_count,1)
        stored = self.store.get(draft.id)
        self.assertEqual([i.product.price for i in stored.items],[15,30])
        self.assertEqual(stored.heading,"Подборка BorzMarket")
        self.assertNotIn(self.editor,bot.INPUTS)

    async def test_cancel_during_fetch_keeps_original(self):
        draft = self.store.create(*self.editor,(item(123),))
        req = bot.Input(draft.id,draft.revision,"append")
        bot.INPUTS[self.editor] = req
        async def cancelled(*args, **kwargs):
            bot.INPUTS.pop(self.editor,None)
            return item(234), ""
        with patch.object(bot,"retrieve_item",cancelled):
            await bot.append_links(AsyncMock(),AsyncMock(),self.store,asyncio.Semaphore(1),
                                   self.editor,req,[item(234).product.url])
        self.assertEqual(len(self.store.get(draft.id).items),1)

    async def test_external_price_button_and_tag(self):
        original = item(717,market="wb")
        draft = self.store.create(*self.editor,(replace(original,photo=b"original"),))
        self.store.bind_view(draft,54)
        draft = self.store.get(draft.id)
        cb = AsyncMock()
        cb.message.chat.id = draft.chat_id
        cb.from_user.id = draft.owner_id
        cb.message.message_id = 54
        cb.data = next(b.callback_data for row in bot.preview_keyboard(draft).inline_keyboard
                       for b in row if b.text=="🌐 Проверить через WBCON")
        provider = wbcon.from_payload(original.product,"717",[{"id":717,"salePriceU":198590}])
        with patch.object(bot,"is_admin",return_value=True), patch.object(bot,"refresh",AsyncMock()), \
             patch.object(wbcon,"lookup",AsyncMock(return_value=provider)):
            await bot.on_callback(cb,AsyncMock(),self.store,asyncio.Semaphore(1))
        saved = self.store.get(draft.id)
        self.assertEqual((saved.items[0].product.price,saved.items[0].product.price_note),
                         (Decimal("1985.90"),"по данным WBCON"))
        self.assertEqual(saved.items[0].photo,b"original")

    def test_wb_zero_wrong_sku_and_region(self):
        product = item(404,market="wb").product
        self.assertIsNone(wbcon.from_payload(product,"404",[{"id":404,"salePriceU":0},{"id":405,"salePriceU":333}]).price)
        self.assertFalse(wbcon.available(replace(product,url=product.url+"?size=1")))
        with patch.dict(os.environ,{"WB_DEST":"other"}):
            self.assertFalse(wbcon.available(product))

    def test_ozon_provider_exact_id_and_images(self):
        p = item(999).product
        payload = {"article":999,"price":560,"name":"Сумка","imageUrl":"https://ir.ozone.ru/goods.jpg"}
        result = wbcon.from_payload(p,"999",payload)
        self.assertEqual((result.price,result.title,len(result.image_urls)),(560,"Сумка",1))
        self.assertFalse(wbcon.from_payload(p,"777",payload).title)

    def test_markup_nested_variant_matches_requested_sku(self):
        p = item(999).product
        doc = {"@type":"ProductGroup","hasVariant":[{"@type":"Product","sku":"888","name":"Другой"},
              {"@type":"Product","sku":"999","name":"Нужный","offers":{"price":560,"priceCurrency":"RUB"}}]}
        result=from_jsonld(replace(p,title=""),[doc])
        self.assertEqual((result.title,result.price),("Нужный",560))

    def test_all_layouts_and_saved_choice(self):
        for n in range(2,7):
            draft = self.store.create(*self.editor,tuple(item(i) for i in range(n)))
            for mode in ("auto","alternate"):
                boxes=layout_boxes([.75]*n,mode)
                self.assertEqual(len(boxes),n)
                self.assertTrue(all(w>0 and h>0 and 0<=x<x+w<=1080 for x,y,w,h in boxes))
                with Image.open(io.BytesIO(post_photo(replace(draft,layout=mode)))) as im:
                    self.assertEqual(im.width,1080)
            self.assertTrue(self.store.update(draft,draft.heading,draft.items,layout="alternate"))
            self.assertEqual(self.store.get(draft.id).layout,"alternate")
            self.assertFalse(self.store.update(draft,draft.heading,draft.items,layout="auto"))
