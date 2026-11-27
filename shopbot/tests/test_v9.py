import asyncio
import io
import json
import os
import unittest
from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

import bot
import collage
import net
import ozon
import prices
import ui
import wb
from card import Product
from drafts import DraftItem, DraftStore
from links import extract_urls, product_link
from isolation import CleanState
from storefront import read_source

WB_URL = "https://www.wildberries.ru/catalog/14671602/detail.aspx"
OZ_URL = "https://www.ozon.ru/product/ryukzak-12345/"


def photo(size=(600, 800), color="#3366aa"):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "JPEG")
    return buffer.getvalue()


def wb_row(nm, price=129900, name="Термос"):
    return {"id": nm, "name": name, "totalQuantity": 5,
            "sizes": [{"optionId": 1, "price": {"basic": price * 2, "product": price}, "stocks": [{"qty": 3}]}]}


class LinkTests(unittest.TestCase):
    def test_links_without_scheme_and_rare_forms(self):
        text = "ozon.ru/t/EBIhF3Z и www.wildberries.ru/catalog/14671602/detail.aspx?size=5 почта a@ozon.ru"
        urls = extract_urls(text)
        self.assertEqual(urls, ["https://ozon.ru/t/EBIhF3Z", "https://www.wildberries.ru/catalog/14671602/detail.aspx?size=5"])
        self.assertEqual(str(product_link("https://www.wildberries.ru/product?card=14671602&size=77")[1]),
                         "https://www.wildberries.ru/catalog/14671602/detail.aspx?size=77")
        self.assertEqual(str(product_link("https://www.ozon.ru/context/detail/id/1526882900/?x=1")[1]),
                         "https://www.ozon.ru/product/1526882900/")
        self.assertEqual(product_link("ozon.ru/product/termos-1526882900/")[0], "ozon")

    def test_repeats_and_markdown_are_collapsed(self):
        text = f"{WB_URL} {WB_URL}, ({OZ_URL})"
        self.assertEqual(extract_urls(text), [WB_URL, OZ_URL])


class ArticleTests(unittest.TestCase):
    def test_market_is_not_guessed_for_bare_numbers(self):
        self.assertEqual(bot.parse_article("1526882900"), (None, "1526882900"))
        self.assertEqual(bot.parse_article("ozon 1526882900"), ("ozon", "1526882900"))
        self.assertEqual(bot.parse_article("Озон: 1526882900"), ("ozon", "1526882900"))
        self.assertEqual(bot.parse_article("wb866768707"), ("wb", "866768707"))
        self.assertIsNone(bot.parse_article("Термос 500"))
        self.assertIsNone(bot.parse_article("123"))

    def test_choice_buttons_fit_callback_limit(self):
        keyboard = bot.article_keyboard("9" * 16)
        for row in keyboard.inline_keyboard:
            for item in row:
                self.assertLessEqual(len(item.callback_data.encode()), 64)



class ArticleCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_choice_opens_the_chosen_market(self):
        cb = AsyncMock()
        cb.data = "bm:art:ozon:1526882900"
        cb.from_user.id = 5
        cb.message.chat.id = 7
        with patch.object(bot, "is_admin", return_value=True), patch.object(bot, "collect_links", AsyncMock()) as collect:
            await bot.on_callback(cb, AsyncMock(), AsyncMock(), asyncio.Semaphore(1))
        self.assertEqual(collect.await_args.args[4], ["https://www.ozon.ru/product/1526882900/"])
        self.assertEqual(collect.await_args.kwargs, {"user_id": 5, "hints": False})


