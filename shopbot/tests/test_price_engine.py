"""Режимы источника цен (own / combo / legacy), журнал наблюдений и контекст предложения.

Все ответы сети — фикстуры. Суммы 2 969 / 2 672 / 3 083 ₽ взяты из наблюдений 27–28.11.2026
и используются только как тестовые значения.
"""
import json
import os
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx

import drafts
import monitor
import prices
import report
import state
import wbcon
from card import Product
from fixtures import (CHALLENGE, OZ_SKU, WB_NM, World, jpeg, ozon_page, ozon_product, wb_basket_card,
                      wb_detail, wb_history, wb_image, wb_row, wbcon_ozon, wbcon_wb)
from isolation import CleanState, reset_state
from links import article_url
from store_prices import wb_quote
from storefront import from_widget
from prices import saved_markup
from test_v10 import network

OZ = article_url("ozon", OZ_SKU)
WB = article_url("wb", WB_NM)
WBCON_OZ = httpx.Response(200, json={"article": int(OZ_SKU), "name": "Рюкзак", "price": 3083,
                                     "imageUrl": f"https://ir-20.ozone.ru/s3/multimedia-1-k/{OZ_SKU}1.jpg"})


def wb_closed():
    return [(wb_detail, httpx.Response(403, headers={"server": "wbaas"})),
            (wb_basket_card, lambda r: httpx.Response(200, json={"nm_id": int(WB_NM), "imt_name": "Робот-пылесос",
                                                                 "media": {"photo_count": 1}})),
            (wb_history, httpx.Response(404)), (wb_image, httpx.Response(200, content=jpeg()))]


