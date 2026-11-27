import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
import wbcon
from card import ParseError, Product
from diagnostics import capture
from prices import market_url


class ArticleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        wbcon._cache.clear()
        self.addCleanup(wbcon._cache.clear)

    async def test_normal_message_and_caption(self):
        url = "https://www.ozon.ru/product/1526882900/"
        for article in ("ozon:1526882900", "Озон 1526882900", "Ozon: 1526882900"):
            msg = SimpleNamespace(text=None, caption=article+"\nТермос — 1299 ₽", entities=[],
                                  caption_entities=[], photo=[], from_user=SimpleNamespace(id=9), chat=SimpleNamespace(id=9))
            with patch.object(bot, "is_admin", return_value=True), patch.object(bot, "collect_links", AsyncMock()) as collect:
                await bot.on_message(msg, AsyncMock(), None, asyncio.Semaphore(1))
            self.assertEqual(collect.await_args.args[-1], [url])
            item, _ = await bot.supplement_message(bot.DraftItem(Product("ozon","",url)),msg,AsyncMock())
            self.assertEqual((item.product.title,item.product.price),("Термос",1299))
            self.assertEqual(market_url(article), ("ozon",url))

    async def test_cached_success_during_cooldown_and_expiry(self):
        sku = "1526882900"
        p = Product("ozon", "", market_url("ozon:"+sku)[1])
        response = {"article":sku,"name":"Рюкзак","price":2969,"imageUrl":"https://ir.ozone.ru/photo.jpg"}
        with patch.object(wbcon,"request_prices",AsyncMock(side_effect=[response,ParseError("HTTP 429")])) as fetch:
            first = await wbcon.lookup(p)
            with capture() as steps:
                self.assertEqual((await wbcon.lookup(p)).price,first.price)
            self.assertEqual(fetch.await_count,1)
            self.assertIn(sku,steps[0])
            stamp, data = wbcon._cache[("ozon",sku)]
            wbcon._cache[("ozon",sku)] = (stamp-301,data)
            with self.assertRaises(ParseError):
                await wbcon.lookup(p)

    async def test_first_request_limited_then_recovered(self):
        p = Product("wb","","https://www.wildberries.ru/catalog/866768707/detail.aspx")
        with patch.object(wbcon, "request_prices", AsyncMock(side_effect=[ParseError("HTTP 429"),[{"id":866768707,"salePriceU":4113300}]])):
            with self.assertRaises(ParseError):
                await wbcon.lookup(p)
            self.assertEqual((await wbcon.lookup(p)).price,41133)