class NetworkTests(CleanState, unittest.IsolatedAsyncioTestCase):
    def test_switch_off_and_missing_library(self):
        with patch.dict(os.environ, {"HTTP_IMPERSONATE": "0"}):
            self.assertFalse(net.impersonation_enabled())
            self.assertIsInstance(net.make_client(impersonate=True), httpx.AsyncClient)
        with patch.object(net, "curl_requests", None):
            self.assertFalse(net.impersonation_enabled())
            self.assertIn("pip install curl_cffi", net.describe())

    def test_curl_reply_becomes_httpx_response(self):
        class Reply:
            status_code = 307
            content = "тело".encode()
            headers = {"Location": "/next", "Content-Encoding": "gzip", "Content-Length": "999"}
        response = net._to_httpx("GET", "https://www.ozon.ru/a", Reply)
        self.assertTrue(response.has_redirect_location)
        self.assertEqual(response.text, "тело")  # уже распакованное тело не распаковывается второй раз

    async def test_cache_and_shared_request(self):
        calls = []

        async def load():
            calls.append(1)
            await asyncio.sleep(.01)
            return "ok"
        results = await asyncio.gather(net.once("k", load), net.once("k", load))
        self.assertEqual((results, len(calls)), (["ok", "ok"], 1))
        cache = net.TTLCache(60, limit=2)
        for key in "abc":
            cache.set(key, key)
        self.assertEqual((cache.get("a"), cache.get("c")), (None, "c"))

    async def test_one_retry_for_timeouts_but_not_for_blocks(self):
        calls = []

        def reply(request):
            calls.append(request.url.path)
            if len(calls) == 1:
                raise httpx.ConnectTimeout("медленно")
            return httpx.Response(200, text="ok")
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            response, note = await read_source(client, "https://card.wb.ru/x", wb.DOMAINS, "Проба")
        self.assertEqual((response.status_code, note, len(calls)), (200, "", 2))

        calls.clear()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: calls.append(1) or httpx.Response(403))) as client:
            response, note = await read_source(client, "https://card.wb.ru/x", wb.DOMAINS, "Проба")
        self.assertEqual((response, len(calls)), (None, 1))
        self.assertIn("HTTP 403", note)

    async def test_antibot_page_is_recognised(self):
        page = httpx.Response(200, text="<html><title>Antibot Challenge Page</title></html>")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: page)) as client:
            response, note = await read_source(client, "https://www.ozon.ru/x", ozon.DOMAINS, "Проба")
        self.assertIsNone(response)
        self.assertIn("проверки доступа", note)