class EngineModeTests(CleanState, unittest.IsolatedAsyncioTestCase):
    async def fetch(self, value, routes, engine):
        world = World(routes)
        with network(world), patch.dict(os.environ, {"PRICE_ENGINE": engine}):
            product = await prices.fetch_product(value, allow_paid=False)
        return product, world

    async def test_own_never_calls_wbcon_and_says_why_price_is_missing(self):
        product, world = await self.fetch(OZ, [(ozon_product, httpx.Response(403, text=CHALLENGE)),
                                               (wbcon_ozon, WBCON_OZ)], "own")
        self.assertEqual(world.calls["wbcon_ozon"], 0)
        self.assertIsNone(product.price)
        self.assertIn("price", product.gaps)

    async def test_own_wb_keeps_title_and_photo_without_aggregator(self):
        routes = wb_closed() + [(wbcon_wb, httpx.Response(200, json=[{"id": int(WB_NM), "salePriceU": 4113300}]))]
        product, world = await self.fetch(WB, routes, "own")
        self.assertEqual(world.calls["wbcon_wb"], 0)
        self.assertEqual((product.title, product.price), ("Робот-пылесос", None))

    async def test_combo_marks_reserve_with_unknown_age(self):
        product, world = await self.fetch(OZ, [(ozon_product, httpx.Response(403, text=CHALLENGE)),
                                               (wbcon_ozon, WBCON_OZ)], "combo")
        self.assertEqual((product.price, product.price_source, product.price_origin), (3083, "wbcon", "aggregator"))
        self.assertIsNone(product.price_at)  # сервис не сообщает, когда видел цену
        self.assertAlmostEqual(product.price_fetched_at, time.time(), delta=5)
        self.assertIn("резерв", report.price_age(product))
        self.assertIn("неизвестен", report.price_age(product))

    async def test_storefront_price_stays_first_and_is_not_reserve(self):
        product, world = await self.fetch(OZ, [(ozon_product, httpx.Response(200, text=ozon_page())),
                                               (wbcon_ozon, WBCON_OZ)], "combo")
        self.assertEqual((product.price, product.conditional_price, product.price_origin), (2969, 2672, ""))
        self.assertEqual(world.calls["wbcon_ozon"], 0)  # витрина полная — резерв не нужен

    async def test_combo_reuses_recorded_reserve_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "borzmarket.sqlite3"
            routes = wb_closed() + [(wbcon_wb, httpx.Response(200, json=[{"id": int(WB_NM), "salePriceU": 4113300,
                                                                         "supplier": "Продавец", "supplierId": 77}]))]
            state.configure(db)
            first, world = await self.fetch(WB, routes, "combo")
            self.assertEqual((first.price, first.price_origin), (Decimal("41133"), "aggregator"))
            self.assertEqual(first.seller, "Продавец (77)")
            reset_state()  # «перезапуск»: память процесса пуста, база та же
            state.configure(db)
            second, world2 = await self.fetch(WB, routes, "combo")
            state.reset()
        self.assertEqual(world2.calls["wbcon_wb"], 0)
        # Ответ WBCON не потерял происхождение: «из памяти бота», источник WBCON, возраст у источника неизвестен.
        self.assertEqual((second.price, second.price_source, second.price_origin), (Decimal("41133"), "wbcon", "memory"))
        self.assertIsNone(second.price_at)
        self.assertIn("из памяти бота", report.price_age(second))
        self.assertIn("неизвестен", report.price_age(second))

    async def test_combo_journal_covers_reserve_when_memory_has_no_price(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "borzmarket.sqlite3"
            state.configure(db)
            quote = Product("ozon", "", OZ, price=Decimal("3083"), price_source="wbcon", price_note="по данным WBCON",
                            price_fetched_at=int(time.time()) - 60, price_origin="aggregator")
            prices.observe(quote)  # в журнале есть ответ минуту назад, в памяти полей нет
            product, world = await self.fetch(OZ, [(ozon_product, httpx.Response(403, text=CHALLENGE)),
                                                   (wbcon_ozon, WBCON_OZ)], "combo")
            state.reset()
        self.assertEqual(world.calls["wbcon_ozon"], 1)  # название и фото резерв ещё может дать
        self.assertEqual((product.price, product.price_origin), (3083, "aggregator"))

    async def test_background_check_spends_wbcon_only_in_legacy(self):
        routes = [(ozon_product, httpx.Response(403, text=CHALLENGE)), (wbcon_ozon, WBCON_OZ)]
        calls = {}
        for mode in ("combo", "legacy"):
            reset_state()
            world = World(routes)
            with network(world), patch.dict(os.environ, {"PRICE_ENGINE": mode}):
                product = await prices.fetch_product(OZ, allow_paid=False, background=True)
            calls[mode] = (world.calls["wbcon_ozon"], product.price)
        self.assertEqual(calls, {"combo": (0, None), "legacy": (1, 3083)})

    def test_engine_switch_per_market(self):
        with patch.dict(os.environ, {"PRICE_ENGINE": "own", "WB_PRICE_ENGINE": "legacy", "OZON_PRICE_ENGINE": ""}):
            self.assertEqual((prices.engine("ozon"), prices.engine("wb")), ("own", "legacy"))
        with patch.dict(os.environ, {"PRICE_ENGINE": "что-то", "WB_PRICE_ENGINE": "", "OZON_PRICE_ENGINE": ""}):
            self.assertEqual(prices.engine("ozon"), "combo")
            self.assertIn("цены oz=combo wb=combo", report.export([]))


class ObservationJournalTests(CleanState, unittest.TestCase):
    def row(self, amount, seller="Лугарика", payment="regular"):
        return dict(market="ozon", sku=OZ_SKU, variant="", seller=seller, region="Москва", source="ozon.webPrice",
                    payment=payment, amount=amount, observed_at=None)

    def test_same_value_extends_record_and_change_creates_new(self):
        key = "ozon:" + OZ_SKU
        self.assertEqual(state.STATE.observe(key, self.row("2969"), now=1000), "new")
        self.assertEqual(state.STATE.observe(key, self.row("2969"), now=1600), "same")
        self.assertEqual(state.STATE.observe(key, self.row("2972"), now=2200), "new")
        self.assertEqual(state.STATE.observe(key, self.row("2972", seller="Другой"), now=2300), "new")
        rows = state.STATE.observations(key)
        self.assertEqual(len(rows), 3)
        first = rows[0]
        self.assertEqual((first["first_seen"], first["last_seen"], first["checks"]), (1000, 1600, 2))
        self.assertEqual(state.STATE.last_observation(key, "ozon.webPrice")["seller"], "Другой")

    def test_journal_survives_restart_and_skips_memory_and_manual(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "j.sqlite3"
            state.configure(db)
            product = Product("ozon", "Рюкзак", OZ, price=Decimal("2969"), price_source="ozon.webPrice",
                              conditional_price=Decimal("2672"), conditional_note="С банками",
                              price_fetched_at=int(time.time()) - 60, seller="Лугарика", region="Москва")
            prices.observe(product)
            prices.observe(Product("ozon", "", OZ, price=Decimal("1"), price_source="manual"))
            prices.observe(Product("ozon", "", OZ, price=Decimal("2"), price_source="ozon.webPrice",
                                   price_origin="memory"))
            state.configure(db)
            rows = state.STATE.observations(prices.memory_key(product))
            state.reset()
        self.assertEqual(sorted((r["payment"], r["amount"]) for r in rows),
                         [("regular", "2969"), ("С банками", "2672")])


class OfferContextTests(CleanState, unittest.TestCase):
    def test_ozon_seller_and_region_come_from_widgets_of_the_same_sku(self):
        base = Product("ozon", "", OZ)
        sticky = {"sku": OZ_SKU, "seller": {"name": "Лугарика", "link": "https://www.ozon.ru/seller/lugarika-123/"}}
        self.assertEqual(from_widget(base, "webStickyProducts", sticky).seller, "Лугарика (lugarika-123)")
        other = dict(sticky, sku="999999999")
        self.assertEqual(from_widget(base, "webStickyProducts", other).seller, "")  # чужой SKU — не наш продавец
        bar = {"customCell": {"cells": [{"button": {"text": "Москва"}}]}}
        self.assertEqual(from_widget(base, "addressBookBarWeb", bar).region, "Москва")
        empty = {"customCell": {"cells": [{"button": {"text": "Укажите адрес"}}]}}
        self.assertEqual(from_widget(base, "addressBookBarWeb", empty).region, "")

    def test_saved_page_keeps_seller_region_and_both_prices(self):
        sticky = json.dumps({"sku": OZ_SKU, "seller": {"name": "Лугарика", "link": "/seller/lugarika/"}},
                            ensure_ascii=False)
        bar = json.dumps({"customCell": {"cells": [{"button": {"text": "Москва"}}]}}, ensure_ascii=False)
        page = ozon_page().replace("</body>", f"<div id=\"state-webStickyProducts-1\" data-state='{sticky}'></div>"
                                              f"<div id=\"state-addressBookBarWeb-1\" data-state='{bar}'></div></body>")
        product = saved_markup(OZ, page)
        self.assertEqual((product.price, product.conditional_price), (2969, 2672))
        self.assertEqual((product.seller, product.region), ("Лугарика (lugarika)", "Москва"))
        self.assertIn("продавец Лугарика (lugarika)", report.offer_context(product))

    def test_wb_variant_and_seller_follow_the_selected_size(self):
        row = wb_row()
        row["supplier"], row["supplierId"] = "ИП Тест", 42
        row["sizes"] = [{"optionId": 11, "name": "42", "price": {"basic": 500000, "product": 300000}, "stocks": [{"qty": 1}]},
                        {"optionId": 12, "name": "43", "price": {"basic": 500000, "product": 320000}, "stocks": [{"qty": 1}]}]
        chosen = wb_quote(Product("wb", "", WB), row, size_id=12)
        self.assertEqual((chosen.price, chosen.seller), (3200, "ИП Тест (42)"))
        self.assertIn("43", chosen.variant)
        unchosen = wb_quote(Product("wb", "", WB), row)
        self.assertEqual(unchosen.variant, "")  # размер не выбран — варианта нет, цена «от» не выдаётся за точную

    def test_wbcon_wb_payload_in_kopecks_with_supplier(self):
        quote = wbcon.from_payload(Product("wb", "", WB), WB_NM,
                                   [{"id": int(WB_NM), "salePriceU": 4369058, "supplier": "Склад", "supplierId": 5}])
        self.assertEqual((quote.price, quote.seller, quote.region), (Decimal("43690.58"), "Склад (5)",
                                                                    "не указан сервисом"))
        self.assertIsNone(quote.price_at)


class CompatibilityTests(CleanState, unittest.IsolatedAsyncioTestCase):
    def test_old_draft_without_offer_fields_opens(self):
        store = drafts.DraftStore(":memory:")
        self.addCleanup(store.close)
        draft = store.create(1, 2, (drafts.DraftItem(Product("ozon", "Рюкзак", OZ, price=Decimal("2969"))),))
        data = json.loads(store.conn.execute("SELECT items FROM drafts WHERE id=?", (draft.id,)).fetchone()[0])
        for item in data:  # так черновик выглядел в v10.0
            for name in ("seller", "variant", "region", "price_origin", "price_fetched_at"):
                item.pop(name)
        store.conn.execute("UPDATE drafts SET items=? WHERE id=?", (json.dumps(data), draft.id))
        loaded = store.get(draft.id).items[0].product
        self.assertEqual((loaded.price, loaded.seller, loaded.price_origin), (2969, "", ""))

    async def test_monitor_ignores_a_price_recalled_from_memory(self):
        import asyncio
        from unittest.mock import AsyncMock
        from history import Receipt
        store = drafts.DraftStore(":memory:")
        self.addCleanup(store.close)
        product = Product("ozon", "Рюкзак", OZ, Decimal("2969"), price_source="ozon.webPrice")
        draft = store.create(1, 2, (drafts.DraftItem(product, jpeg()),))
        store.claim(draft, "@chan", int(time.time()))
        store.finish(draft, "sent", receipt=Receipt(-1, "@chan", 1, "", int(time.time()) - 2 * 86400))
        watch = store.history.claim_watch(int(time.time()))
        recalled = Product("ozon", "Рюкзак", OZ, Decimal("1000"), price_source="ozon.webPrice", price_origin="memory")
        fetch = AsyncMock(return_value=(drafts.DraftItem(recalled, jpeg()), ""))
        bot = AsyncMock()
        result = await monitor.check_price(bot, store, watch, Decimal("15"), fetch, AsyncMock(), asyncio.Semaphore(1))
        self.assertIsNone(result)  # «падение» на 66 % из памяти не превращается в черновик
        bot.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