class WildberriesTests(CleanState, unittest.IsolatedAsyncioTestCase):
    def test_endpoints_and_basket_table(self):
        self.assertTrue(all("v2" not in endpoint for endpoint in wb.DETAIL_ENDPOINTS))
        # Контрольные точки, измеренные независимо: vol 146 → 02, vol 8826 → 39, vol 10979 → 42.
        self.assertEqual([wb.predicted_basket(v) for v in (146, 8826, 10979)], [2, 39, 42])
        order = wb.basket_order(146)
        self.assertEqual(order[:3], [2, 3, 1])
        self.assertEqual(len(order), len(set(order)))

    async def test_basket_found_next_to_prediction_and_remembered(self):
        requests = []

        def reply(request):
            requests.append(request.url.host)
            if request.url.host == "basket-03.wbbasket.ru":
                return httpx.Response(200, json={"nm_id": 14671602, "imt_name": "Термос", "media": {"photo_count": 4}})
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            found = await wb._find_basket(client, 14671602)
            self.assertEqual(found[0], 3)
            requests.clear()
            self.assertEqual((await wb._find_basket(client, 14671602))[0], 3)
        self.assertEqual(len(requests), 1)  # второй раз сервер известен

    async def test_one_timeout_does_not_cancel_the_search(self):
        def reply(request):
            if request.url.host == "basket-02.wbbasket.ru":
                raise httpx.ConnectTimeout("медленно")
            if request.url.host == "basket-03.wbbasket.ru":
                return httpx.Response(200, json={"nm_id": 14671602, "imt_name": "Термос"})
            return httpx.Response(404)
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            found = await wb._find_basket(client, 14671602)
        self.assertEqual(found[0], 3)

    async def test_dead_network_stops_quickly(self):
        calls = []

        def reply(request):
            calls.append(1)
            raise httpx.ConnectError("нет связи")
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            self.assertIsNone(await wb._find_basket(client, 14671602))
        self.assertLess(len(calls), 20)

    async def test_collage_prices_come_from_one_request(self):
        requests = []

        def reply(request):
            requests.append(request.url.params.get("nm"))
            return httpx.Response(200, json={"data": {"products": [wb_row(101), wb_row(102, 55000, "Кружка")]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await wb.prefetch_details(client, [101, 102, 103])
            first = await wb._detail(client, 101, None, "https://www.wildberries.ru/catalog/101/detail.aspx")
            second = await wb._detail(client, 102, None, "https://www.wildberries.ru/catalog/102/detail.aspx")
        self.assertEqual(requests, ["101;102;103"])
        self.assertEqual((first.price, second.price, second.title), (Decimal("1299"), Decimal("550"), "Кружка"))

    async def test_substituted_response_is_ignored(self):
        junk = {"state": 0, "data": {"products": [wb_row(999)]}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=junk))) as client:
            product = await wb._detail(client, 101, None, "https://www.wildberries.ru/catalog/101/detail.aspx")
        self.assertIsNone(product.price)


class OzonTests(CleanState, unittest.IsolatedAsyncioTestCase):
    def widgets(self):
        return {"widgetStates": {
            "webProductHeading-1": json.dumps({"title": "Рюкзак"}),
            "webPrice-1": json.dumps({"price": "1 199 ₽", "originalPrice": "1 999 ₽"}),
            "webGallery-1": json.dumps({"coverImage": "https://ir.ozone.ru/a.jpg", "images": [{"src": "https://ir.ozone.ru/b.jpg"}]}),
        }}

    def client(self, handler):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_public_page_first_without_internal_api_or_warm_up(self):
        """v10: одна публичная страница вместо цепочки API → прогрев → второй API."""
        paths = []
        page = ('<link rel="canonical" href="https://www.ozon.ru/product/ryukzak-12345/">'
                '<script type="application/ld+json">' + json.dumps({
                    "@type": "Product", "sku": "12345", "name": "Рюкзак",
                    "image": ["https://ir.ozone.ru/a.jpg", "https://ir.ozone.ru/b.jpg"],
                    "offers": {"price": "1199", "priceCurrency": "RUB", "availability": "https://schema.org/InStock"}})
                + '</script>')

        def reply(request):
            paths.append(request.url.path)
            return httpx.Response(200, text=page)
        with patch.object(ozon, "make_client", lambda **kw: self.client(reply)):
            product = await ozon.fetch_ozon(OZ_URL)
        self.assertEqual((product.title, product.price, len(product.image_urls)), ("Рюкзак", Decimal("1199"), 2))
        self.assertEqual(paths, ["/product/ryukzak-12345/"])

    async def test_block_without_fingerprint_library_explains_the_fix(self):
        with patch.object(ozon, "make_client", lambda **kw: self.client(lambda r: httpx.Response(403))), \
             patch.object(ozon, "impersonation_enabled", return_value=False):
            product = await ozon.fetch_ozon(OZ_URL)
        self.assertIsNone(product.price)
        self.assertIn("HTTP 403", product.import_note)
        self.assertIn("curl_cffi", product.import_note)

    async def test_redirect_loop_is_reported_not_crashed(self):
        loop = lambda r: httpx.Response(307, headers={"Location": str(r.url)})
        with patch.object(ozon, "make_client", lambda **kw: self.client(loop)), \
             patch.object(ozon, "impersonation_enabled", return_value=True):
            product = await ozon.fetch_ozon(OZ_URL)
        self.assertIn("перенаправлени", product.import_note)


class ResultCacheTests(CleanState, unittest.IsolatedAsyncioTestCase):
    async def test_only_complete_results_are_reused(self):
        full = Product("wb", "Термос", WB_URL, Decimal(900), image_urls=["https://basket-01.wbbasket.ru/a.webp"])
        with patch.object(prices, "fetch_wb", AsyncMock(return_value=full)) as fetch:
            await prices.fetch_product(WB_URL, direct_only=True)
            await prices.fetch_product(WB_URL, direct_only=True)
            self.assertEqual(fetch.await_count, 1)
            await prices.fetch_product(WB_URL, direct_only=True, fresh=True)
            self.assertEqual(fetch.await_count, 2)
        prices._RESULT_CACHE.clear()
        partial = Product("wb", "Термос", WB_URL)
        with patch.object(prices, "fetch_wb", AsyncMock(return_value=partial)) as fetch:
            await prices.fetch_product(WB_URL, direct_only=True)
            await prices.fetch_product(WB_URL, direct_only=True)
            self.assertEqual(fetch.await_count, 2)


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)

    def item(self, i=0, ready=True, market="wb"):
        product = Product(market, f"Товар {i}" if ready else "", WB_URL, Decimal(100 + i) if ready else None,
                          image_urls=["https://basket-01.wbbasket.ru/a.webp"])
        return DraftItem(product, photo() if ready else None)

    def test_every_button_fits_telegram_limit(self):
        for count in range(1, 7):
            for ready in (True, False):
                draft = self.store.create(1, 2, tuple(self.item(i, ready) for i in range(count)))
                boards = [bot.preview_keyboard(draft)]
                for index in range(count):
                    boards.append(bot.InlineKeyboardMarkup(inline_keyboard=bot.item_buttons(draft, index)))
                    boards.append(bot.InlineKeyboardMarkup(inline_keyboard=bot.item_buttons(draft, index, more=True)))
                for board in boards:
                    for row in board.inline_keyboard:
                        self.assertLessEqual(len(row), 4)
                        for button in row:
                            self.assertLessEqual(len((button.callback_data or "").encode()), 64, button.text)

    def test_ready_draft_leads_with_publish_and_marks_gaps(self):
        draft = self.store.create(1, 2, (self.item(0), self.item(1, ready=False)))
        labels = [b.text for row in bot.preview_keyboard(draft).inline_keyboard for b in row]
        self.assertNotIn("🚀 Опубликовать", labels)
        self.assertIn("✏️ 2 ⚠️", labels)
        draft = self.store.create(1, 2, (self.item(0), self.item(1)))
        first = [b.text for b in bot.preview_keyboard(draft).inline_keyboard[0]]
        self.assertEqual(first, ["🚀 Опубликовать", "🕓 Запланировать"])
        self.assertIn("Готово к публикации", bot.preview_text(draft))

    def test_counts_and_list_labels(self):
        ready = self.store.create(1, 2, (self.item(0),))
        self.store.create(1, 2, (self.item(1, ready=False, market="ozon"),))
        self.store.create(1, 2, (self.item(2), self.item(3)))
        self.assertEqual(self.store.counts(1, 2), (3, 0, None))
        self.assertEqual(self.store.counts(9, 9), (0, 0, None))
        entries = {e.id: e for e in self.store.page(1, 2, False, 0, 10)}
        self.assertTrue(entries[ready.id].ready)
        label = ui.list_label(entries[ready.id], queue=False)
        self.assertTrue(label.startswith("🟢 🟣 Товар 0"))
        self.assertIn("100", label)
        multi = next(e for e in entries.values() if e.count == 2)
        self.assertIn("2 товара", ui.list_label(multi, queue=False))
        gap = next(e for e in entries.values() if not e.ready)
        self.assertTrue(ui.list_label(gap, queue=False).startswith("🟡 🔵"))

    def test_home_text_and_reply_keyboard(self):
        text = ui.home_text(3, 2, "28.11 10:00", 1)
        self.assertIn("Черновики — <b>3</b>", text)
        self.assertIn("ближайший 28.11 10:00", text)
        labels = {label for row in ui.REPLY_ROWS for label in row}
        self.assertTrue(labels <= set(ui.REPLY_ACTIONS))
        self.assertEqual(ui.REPLY_ACTIONS["📚 История"], "history")  # старая клавиатура в чатах продолжает работать
        self.assertEqual([ui.plural(n, "товар", "товара", "товаров") for n in (1, 2, 5, 11, 21)],
                         ["товар", "товара", "товаров", "товаров", "товар"])


class CollageStyleTests(unittest.TestCase):
    def setUp(self):
        self.store = DraftStore(":memory:")
        self.addCleanup(self.store.close)

    def product(self, **kwargs):
        base = dict(marketplace="ozon", title="Рюкзак городской водонепроницаемый с USB для ноутбука 15.6 дюймов",
                    url=OZ_URL, price=Decimal("2790"), old_price=Decimal("4990"))
        base.update(kwargs)
        return Product(**base)

    def test_wrap_cuts_at_word_boundary(self):
        face = collage.font(24, 600)
        lines = collage.wrap_lines("Термос Thermos вакуумный 500 мл нержавеющая сталь для чая и кофе", face, 300, 2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].endswith("…"))
        self.assertLessEqual(max(face.getlength(line) for line in lines), 300)
        self.assertNotIn(" …", lines[1])
        self.assertEqual(collage.wrap_lines("Кружка", face, 300), ["Кружка"])
        self.assertEqual(len(collage.wrap_lines("Сверхдлиннаяфразабезпробеловкоторуюнеудастсяразместить", face, 120, 2)), 1)

    def test_all_sizes_with_conditions_and_gaps(self):
        items = [
            DraftItem(self.product(price_note="с Ozon Картой", old_price=None), photo((900, 600))),
            DraftItem(self.product(marketplace="wb", price=Decimal("12990"), old_price=Decimal("14990")), photo((500, 900))),
            DraftItem(self.product(title="", price=None), None),
            DraftItem(self.product(price=Decimal("1299999.99")), photo()),
            DraftItem(self.product(), photo((1200, 700))),
            DraftItem(self.product(), photo((700, 700))),
        ]
        for count in range(1, 7):
            for layout in ("auto", "alternate"):
                draft = replace(self.store.create(1, 2, tuple(items[:count])), layout=layout)
                with Image.open(io.BytesIO(collage.post_photo(draft))) as image:
                    self.assertEqual(image.format, "JPEG")
                    self.assertEqual(image.width, 1080)
                    self.assertLess(image.height, 4000)
                    self.assertLessEqual(image.height / image.width, 4)

    def test_boxes_stay_inside_canvas(self):
        for n in range(2, 7):
            for mode in ("auto", "alternate"):
                for ratios in ([.6] * n, [1.65] * n, [.6, 1.65, .75, 1.2, .9, 1.1][:n]):
                    boxes = collage.layout_boxes(ratios, mode)
                    self.assertTrue(all(w > 0 and h > 0 and collage.MARGIN <= x and x + w <= 1080 - collage.MARGIN for x, y, w, h in boxes))

    def test_badges_can_be_switched_off(self):
        draft = self.store.create(1, 2, (DraftItem(self.product(), photo()),))
        raw = draft.items[0].photo
        with patch.dict(os.environ, {"IMAGE_BADGES": "0"}):
            self.assertEqual(collage.post_photo(draft), raw)
            many = self.store.create(1, 2, (draft.items[0], draft.items[0]))
            with Image.open(io.BytesIO(collage.post_photo(many))) as image:
                self.assertEqual(image.width, 1080)

    def test_without_variable_font_support_it_still_renders(self):
        collage.font.cache_clear()
        self.addCleanup(collage.font.cache_clear)
        with patch.object(collage.ImageFont.FreeTypeFont, "set_variation_by_axes", side_effect=OSError):
            draft = self.store.create(1, 2, (DraftItem(self.product(), photo()),) * 3)
            with Image.open(io.BytesIO(collage.post_photo(draft))) as image:
                self.assertEqual(image.width, 1080)


if __name__ == "__main__":
    unittest.main()
